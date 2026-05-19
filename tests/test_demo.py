import subprocess
import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
DEMO = EXAMPLES / "slice1_demo.py"
SLICE6_DEMO = EXAMPLES / "slice6_demo.py"


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


def test_slice6_demo_runs_three_scenarios():
    result = subprocess.run(
        [sys.executable, str(SLICE6_DEMO)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout

    # 1. Args-aware rule denied one tier, allowed the other.
    assert "update(tier='basic')      -> committed" in out
    assert "no_enterprise" in out

    # 2. Cap.sum tripped on the third charge.
    assert "Cap.sum(tool='charge'" in out
    assert "charges fired: []" in out

    # 3. Commit-time bracket marked the offending effect and rolled back
    #    the SQL side; HTTP charge never fired (preempted by the bracket).
    assert "where         = 'commit'" in out
    assert "txn state     = ROLLED_BACK" in out
    assert "SQL state after txn: []" in out
