"""CI's workflow: things a review would otherwise have to remember (CODING_GUIDELINES §3.9)."""

import re

from countertalk.config import BACKEND_DIR

WORKFLOW = BACKEND_DIR.parent / ".github" / "workflows" / "ci.yml"


def test_every_ci_job_runs_on_a_pinned_runner() -> None:
    # GitHub moves the -latest labels to new images mid-project (batch 4).
    runners = re.findall(r"^\s+runs-on:\s*(\S+)", WORKFLOW.read_text(encoding="utf-8"), re.M)

    assert runners
    assert all(runner == "ubuntu-24.04" for runner in runners), runners
