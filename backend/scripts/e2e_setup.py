"""Sets up the browser checks' own database and data folder: a dev tool for web/e2e (batch 7).

    DATABASE_URL=…/countertalk_e2e DATA_DIR=<folder> uv run python scripts/e2e_setup.py

It drops and recreates the database, and only one whose name ends in _e2e, so it can never wipe
the dev or test database. Then it migrates it, seeds the demo org, and makes two visits:
- a transcribed one: a few turns and a 20 s playback.m4a made with ffmpeg, never through Meta;
- one still recording.
It prints their IDs as JSON, for the runner. The words are made up, with no names.
"""

import json
import subprocess
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from countertalk import storage
from countertalk.config import BACKEND_DIR, get_settings
from countertalk.db import session_scope
from countertalk.logs import configure_logging
from countertalk.models import (
    Shop,
    Transcript,
    Turn,
    Visit,
    VisitClient,
    VisitKind,
    VisitStatus,
    utc_now,
)
from countertalk.playbooks import load_org_pack
from countertalk.users import demo_rep
from scripts.seed_dev import DEMO_PACK, seed

E2E_SUFFIX = "_e2e"
PLAYBACK_MS = 20_000
TURNS = [
    ("c0:A", 1_000, 4_000, "नमस्ते जी, कैसे हैं?"),
    ("c0:B", 6_000, 9_000, "सब ठीक है, आइए आइए।"),
    ("c0:A", 11_000, 14_000, "इस बार scheme का क्या हुआ?"),
    ("c0:B", 16_000, 19_000, "पाँच carton Kurkure लगा दीजिए।"),
]


def recreate(name: str) -> None:
    url = make_url(get_settings().DATABASE_URL)
    server = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with server.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        server.dispose()


def make_playback(visit_id: str) -> None:
    path = storage.playback_path(visit_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tone = f"sine=frequency=330:duration={PLAYBACK_MS / 1000}"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", tone]
        + ["-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(path)],
        check=True,
        timeout=60,
    )


def main() -> int:
    name = make_url(get_settings().DATABASE_URL).database or ""
    if not name.endswith(E2E_SUFFIX):
        print(
            f"refusing: {name!r} isn't an e2e database (its name must end in {E2E_SUFFIX})",
            file=sys.stderr,
        )
        return 1
    recreate(name)
    command.upgrade(Config(toml_file=str(BACKEND_DIR / "pyproject.toml")), "head")
    pack = load_org_pack(*DEMO_PACK)
    with session_scope() as session:
        seed(session, pack)
        rep = demo_rep(session)
        shop_id = session.scalars(
            select(Shop.id).where(Shop.org_id == rep.org_id).order_by(Shop.name)
        ).first()
        common = {
            "org_id": rep.org_id,
            "shop_id": shop_id,
            "rep_id": rep.id,
            "kind": VisitKind.CONVERSATION,
        }
        transcribed = Visit(
            **common,
            client=VisitClient.UPLOAD,
            status=VisitStatus.TRANSCRIBED,
            source_ext="m4a",
            completed_at=utc_now(),
            duration_ms=PLAYBACK_MS,
        )
        recording = Visit(**common, client=VisitClient.WEB)
        session.add_all([transcribed, recording])
        session.flush()
        transcript = Transcript(
            visit_id=transcribed.id,
            provider="e2e",
            model="seeded",
            language_code="hi,en",
            raw_dir=storage.relative_to_data_dir(storage.stt_dir(transcribed.id)),
        )
        session.add(transcript)
        session.flush()
        session.add_all(
            Turn(
                transcript_id=transcript.id,
                turn_id=n,
                speaker_label=label,
                start_ms=start,
                end_ms=end,
                text=words,
            )
            for n, (label, start, end, words) in enumerate(TURNS)
        )
        transcribed.transcript_id = transcript.id
    make_playback(transcribed.id)
    print(json.dumps({"transcribed": transcribed.id, "recording": recording.id}))
    return 0


if __name__ == "__main__":
    configure_logging()
    sys.exit(main())
