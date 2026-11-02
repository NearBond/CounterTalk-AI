"""Dev scripts never print a traceback (CODING_GUIDELINES §8.5): a bad input gets one line and a
non-zero exit. Each runs with a bad input that costs nothing: never --yes. And each sets up
logging first (R5), so the frames-only excepthook is installed."""

import ast
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
    "show_quotes.py": ([UNKNOWN], {}),
    "compare_extraction.py": ([UNKNOWN, "no-such.expected.yaml"], {}),
    "export_eval_case.py": ([UNKNOWN, "rp99"], {}),
    "run_eval.py": (["--cases", "no-such-folder"], {}),
    "write_summary.py": ([UNKNOWN, "--org", "--period", "week"], {}),
    "load_patterns.py": (["--world", "no-such-world.yaml"], {}),
    "reprocess_failed.py": (
        ["--code", "llm_auth_failed", "--since", "2026-11-01"],
        {"DATABASE_URL": NO_SUCH_DATABASE},
    ),
}


def test_every_script_has_a_bad_input_here() -> None:
    # A leading underscore marks a module the scripts share, not a script (_paid.py).
    names = [path.name for path in SCRIPTS.glob("*.py") if not path.name.startswith("_")]
    assert sorted(names) == sorted(BAD_INPUTS)


def test_a_shared_module_has_no_main() -> None:
    shared = sorted(SCRIPTS.glob("_*.py"))

    assert [path.name for path in shared] == ["_k6.py", "_paid.py"]
    for path in shared:
        assert main_block(ast.parse(path.read_text(encoding="utf-8"))) == []


def main_block(tree: ast.Module) -> list[ast.stmt]:
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'":
            return node.body
    return []


def calls_configure_logging(statement: ast.stmt) -> bool:
    return ast.unparse(statement) == "configure_logging()"


@pytest.mark.parametrize("script", sorted(BAD_INPUTS))
def test_every_script_sets_up_logging_first_under_main(script: str) -> None:
    tree = ast.parse((SCRIPTS / script).read_text(encoding="utf-8"))
    imported = [
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "countertalk.logs"
        for alias in node.names
    ]

    assert "configure_logging" in imported
    assert [calls_configure_logging(s) for s in main_block(tree)][:1] == [True]


def test_the_logging_check_sees_a_missing_call() -> None:
    without = ast.parse('if __name__ == "__main__":\n    sys.exit(main())\n')
    with_it = ast.parse('if __name__ == "__main__":\n    configure_logging()\n    main()\n')

    assert [calls_configure_logging(s) for s in main_block(without)] == [False]
    assert [calls_configure_logging(s) for s in main_block(with_it)][:1] == [True]
    assert main_block(ast.parse("main()\n")) == []


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
