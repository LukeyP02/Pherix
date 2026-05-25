"""Capture the air-gapped run as demo evidence — and *verify* the perimeter held.

    LOCAL_MODEL_URL=http://localhost:11434/v1 LOCAL_MODEL=llama3.1:8b \
    python -m examples.local_airgap.capture_airgap

The sovereignty claim — "the regulated data never leaves the perimeter, the model
is local, Pherix governs it offline" — is worthless if it is merely *asserted*.
So this capture **measures** it: it wraps the whole governed run in an
:class:`EgressGuard` that records every TCP peer any socket in the process
connects to, then asserts that not one of them is a public-internet address. A
stray call to ``api.anthropic.com`` / ``api.openai.com`` would land a public IP in
the recorded set and **fail the capture** — the claim cannot pass by accident.

What it captures:
  * the local model id and endpoint (what brain ran, where);
  * the egress proof — every peer the run touched, and that all are
    loopback / private (the perimeter held; no data left the building);
  * the agent's actions — the tool calls it actually made;
  * the Pherix journal — surfaced both inline and via the governance console
    (``python -m pherix.inspector --db <path>``).

Offline (the mock-client path the tests drive), no socket opens at all, so the
guard records an empty set — zero egress is trivially inside the perimeter. The
real enforcement bites on the live box, where loopback connections to the local
server are the *only* peers allowed.
"""

from __future__ import annotations

import functools
import ipaddress
import json
import socket
import sys
from dataclasses import dataclass, field
from typing import Any

from pherix.core.audit import AuditJournal

from examples.dogfood.capture import (
    inspector_hint,
    journal_path_for,
    journal_summary,
    verdict_for,
)
from examples.local_airgap.run_local import (
    LocalConfig,
    LocalRunResult,
    endpoint_reachable,
    resolve_config,
    run_local,
)


class EgressViolation(AssertionError):
    """Raised when the run connected to an address outside the perimeter.

    The exception *is* the failure of the sovereignty claim: a public-internet
    peer was contacted during a run that promised to stay local. It carries the
    offending peers so the operator sees exactly what leaked.
    """


# --- the egress guard — the verified half of the sovereignty claim ----------


class EgressGuard:
    """Record every TCP peer the process connects to while active; judge locality.

    Implemented by wrapping ``socket.socket.connect`` / ``connect_ex`` for the
    duration of the ``with`` block — the single chokepoint every outbound TCP
    connection passes through, including the ones the ``openai`` SDK's HTTP stack
    opens. By the time ``connect`` is called the hostname is already resolved to a
    numeric address, so what we record is the real peer IP, not a name that could
    lie. Unix-domain sockets (a string address) are local IPC and recorded as
    such, never as egress.

    Locality is defined as the perimeter a regulated buyer means: loopback
    (127/8, ::1), RFC1918 / unique-local private ranges, and link-local. Anything
    globally routable is egress — the run reached the public internet. The guard
    is a *measurement*, not a network block: it does not stop a connection, it
    records it, so the assertion afterwards is honest about what actually
    happened rather than what was permitted.
    """

    def __init__(self) -> None:
        self.peers: list[tuple[str, int]] = []
        self._orig_connect: Any = None
        self._orig_connect_ex: Any = None

    @staticmethod
    def is_local_ip(host: str) -> bool:
        """Is ``host`` (a numeric peer address) inside the perimeter?

        ``True`` for loopback / private / link-local addresses; ``False`` for a
        globally routable one. A value that does not parse as an IP is treated as
        *not* local except the literal name ``localhost`` — at ``connect`` time the
        address is numeric, so a non-parsing value is anomalous and judged
        conservatively (it is egress unless proven otherwise).
        """
        if host == "localhost":
            return True
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        return ip.is_loopback or ip.is_private or ip.is_link_local

    def _record(self, address: Any) -> None:
        # AF_INET: (host, port); AF_INET6: (host, port, flowinfo, scopeid);
        # AF_UNIX: a str/bytes path — local IPC, recorded with port -1.
        if isinstance(address, (str, bytes)):
            self.peers.append(("<unix>", -1))
            return
        if isinstance(address, tuple) and len(address) >= 2:
            self.peers.append((str(address[0]), int(address[1])))

    def __enter__(self) -> "EgressGuard":
        self._orig_connect = socket.socket.connect
        self._orig_connect_ex = socket.socket.connect_ex
        guard = self

        @functools.wraps(self._orig_connect)
        def _connect(sock_self, address, *a, **kw):
            guard._record(address)
            return guard._orig_connect(sock_self, address, *a, **kw)

        @functools.wraps(self._orig_connect_ex)
        def _connect_ex(sock_self, address, *a, **kw):
            guard._record(address)
            return guard._orig_connect_ex(sock_self, address, *a, **kw)

        socket.socket.connect = _connect  # type: ignore[method-assign]
        socket.socket.connect_ex = _connect_ex  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc: Any) -> None:
        socket.socket.connect = self._orig_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = self._orig_connect_ex  # type: ignore[method-assign]

    def public_peers(self) -> list[tuple[str, int]]:
        """The recorded peers that are *outside* the perimeter (the leak set)."""
        out: list[tuple[str, int]] = []
        seen: set[tuple[str, int]] = set()
        for host, port in self.peers:
            if host == "<unix>":
                continue
            if not self.is_local_ip(host) and (host, port) not in seen:
                seen.add((host, port))
                out.append((host, port))
        return out

    def assert_local_only(self) -> None:
        """Raise :class:`EgressViolation` if the run reached the public internet.

        This is the line that turns "the data stayed inside" from a slogan into a
        checked fact. An empty peer set (the offline / mock path) passes trivially
        — nothing left because nothing connected.
        """
        leaks = self.public_peers()
        if leaks:
            rendered = ", ".join(f"{h}:{p}" for h, p in leaks)
            raise EgressViolation(
                "EGRESS DETECTED — the air-gapped run connected to a public "
                f"address: {rendered}. The sovereignty claim is FALSE for this "
                "run: regulated data may have left the perimeter."
            )


# --- evidence ----------------------------------------------------------------


@dataclass
class CaptureEvidence:
    """The demo artifact: what ran, that nothing leaked, and what Pherix did."""

    endpoint: str
    model: str
    fixture: str
    verdict: str
    harmed: bool
    no_public_egress: bool
    peers: list[str]
    public_peers: list[str]
    agent_actions: list[dict]
    journal: list[dict]
    proof: dict = field(default_factory=dict)
    audit_path: str | None = None

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, default=str)


def _agent_actions(result: LocalRunResult) -> list[dict]:
    """The tool calls the model actually emitted, read off the journal.

    The journal is backend-agnostic (it is the same whether a local or cloud
    model produced the calls), so this works for the OpenAI-compatible path
    without parsing the wire transcript.
    """
    return [
        {"index": e["index"], "tool": e["tool"], "args": e["args"]}
        for e in journal_summary(result.run)
    ]


def capture(
    config: LocalConfig,
    *,
    client: Any = None,
    audit_path: str | None = None,
) -> CaptureEvidence:
    """Run the agent under the egress guard, assert the perimeter held, build evidence.

    ``client`` is injected by the tests (a mock OpenAI-compatible client) so the
    capture — guard included — runs offline. ``audit_path`` persists the journal
    to disk so the governance console can open it; ``None`` keeps it in memory
    (the test path, where nothing is surfaced).

    Order matters: the guard is entered *before* the run and the assertion is made
    *after*, so every socket the run opens — the harness's reachability probe, the
    SDK's HTTP connections — is inside the measured window.
    """
    audit = AuditJournal(audit_path) if audit_path else AuditJournal.in_memory()
    with EgressGuard() as guard:
        result = run_local(config, client=client, audit=audit)
    guard.assert_local_only()  # raises EgressViolation if anything left the perimeter

    peers = sorted({f"{h}:{p}" for h, p in guard.peers})
    leaks = sorted({f"{h}:{p}" for h, p in guard.public_peers()})
    return CaptureEvidence(
        endpoint=config.base_url,
        model=config.model,
        fixture=config.fixture,
        verdict=verdict_for(result.run),
        harmed=result.harmed,
        no_public_egress=not leaks,
        peers=peers,
        public_peers=leaks,
        agent_actions=_agent_actions(result),
        journal=journal_summary(result.run),
        proof=result.proof,
        audit_path=audit_path,
    )


# --- CLI ---------------------------------------------------------------------


def render(ev: CaptureEvidence) -> str:
    lines = [
        "AIR-GAPPED RUN — CAPTURED EVIDENCE",
        f"  model    : {ev.model}  (open weights — the brain never left the box)",
        f"  endpoint : {ev.endpoint}  (local)",
        f"  fixture  : {ev.fixture}",
        f"  verdict  : {ev.verdict}    harm: {'HARMED' if ev.harmed else 'no harm'}",
        "",
        "  SOVEREIGNTY CHECK (measured, not asserted):",
        f"    peers contacted : {ev.peers or '(none — no socket opened)'}",
        f"    public egress   : {ev.public_peers or 'NONE'}",
        f"    verdict         : {'PERIMETER HELD — no data left' if ev.no_public_egress else 'LEAK'}",
        "",
        "  AGENT ACTIONS (what the local model did):",
    ]
    for a in ev.agent_actions or []:
        lines.append(f"    [{a['index']}] {a['tool']} {a['args']}")
    if not ev.agent_actions:
        lines.append("    (none)")
    lines.append("")
    lines.append("  PHERIX JOURNAL:")
    for e in ev.journal or []:
        lines.append(
            f"    [{e['index']}] {e['tool']} on {e['resource']} -> {e['status']}"
        )
    if ev.audit_path:
        lines.append(inspector_hint(ev.audit_path))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    fixture = argv[0] if argv else None

    config = resolve_config(fixture=fixture)
    if config is None:
        print(
            "SKIPPED — no local model endpoint (LOCAL_MODEL_URL unset).\n"
            "  Set it to an OpenAI-compatible local server and re-run, e.g.\n"
            "    LOCAL_MODEL_URL=http://localhost:11434/v1 LOCAL_MODEL=llama3.1:8b \\\n"
            "      python -m examples.local_airgap.capture_airgap"
        )
        return 0
    if not endpoint_reachable(config.base_url):
        print(f"SKIPPED — {config.base_url} is configured but not reachable.")
        return 0

    # One path; derive the evidence-JSON sibling from it. Calling
    # ``journal_path_for`` again would unlink the journal we just wrote.
    journal = journal_path_for(f"airgap-{config.fixture}")
    ev = capture(config, audit_path=str(journal))
    print(render(ev))
    out = journal.with_suffix(".evidence.json")
    out.write_text(ev.to_json())
    print(f"\nEvidence written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
