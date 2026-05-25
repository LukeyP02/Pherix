"""Entry point — `python -m examples.demo`.

Runs the three acts in order, narrates each matched before/after, replays the
governed journal, then writes the watchable session JSON the web player
(docs/demo.html) animates. Deterministic, offline, no key.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Make the package runnable without an editable install — put the repo root on
# the path before importing pherix (matches examples/slice3_demo.py house style).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from pherix import AuditJournal  # noqa: E402

from examples.demo import acts, session  # noqa: E402

RULE = "=" * 72


def _banner(act_no: int, title: str, tagline: str) -> None:
    print(RULE)
    print(f"ACT {act_no} — {title}  ·  {tagline}")
    print(RULE)


def _narrate(res: acts.ActResult) -> None:
    for line in res.lines:
        print(f"  {line}")
    mark = "contained ✓" if res.contained else "NOT contained ✗"
    print(f"  --> {mark}")
    print()


def main() -> int:
    print()
    print("Pherix — ACID for agents")
    print("database guarantees over an agent's real-world actions:")
    print("undo the reversible · gate the irreversible · audit everything")
    print()

    # One durable, temp-file journal across the governed runs, so the inspector
    # can open the very same DB the demo wrote. Printed at the end.
    journal_path = str(Path(tempfile.gettempdir()) / "pherix_demo_journal.db")
    Path(journal_path).unlink(missing_ok=True)  # fresh each run -> deterministic
    audit = AuditJournal(journal_path)

    # Act 2 leads the narrative (the wedge) but the journal is naturally
    # ordered by transaction; we run Act 1 then Act 2 so audit (Act 3) reads
    # both governed transactions back in the order they happened.
    _banner(1, "Blast radius", "a mistake is contained, not catastrophic")
    print("  The agent ships a WHERE-less DELETE — the classic blast-radius bug.")
    print()
    r1 = acts.act1_blast_radius(audit)
    _narrate(r1)

    _banner(2, "Oversight", "the wedge — a human stays on the irreversible")
    print("  The agent tries to wire $480,000 to a wrong/attacker account. Money")
    print("  cannot be un-sent, so this is the effect that matters most.")
    print()
    r2 = acts.act2_oversight(audit)
    _narrate(r2)

    _banner(3, "Audit", "the record an auditor asks for")
    print("  No new action — Act 3 reads back the journal the governed runs wrote.")
    print("  The journal IS the audit log: every effect, every status.")
    print()
    governed = r1.txn_ids + r2.txn_ids
    r3 = acts.act3_audit(audit, governed)
    _narrate(r3)

    results = {r1.marker: r1, r2.marker: r2, r3.marker: r3}

    # Write the watchable session JSON (the web player's timeline) — one
    # continuous session driven through the real engine, deterministic. The
    # player at docs/demo.html inlines this shape; it is NOT overwritten here.
    session_path = session.write()

    # Surface the journal + inspector as the next step.
    print(RULE)
    print("Next steps")
    print(RULE)
    print(f"  Session JSON   : {session_path}")
    print(f"  Journal DB     : {journal_path}")
    print("  Explore it     : python -m pherix.inspector")
    print("                   (launches a local server — open the journal DB above)")
    print("  Watch the demo : python -m http.server  (then open docs/demo.html)")
    print()

    all_contained = all(r.contained for r in results.values())
    print(
        "RESULT: blast-radius "
        + ("✓" if r1.contained else "✗")
        + "  ·  oversight "
        + ("✓" if r2.contained else "✗")
        + "  ·  audit "
        + ("✓" if r3.contained else "✗")
    )
    print()

    audit.close()
    return 0 if all_contained else 1


if __name__ == "__main__":
    raise SystemExit(main())
