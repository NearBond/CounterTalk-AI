"""Sets up the browser checks' own database and data folder: a dev tool for web/e2e (batch 7).

    DATABASE_URL=…/countertalk_e2e DATA_DIR=<folder> E2E_PASSWORD=<password> \
        uv run python scripts/e2e_setup.py

It drops and recreates the database, and only one whose name ends in _e2e, so it can never wipe
the dev or test database. Then it migrates it, seeds the demo org, and makes two visits:
- a transcribed one: a few turns and a 20 s playback.m4a made with ffmpeg, never through Meta;
- one still recording.
It prints their IDs as JSON, for the runner. The words are made up, with no names. Then K6,
the planted patterns, in an org of its own, for the click check (batch 15c): see k6(). The
demo rep and K6's finance member sign in with E2E_PASSWORD (batch 16): the runner makes a
new one each run, so none is written down anywhere.
"""

import json
import os
import subprocess
import sys
from datetime import timedelta

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from countertalk import auth, issues, storage, transcription
from countertalk.config import BACKEND_DIR, get_settings
from countertalk.db import session_scope
from countertalk.errors import DomainError, describe
from countertalk.logs import configure_logging
from countertalk.models import (
    Distributor,
    Insight,
    Issue,
    Role,
    Shop,
    User,
    Visit,
    VisitClient,
    VisitKind,
    VisitStatus,
    ist_date,
    utc_now,
)
from countertalk.playbooks import load_org_pack
from countertalk.summaries import last_complete
from scripts import _k6
from scripts.seed_dev import DEMO_PACK, DEMO_REP, seed, seed_fixed

E2E_SUFFIX = "_e2e"
PLAYBACK_MS = 20_000
TURNS = [
    ("c0:A", 1_000, 4_000, "नमस्ते जी, कैसे हैं?"),
    ("c0:B", 6_000, 9_000, "सब ठीक है, आइए आइए।"),
    ("c0:A", 11_000, 14_000, "इस बार scheme का क्या हुआ?"),
    ("c0:B", 16_000, 19_000, "पाँच carton Kurkure लगा दीजिए।"),
]

# Made up, like the seed's, and with no consent yet (batch 17).
UNANSWERED_SHOPS = ("Link Kirana", "Paper Kirana")


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


def k6() -> dict[str, str]:
    """K6, the planted patterns (batch 15c's click check), in an org of its own, moved so its
    W6 is last week, as the summaries page shows it; E's issue merged into P1's, as a person
    would from the suggestion; and a tone as each of P1's visits' recording, so ▶ plays.
    What the check needs: the finance member's address, to sign in as, and D2's name; and a
    manager and an admin of K6's, for their homes' checks (batch F4). Made-up names."""
    last_week = ist_date(last_complete("week", utc_now()).start)
    world = _k6.load(w1_start=last_week - timedelta(weeks=5))
    p1, e = _k6.expected_merge(world)
    with session_scope() as session:
        versions = {
            i.id: i.version for i in session.scalars(select(Issue).where(Issue.id.in_([p1, e])))
        }
        issues.merge(
            session,
            lambda: world.pack,
            world.org_id,
            world.finance,
            (p1, versions[p1]),
            (e, versions[e]),
        )
        d2 = session.scalars(
            select(Distributor.name).where(
                Distributor.org_id == world.org_id, Distributor.key == "d2"
            )
        ).one()
        visits = session.scalars(
            select(Insight.visit_id).where(Insight.id.in_(world.insights["P1"]))
        ).all()
        leads = {}
        for role in (Role.MANAGER, Role.ADMIN):
            email = f"k6-{role.value}-{world.org_id[:8]}@example.com"
            name = f"K6 {role.value.capitalize()} (example)"
            session.add(User(org_id=world.org_id, email=email, name=name, role=role))
            leads[role.value] = email
    for visit_id in visits:
        make_playback(visit_id)
    return {"email": _k6.finance_email(world.org_id), "d2": d2, "p1": p1, **leads}


def main() -> int:
    name = make_url(get_settings().DATABASE_URL).database or ""
    if not name.endswith(E2E_SUFFIX):
        print(
            f"refusing: {name!r} isn't an e2e database (its name must end in {E2E_SUFFIX})",
            file=sys.stderr,
        )
        return 1
    password = os.environ.get("E2E_PASSWORD", "")
    if not password:
        print("no password: set E2E_PASSWORD (the runner makes one)", file=sys.stderr)
        return 1
    try:
        auth.long_enough(password)  # before anything is dropped
    except DomainError as error:
        print(f"E2E_PASSWORD: {describe(error)}", file=sys.stderr)
        return 1
    recreate(name)
    command.upgrade(Config(toml_file=str(BACKEND_DIR / "pyproject.toml")), "head")
    pack = load_org_pack(*DEMO_PACK)
    with session_scope() as session:
        seed(session, pack)
        rep = session.scalars(select(User).where(User.email == DEMO_REP["email"])).one()
        # Two shops that haven't answered (batch 17): the consent checks ask them, by link
        # and on paper.
        for name in UNANSWERED_SHOPS:
            session.add(Shop(org_id=rep.org_id, name=name, area="Old Market"))
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
        turns = [
            {"turn_id": n, "speaker_label": label, "start_ms": start, "end_ms": end, "text": words}
            for n, (label, start, end, words) in enumerate(TURNS)
        ]
        transcription.add_transcript(session, transcribed, "e2e", "seeded", "hi,en", turns)
    make_playback(transcribed.id)
    seed_fixed(pack)  # the record screen's "fixed since your last visit" (batch 17b)
    found = k6()
    with session_scope() as session:
        for email in (DEMO_REP["email"], found["email"], found["manager"], found["admin"]):
            assert auth.set_password(session, email, password), email  # seeded just now
    seeded = {"transcribed": transcribed.id, "recording": recording.id, "rep": DEMO_REP["email"]}
    print(json.dumps({**seeded, "k6": found}))
    return 0


if __name__ == "__main__":
    configure_logging()
    sys.exit(main())
