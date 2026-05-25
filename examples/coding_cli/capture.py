"""Offline governed-path capture for the coding-CLI MCP gateway.

**This is NOT a real coding CLI.** It is the deterministic, no-key *capture* that
exercises the gateway exactly as a real MCP client would — over the same
``MCPServer.handle`` surface a subprocess speaks JSON-RPC to — and prints the
resulting audit journal as shareable evidence. The genuinely-live run (a real
``aider`` / Claude-Code session driven by a model, with a key) is documented step
by step in ``FINDINGS.md``; this script stands in for that operator only to prove
the *governed path* is correct and to produce a journal you can read.

It drives the four canonical coding-agent outcomes through one
:class:`InProcessMCPClient` session, each call its own auto-committing
transaction (the one-shot-call model — see ``FINDINGS.md``):

  1. ``write_file`` — a reversible edit that **commits** (APPLIED).
  2. ``apply_code_edit`` with broken Python — the post-write compile check
     raises, so Pherix **rolls the live write back** (the txn FAILS, nothing left
     on disk).
  3. ``git_push`` — irreversible, no compensator, so it **gates** at commit
     (STAGED, never fired — the push never leaves the machine).
  4. ``run_shell`` ``rm -rf .`` — **policy-denied** at stage-time (nothing
     journalled APPLIED, the resource never touched).

Then it reads the on-disk audit journal back and prints, per transaction, the
final ``TxnState`` and each effect's ``EffectStatus`` + the ``client_id`` the
session ran under — the journal *is* the evidence.

Run it::

    python -m examples.coding_cli.capture

Fully offline: the gateway dispatches tools, it never calls an LLM. No key, no
network.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from examples.dogfood.infra import scratch_repo
from examples.coding_cli.gateway import build_gateway
from pherix.core.audit import AuditJournal
from pherix.frontends.proxy import InProcessMCPClient

# The handshake identity this capture announces — one of CODING_CLI_IDENTITIES,
# so the gateway grants the coding policy (an unrecognised name hits the
# deny-all floor).
_IDENTITY = "aider"

_BROKEN_PY = "def main(:\n    print('syntax error'\n"  # will not compile()
_GOOD_PY = "def main():\n    print('v2')\n"


def _run_capture(audit_path: str, repo_root: Path) -> list[str]:
    """Drive the four scenarios through the gateway; return the txn_ids in order.

    The gateway is built against the scratch repo and the on-disk audit journal
    so the journal survives this function and can be read back as evidence.
    """
    os.environ["PHERIX_CODING_REPO"] = str(repo_root)
    os.environ["PHERIX_CODING_AUDIT"] = audit_path
    gateway = build_gateway(repo_root)

    client = InProcessMCPClient(gateway)
    client.initialize(_IDENTITY)

    tools = sorted(t["name"] for t in client.tool_descriptors())
    print(f"  client identity announced : {_IDENTITY!r}")
    print(f"  governed tools over MCP    : {', '.join(tools)}")
    print()

    txn_ids: list[str] = []

    def call(label: str, name: str, args: dict) -> None:
        envelope = client.call_tool(name, args)
        sc = InProcessMCPClient.structured_of(envelope)
        is_err = envelope["result"]["isError"]
        txn_id = sc.get("txn_id")
        if txn_id:
            txn_ids.append(txn_id)
        if not is_err:
            verdict = f"COMMITTED  result={sc.get('result')!r}"
        else:
            verdict = f"REFUSED    {sc.get('pherix_error')} (code {sc.get('code')})"
        print(f"  [{label}] {name}")
        print(f"        -> {verdict}")
        if is_err:
            print(f"        -> {sc.get('message')}")
        print()

    # 1. reversible edit that commits
    call("1 reversible commit", "write_file",
         {"path": "app.py", "content": _GOOD_PY})

    # 2. broken edit that the compile check rejects → Pherix rolls it back
    call("2 broken edit rolls back", "apply_code_edit",
         {"path": "app.py", "content": _BROKEN_PY})

    # 3. irreversible push → gates at commit (never fires)
    call("3 push gates", "git_push",
         {"remote": "origin", "branch": "HEAD"})

    # 4. rm -rf → denied at stage-time by policy
    call("4 rm -rf denied", "run_shell",
         {"command": "rm -rf ."})

    return txn_ids


def _print_journal(audit_path: str, txn_ids: list[str]) -> None:
    """Read the audit journal back and print txn states + per-effect statuses."""
    audit = AuditJournal(audit_path)
    try:
        print("=" * 72)
        print("AUDIT JOURNAL — the evidence (read back from the on-disk SQLite db)")
        print("=" * 72)
        # Preserve call order, de-dupe (each call is its own txn here anyway).
        seen: set[str] = set()
        for txn_id in txn_ids:
            if txn_id in seen:
                continue
            seen.add(txn_id)
            txn = audit.get_transaction(txn_id)
            effects = audit.get_effects(txn_id)
            client_id = txn.get("client_id") if txn else None
            state = txn.get("state") if txn else "<missing>"
            print(f"\n  txn {txn_id}  state={state}  client_id={client_id!r}")
            for e in effects:
                rev = "reversible" if e["reversible"] else "irreversible"
                print(
                    f"      effect[{e['idx']}] {e['tool']:<16} "
                    f"resource={e['resource']:<5} {rev:<12} status={e['status']}"
                )
        print()
        print("Reading: a committed edit's effect is APPLIED under a COMMITTED txn;")
        print("the broken edit FAILS and its txn ROLLED_BACK (write reverted);")
        print("the push STAGED but its txn never commits (gate); the rm -rf is")
        print("policy-denied so its txn ROLLED_BACK with the effect never APPLIED.")
    finally:
        audit.close()


def main() -> int:
    print("=" * 72)
    print("Pherix coding-CLI gateway — IN-PROCESS GOVERNED-PATH CAPTURE (no key)")
    print("=" * 72)
    print("NOT a real CLI: this drives the gateway's MCP surface directly to prove")
    print("the governed path. The live aider/Claude-Code run is in FINDINGS.md.")
    print()

    fd, audit_path = tempfile.mkstemp(suffix=".audit.db", prefix="coding_cli_")
    os.close(fd)
    try:
        with scratch_repo({"app.py": "def main():\n    print('v1')\n"}) as repo_root:
            txn_ids = _run_capture(audit_path, repo_root)
            _print_journal(audit_path, txn_ids)
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.unlink(audit_path + suffix)
            except FileNotFoundError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
