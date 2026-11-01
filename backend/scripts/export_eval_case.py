"""Exports a K1 visit as an eval case (batch 13): a dev tool, run once per recording.

    uv run python scripts/export_eval_case.py <visit_id> <rpNN> [--samples ../data/samples]
        [--to evals/fmcg-kirana-visit/v1]

Writes a new folder <to>/<rpNN>/, whole or not at all:
- turns.json: the visit's current transcript as the pipeline sees it (each turn's label, times
  and words), each label's saved role, the visit's time, and the org pack it was extracted
  with. run_eval.py builds its visit from this, so neither speech-to-text nor roles are paid
  for again;
- script_turns.json: <samples>/<rpNN>.turns.json, byte for byte, for the insights' timing;
- expected.yaml: <samples>/<rpNN>.expected.yaml with its draft header line marked as the checked
  answer key (today's date), every other byte as it was.

Nothing is paid. The cases are committed, so it exports K1's generated recordings only: the
visit must last as long as rpNN's script (within DURATION_SLACK_MS), and the expected file must
name rpNN. It also refuses a visit without saved roles, a name that isn't rpNN, an expected
file without the draft line, and a case that's there already (delete it to export again).
"""

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from countertalk import roles
from countertalk.config import BACKEND_DIR
from countertalk.db import SETUP_ERRORS, session_scope, setup_trouble
from countertalk.errors import DomainError, describe
from countertalk.logs import configure_logging
from countertalk.models import (
    Extraction,
    Org,
    SpeakerRole,
    Transcript,
    Visit,
    ist_date,
    parse_id,
    utc_now,
)
from countertalk.playbooks import read_yaml
from scripts.compare_extraction import K2File
from scripts.compare_transcript import BadReference, load_reference

CASES = BACKEND_DIR / "evals" / "fmcg-kirana-visit" / "v1"
SAMPLES = BACKEND_DIR.parent / "data" / "samples"
CASE_VERSION = 1
RECORDING = re.compile(r"rp[0-9]{2}")
DRAFT = re.compile(r"# Planner's draft .*, for the user to review and edit\.")
# K1's visits last their script's length to within a second or so (1 Nov: 0.8 to 2.8 s over).
DURATION_SLACK_MS = 5_000


class Refused(Exception):
    """Why the case can't be exported, in one line."""


def case_of(visit_id: str, recording: str, script_ms: int) -> dict[str, Any]:
    with session_scope(read_only=True) as session:
        visit = session.get(Visit, visit_id)
        if visit is None or visit.transcript_id is None:
            raise Refused(f"visit {visit_id} has no transcript")
        lasted = visit.duration_ms or 0
        if abs(lasted - script_ms) > DURATION_SLACK_MS:
            raise Refused(
                f"visit {visit_id} isn't {recording}'s recording: it lasts {lasted / 1000:.0f} s,"
                f" the script {script_ms / 1000:.0f} s"
            )
        transcript = session.get_one(Transcript, visit.transcript_id)
        saved = roles.saved_roles(session, transcript.id)
        if not saved:
            raise Refused(f"visit {visit_id}'s roles aren't saved")
        playbook = session.execute(
            select(SpeakerRole.playbook_id, SpeakerRole.playbook_version)
            .where(SpeakerRole.transcript_id == transcript.id)
            .limit(1)
        ).one()
        # The pack it was extracted with; the org's own if it hasn't been yet.
        pack = session.execute(
            select(Extraction.org_pack_id, Extraction.org_pack_version)
            .where(Extraction.visit_id == visit_id)
            .order_by(Extraction.seq.desc())
            .limit(1)
        ).first()
        if pack is None:
            org = session.get_one(Org, visit.org_id)
            pack = (org.org_pack_id, org.org_pack_version)
        turns = roles.load_turns(session, transcript.id)
    return {
        "version": CASE_VERSION,
        "recording": recording,
        "org_pack": {"id": pack[0], "version": pack[1]},
        "visited_at": visit.created_at.isoformat(),
        "transcript": {
            "provider": transcript.provider,
            "model": transcript.model,
            "language_code": transcript.language_code,
        },
        "roles": {
            "playbook": {"id": playbook.playbook_id, "version": playbook.playbook_version},
            "speakers": {
                label: {"role": role, "confidence": confidence}
                for label, (role, confidence) in saved.items()
            },
        },
        "turns": [
            {
                "turn_id": t.turn_id,
                "label": t.label,
                "start_ms": t.start_ms,
                "end_ms": t.end_ms,
                "text": t.text,
            }
            for t in turns
        ],
    }


def checked_key(raw: bytes, name: str) -> bytes:
    """The expected file with its draft header line marked as checked today; the rest as it
    was, line endings included."""
    lines = raw.decode("utf-8").splitlines(keepends=True)
    drafts = [n for n, line in enumerate(lines) if DRAFT.fullmatch(line.rstrip("\r\n"))]
    if len(drafts) != 1:
        raise Refused(f"{name} hasn't one draft header line to mark as checked")
    line = lines[drafts[0]]
    today = ist_date(utc_now())
    lines[drafts[0]] = (
        f"# Answer key, checked {today.day} {today:%b %Y}." + line[len(line.rstrip("\r\n")) :]
    )
    return "".join(lines).encode("utf-8")


def answer_key(path: Path, name: str) -> None:
    """Refused unless it's a K2 file, read strictly, for this recording."""
    try:
        key = K2File.model_validate(read_yaml(path))
    except ValidationError:
        raise Refused(f"{path.name} isn't an answer key") from None
    if key.recording != name:
        raise Refused(f"{path.name} is {key.recording}'s answer key")


def read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        raise Refused(f"{path} can't be read") from None


def write_case(folder: Path, files: dict[str, bytes]) -> None:
    """A new case folder, whole or not at all: written beside it, then renamed into place."""
    if folder.exists():
        raise Refused(f"{folder} is there already: delete it to export again")
    partial = folder.with_name(f".{folder.name}.partial")
    try:
        shutil.rmtree(partial, ignore_errors=True)  # a run that stopped part-way
        partial.mkdir(parents=True)
        for name, body in files.items():
            (partial / name).write_bytes(body)
        os.replace(partial, folder)
    except OSError as error:
        shutil.rmtree(partial, ignore_errors=True)
        raise Refused(f"{folder} can't be written ({type(error).__name__})") from None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="export_eval_case.py")
    parser.add_argument("visit_id")
    parser.add_argument("recording", help="rpNN")
    parser.add_argument("--samples", type=Path, default=SAMPLES, help="K1 and K2's folder")
    parser.add_argument("--to", type=Path, default=CASES, help="the eval cases' folder")
    args = parser.parse_args(argv)
    visit_id = parse_id(args.visit_id)
    if visit_id is None:
        parser.error("that isn't a visit ID")
    if RECORDING.fullmatch(args.recording) is None:
        parser.error("the recording's name is rp and two digits, like rp01")
    name = args.recording
    script_path = args.samples / f"{name}.turns.json"
    expected_path = args.samples / f"{name}.expected.yaml"
    try:
        script = read(script_path)
        spans = load_reference(script_path, 0)
        answer_key(expected_path, name)
        case = case_of(visit_id, name, max((s.end_ms for s in spans), default=0))
        expected = checked_key(read(expected_path), expected_path.name)
        body = json.dumps(case, ensure_ascii=False, indent=1) + "\n"
        files = {
            "turns.json": body.encode(),
            "script_turns.json": script,
            "expected.yaml": expected,
        }
        write_case(args.to / name, files)
    except Refused as why:
        print(f"can't export {name}: {why}", file=sys.stderr)
        return 1
    except BadReference as why:
        print(f"can't export {name}: {script_path.name}: {why}", file=sys.stderr)
        return 1
    except SETUP_ERRORS as error:
        print(f"failed: {setup_trouble(error)}", file=sys.stderr)
        return 1
    except DomainError as error:  # an expected file that isn't YAML, or is missing
        print(f"failed: {describe(error)}", file=sys.stderr)
        return 1
    folder = args.to / name
    print(
        f"{name}: {len(case['turns'])} turns, {len(case['roles']['speakers'])} labels -> {folder}"
    )
    return 0


if __name__ == "__main__":
    configure_logging()
    sys.exit(main(sys.argv[1:]))
