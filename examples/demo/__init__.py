"""The Pherix headline demo — a deterministic, narrated three-act walkthrough.

Run:  python -m examples.demo

Three acts, each a *matched pair* — the same scripted agent action run once
WITHOUT Pherix and once WITH it, so the contrast is the message:

  Act 1 — Blast radius   a too-broad DELETE.  Without: rows gone.
                         With: savepoint rollback restores the table byte-exact.
  Act 2 — Oversight      a wrong/duplicate payment over an irreversible tool.
                         Without: the charge fires, money "gone".
                         With: the gate blocks commit — the charge never fires.
  Act 3 — Audit          replay the governed journal: every effect, every
                         status — "the record an auditor asks for."

NO live model, NO API key, NO network. The agent's actions are a fixed
script, but they run through the REAL engine — real SQLite SAVEPOINT
rollback, the real irreversible gate, the real append-only journal. The demo
shows the *mechanism*, deterministically: byte-identical output every run.
"""
