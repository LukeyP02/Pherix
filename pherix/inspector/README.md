# Pherix inspector — the live governance console

The "see it" layer. A self-contained, **read-only** web console over a Pherix
audit journal. It renders the four axes happening live: the effect **timeline**
(interception + adapter), the **fold / gate / STUCK** at a glance (compensation),
and **per-effect policy verdicts** (policy). Zero third-party dependencies —
stdlib `http.server` + a static vanilla-JS frontend, fully offline.

## Run it against the demo journal

No live demos are needed — the inspector ships its own representative journal:

```bash
# 1. seed a demo journal (7 stories: clean commit, rollback, gated, STUCK,
#    dry-run, two attributed clients)
python -m pherix.inspector.seed demo.db

# 2. launch the console
python -m pherix.inspector --db demo.db
#    → http://127.0.0.1:8765/
```

Open the URL. Click any transaction on the left to see its effect timeline on
the right. Things to look for:

- **`txn-rollback-rel02`** — every effect struck through and marked *compensated*:
  the backward fold. Banner: "rolled back — nothing took effect."
- **`txn-gated-charge03`** — the irreversible `charge_card` held at the gate
  (amber/orange), the reversible read above it already applied.
- **`txn-stuck-payout04`** — the red **STUCK** banner: a compensator went missing
  mid-unwind; an operator must intervene.
- **`txn-dryrun-plan05`** — flagged `dry-run`; tick **hide dry-run** in the
  filters to get the compliance view.
- **`txn-clientA-q06` / `txn-clientB-w07`** — same gateway, different
  `client_id`: attribution in the audit view. Filter by client.

### Live mode

Click **live: off** (top right) to start polling. Point a running agent (or a
second `seed`/scripted txn) at the same `demo.db` and watch new transactions
flash in and effects land / unwind in real time — this is the demo footage.

## Run it against a real journal

Any Pherix audit DB works — it's whatever path you passed to
`AuditJournal(path)` (or the gateway's journal). The console opens it
**read-only** (`?mode=ro`), so it can never mutate the journal it's auditing:

```bash
python -m pherix.inspector --db /path/to/your/audit.db --port 8765
```

Flags: `--db` (required), `--host` (default `127.0.0.1`), `--port` (default
`8765`), `--verbose` (log each request).

## What it reads

The console is a pure consumer of the `transactions` + `effects` tables
(`pherix/core/audit.py`). The per-effect **effective verdict** is derived from
the persisted `status`:

| status | reads as | colour |
|---|---|---|
| `APPLIED` | executed & committed | green |
| `STAGED` | irreversible, held until commit | amber |
| `GATED` | blocked at the gate | orange |
| `COMPENSATED` | undone on rollback (struck through) | muted |
| `FAILED` | denied / errored — never took effect | red |

If the journal also carries the optional `verdicts` table (written by the
runtime's policy bracket), each effect additionally shows the **per-rule**
allow/deny/cap decisions at **stage** and **commit** time — including a
world-state divergence (allowed at stage, denied at commit). A journal without
that table renders the status-derived verdict only; nothing breaks.

## Layout

```
pherix/inspector/
  reader.py     read-only query layer over the journal (engine-free, robust)
  server.py     stdlib ThreadingHTTPServer + JSON API
  seed.py       the demo-journal generator (also the test fixtures)
  static/       index.html · app.js · style.css   (dark, monospace, no build)
  __main__.py   python -m pherix.inspector
```

Read-only by design: no auth, no multi-tenancy, no write operations, no
policy-building (that's the `governance-ui` task). This is the seed of the
control plane's audit view, not its finished form.
