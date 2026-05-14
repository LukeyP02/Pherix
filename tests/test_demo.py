import subprocess
import sys
from pathlib import Path

DEMO = Path(__file__).resolve().parent.parent / "examples" / "slice1_demo.py"


def test_slice1_demo_runs_and_tells_the_story():
    result = subprocess.run(
        [sys.executable, str(DEMO)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout

    # rollback wiped txn 1's writes; commit persisted txn 2's
    assert "after rollback: []" in out
    assert "('ada', 'engineer'), ('grace', 'scientist')" in out

    # the journal carries both stories
    assert "state=ROLLED_BACK" in out
    assert "state=COMMITTED" in out
    assert "COMPENSATED" in out
    assert "APPLIED" in out
