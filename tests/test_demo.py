import subprocess
import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
DEMO = EXAMPLES / "slice1_demo.py"
SLICE6_DEMO = EXAMPLES / "slice6_demo.py"
SLICE7_DEMO = EXAMPLES / "slice7_demo.py"


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


def test_slice7_demo_runs_three_scenarios():
    result = subprocess.run(
        [sys.executable, str(SLICE7_DEMO)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout

    # 1. Clean mixed txn — journal materialises, irreversibles stay STAGED,
    #    nothing fires, world unchanged.
    assert "would_have_fired:  ['notify']" in out
    assert "notify_fires:      []" in out
    assert "status=STAGED" in out
    assert "status=COMPENSATED" in out

    # 2. Policy denial captured at BOTH stage and commit, body keeps running.
    assert "journal materialised: 3 effects" in out
    assert "is_clean:             False" in out
    assert "where='stage' rule='no_enterprise'" in out
    assert "where='commit' rule='no_enterprise'" in out

    # 3. The load-bearing pin: SQL + FS bit-identical before/after.
    assert "SQL identical?  True" in out
    assert "FS identical?   True" in out
