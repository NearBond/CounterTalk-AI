"""Gives a transcribed visit's speaker labels their roles with Muse Spark: a dev tool (batch 9).

    uv run python scripts/assign_roles.py <visit_id> [--yes]
        [--root <folder>] [--playbooks <folder>]

Without --yes it prints what would be sent: the labels, the turns and characters, and an
estimate of the tokens and cost. It sends nothing. With --yes it asks Muse Spark, with
MODEL_API_KEY from backend/.env, saves the roles, and prints each label's role and each call's
tokens and cost. Roles saved already are printed, and nothing is sent: they're never asked for
twice. Until batch 10's extract job, roles run only from here.

It prints labels, roles, counts and costs: never the transcript's words. --root and --playbooks
are the org packs' and playbooks' folders, for a test's own packs.
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from countertalk import llm_calls, roles
from countertalk.db import SETUP_ERRORS, session_scope, setup_trouble
from countertalk.errors import DomainError, describe
from countertalk.llm.base import StructuredLlm
from countertalk.logs import configure_logging
from countertalk.models import LlmPurpose, Org, Visit, parse_id
from countertalk.playbooks import ORG_PACKS_DIR, PLAYBOOKS_DIR, OrgPack, load_org_pack
from scripts._paid import estimate, model_llm, paid_since, show_calls

# A dry run's estimate of the answer: each label's line. Reasoning is added on top (pricing).
ANSWER_TOKENS_PER_LABEL = 25


@dataclass(frozen=True)
class Found:
    transcript_id: str
    pack: OrgPack
    saved: dict[str, tuple[str, float]]


def find(visit_id: str, root: Path, playbooks: Path) -> Found | str:
    """The visit's transcript, its org's pack and any saved roles, or why it can't have roles."""
    with session_scope(read_only=True) as session:
        visit = session.get(Visit, visit_id)
        if visit is None:
            return "there's no such visit"
        if visit.transcript_id is None:
            return "it has no transcript"
        org = session.get_one(Org, visit.org_id)
        transcript_id, pack_id, version = visit.transcript_id, org.org_pack_id, org.org_pack_version
        saved = roles.saved_roles(session, transcript_id)
    if pack_id is None or version is None:
        return "its org isn't on an org pack"
    return Found(transcript_id, load_org_pack(pack_id, version, root, playbooks), saved)


def show_roles(found: dict[str, tuple[str, float]]) -> None:
    for label, (role, confidence) in found.items():
        print(f"  {label:<7} {role:<11} {confidence:.2f}")


def dry_run(visit_id: str, request: roles.Request) -> None:
    answer = len(request.labels) * ANSWER_TOKENS_PER_LABEL
    found = estimate(request.messages, request.schema, answer)
    print(
        f"visit {visit_id}: {request.turns} turns, {len(request.labels)} labels:"
        f" {', '.join(request.labels)}"
    )
    print(
        f"to send: {request.sent} turns, {found.chars:,} characters with the instructions"
        " and schema"
    )
    print(
        f"about {found.tokens_in:,} input and {found.tokens_out:,} output tokens,"
        f" ${found.usd:.4f} a request"
        " (an estimate: a re-ask or a repair sends another; --yes sends it)"
    )


def saved_already(visit_id: str, saved: dict[str, tuple[str, float]]) -> int:
    print(f"visit {visit_id}: roles saved already, nothing sent")
    show_roles(saved)
    return 0


def run(visit_id: str, found: Found, llm: StructuredLlm) -> int:
    before = llm_calls.latest_seq(visit_id)
    try:
        assigned = roles.assign_roles(found.transcript_id, llm, found.pack)
    except DomainError as error:
        show_calls(paid_since(visit_id, before, LlmPurpose.ROLES))
        print(f"failed: {describe(error)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        show_calls(paid_since(visit_id, before, LlmPurpose.ROLES))
        print("stopped: a call in flight may still be billed; no roles saved", file=sys.stderr)
        return 130
    if not assigned.calls:  # another run saved them meanwhile
        return saved_already(visit_id, assigned.roles)
    print(f"visit {visit_id}: roles for {len(assigned.roles)} labels")
    show_roles(assigned.roles)
    show_calls(assigned.calls)
    return 0


def main(argv: list[str], llm: StructuredLlm | None = None) -> int:
    parser = argparse.ArgumentParser(prog="assign_roles.py")
    parser.add_argument("visit_id")
    parser.add_argument("--yes", action="store_true", help="send it, and pay")
    parser.add_argument("--root", type=Path, default=ORG_PACKS_DIR, help="the org packs' folder")
    parser.add_argument(
        "--playbooks", type=Path, default=PLAYBOOKS_DIR, help="the playbooks' folder"
    )
    args = parser.parse_args(argv)
    visit_id = parse_id(args.visit_id)
    if visit_id is None:
        parser.error("that isn't a visit ID")
    try:
        found = find(visit_id, args.root, args.playbooks)
        if isinstance(found, str):
            print(f"can't give visit {visit_id} roles: {found}", file=sys.stderr)
            return 1
        if found.saved:
            return saved_already(visit_id, found.saved)
        if not args.yes:
            request = roles.request_for(found.transcript_id, found.pack)
            if isinstance(request, roles.Assigned):
                return saved_already(visit_id, request.roles)
            dry_run(visit_id, request)
            return 0
        llm = llm or model_llm()
        if llm is None:
            return 1
        return run(visit_id, found, llm)
    except SETUP_ERRORS as error:
        print(f"failed: {setup_trouble(error)}", file=sys.stderr)
        return 1
    except DomainError as error:  # a pack that doesn't check out
        print(f"failed: {describe(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    configure_logging()  # each call's line: tokens, latency and request ID, never the prompt
    sys.exit(main(sys.argv[1:]))
