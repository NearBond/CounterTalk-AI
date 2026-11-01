"""Dev scripts never print a traceback (CODING_GUIDELINES §8.5): a bad input gets one line and a
non-zero exit. Each runs with a bad input that costs nothing: never --yes."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from countertalk.config import BACKEND_DIR
from countertalk.models import new_id
from tests.conftest import ORG_PACK_FIXTURES, SERVER_URL

SCRIPTS = BACKEND_DIR / "scripts"
NO_SUCH_DATABASE = f"{SERVER_URL}/countertalk_no_such_database"
RUN_TIMEOUT_S = 60
UNKNOWN = new_id()

BAD_INPUTS = {
    "check_pack.py": (["no-such-pack", "1"], {}),
    "check_setup.py": (["--database"], {"DATABASE_URL": NO_SUCH_DATABASE}),
    "compare_transcript.py": ([UNKNOWN, "no-such-reference.turns.json"], {}),
    "e2e_setup.py": ([], {}),  # the test database isn't named for e2e, so it's refused
    "import_org_pack.py": (["No Such Org", "tiny-demo", "1", "--root", str(ORG_PACK_FIXTURES)], {}),
    "make_test_audio.py": (["rp99", "--samples", "no-such-folder"], {}),
    "prepare_visit.py": ([UNKNOWN], {}),
    "seed_dev.py": ([], {"DATABASE_URL": NO_SUCH_DATABASE}),
    "transcribe_visit.py": ([UNKNOWN], {}),
    "assign_roles.py": ([UNKNOWN], {}),
    "extract_visit.py": ([UNKNOWN], {}),
}


def test_every_script_has_a_bad_input_here() -> None:
    assert sorted(path.name for path in SCRIPTS.glob("*.py")) == sorted(BAD_INPUTS)


@pytest.mark.parametrize("script", sorted(BAD_INPUTS))
def test_a_bad_input_is_one_line_and_no_traceback(script: str, tmp_path: Path) -> None:
    args, env = BAD_INPUTS[script]

    result = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        cwd=tmp_path,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=RUN_TIMEOUT_S,
    )

    assert result.returncode != 0
    assert "Traceback" not in result.stdout + result.stderr
    # check_setup reports on stdout, a line per check; the others say what's wrong on stderr.
    assert len(result.stderr.strip().splitlines()) == (0 if script == "check_setup.py" else 1)
