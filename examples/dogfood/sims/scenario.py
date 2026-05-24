"""The sim framework: a ``Scenario``, the two-arm runner, and the report.

THE UNBIASED CONTRACT — every scenario in this package must honour all five:

  1. **Neutral prompt.** The agent is given a realistic *job* and the rules a
     real operator in that role would already have — never the crime. We never
     say "overpay", "drop the table", "push to main". The prompt is surfaced in
     the evidence so a viewer sees exactly what it was told.

  2. **Realistic, not rigged, data.** The seed workload *contains* hard/edge
     cases at a realistic frequency (a claim over its limit, an ambiguous bulk
     update). We never tell the agent the answer, and the forbidden path is
     never the *only* path to success. The error must be a genuine mistake.

  3. **The harm oracle is independent of the policy.** ``harm_oracle(conn)``
     measures an *objective fact about the end-state* — read straight off the
     resource (a payout above the limit, an unbalanced ledger, rows touched
     beyond the intended scope) — and is defined WITHOUT reference to the
     policy. The whole experiment's validity rests on ``harm ⟂ policy``: if harm
     were defined as "the policy fired", the result would be circular and prove
     nothing. The *same* oracle judges both arms.

  4. **Matched arms.** Ungoverned vs governed differ by one bit — Pherix in the
     path or not. Same model, prompt, seed schema, run count. The ungoverned arm
     *actually executes* the harm into a disposable scratch DB, so we OBSERVE the
     natural rate rather than hypothesise it.

  5. **Report everything, including escapes.** Every run is counted. A governed
     run whose end-state is *still* harmed is an **escape** — a policy gap — and
     it is reported, with its proof, not hidden. A suite that shows 0 escapes
     everywhere is suspicious; a credible one surfaces its own gaps.

A scenario is a plain dataclass of (neutral prompt, seed schema, tools, policy,
harm oracle). Adding one is adding a module that exposes ``SCENARIO`` — the
runner discovers it automatically (see :func:`all_scenarios`), so scenarios can
be built in parallel with no shared-file merge conflict.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from pherix.core.audit import AuditJournal
from pherix.core.policy import Policy
from pherix.core.tools import REGISTRY

from examples.dogfood.capture import count_gated_calls, verdict_for
from examples.dogfood.harness import run_agent

HarmOracle = Callable[[Any], "tuple[bool, dict]"]


@dataclass
class ResourceBundle:
    """One run's freshly-provisioned resource(s), wired for both arms.

    A scenario's ``setup()`` context manager stands up its *real* backend(s) — a
    scratch SQLite file, a throwaway git repo, an in-memory store — and yields
    this bundle, then tears everything down on exit. The runner picks the field
    it needs per arm:

      * ``adapters`` (``resource -> ResourceAdapter``) is the **governed** arm's
        wiring: each adapter snapshots/applies/restores its class of resource
        through Pherix. A scenario may bind more than one (e.g. ``git`` + ``fs``).
      * ``handles`` (``resource -> handle``) is the **ungoverned "before"** arm's
        wiring: the harness fires each call straight at the handle so the effect
        persists with no journal and no policy (rule 4). The shape matches what
        the ``@tool`` wrapper would inject inside ``agent_txn`` — a live SQLite
        connection for ``sql``, a :class:`pherix.core.adapters.git.GitHandle` for
        ``git``, an ``UngovernedFsHandle`` for ``fs``, ``None`` for an
        injection-free tool.
      * ``probe`` is the live object ``build_policy`` and ``harm_oracle`` read to
        consult / judge the system of record — a ``conn`` for a SQL scenario, a
        repo root :class:`~pathlib.Path` for git+fs, the store for memory. The
        *same* probe feeds both, so the oracle judges the identical end-state in
        both arms (rule 3).

    Both arms wrap the **same** underlying resource the ``probe`` reads — the
    adapter and the handle are two views onto one fresh backend, so the oracle's
    post-run read is honest for whichever arm ran.
    """

    adapters: dict[str, Any]
    handles: dict[str, Any]
    probe: Any


@dataclass
class Scenario:
    """One domain scenario — the four axes pointed at a single realistic task.

    ``setup`` is a zero-arg callable returning a context manager that provisions
    a *fresh* real resource per run and yields a :class:`ResourceBundle` (rule 2:
    its seed carries the edge cases). ``system`` / ``task`` are the neutral prompt
    (rule 1). ``build_tools`` returns the agent's domain tools (``@tool``-
    decorated, registered fresh on each call — so the runner can
    ``REGISTRY.clear()`` between runs). ``build_policy(probe)`` returns the
    operator's guardrails, closed over the live resource so a rule can consult the
    system of record (world-state checks). ``harm_oracle(probe)`` is the
    independent end-state judge (rule 3): ``(harmed, proof)``.

    ``provider`` (``"anthropic"`` | ``"openai"``) and ``model`` name the backend
    this scenario runs on, so a mixed fleet (some Claude, some GPT) runs in one
    pass — the runner threads them into ``run_agent`` unless an explicit override
    is passed. ``model=None`` lets the harness pick its per-backend default.
    """

    name: str
    query: str  # plain-English description of what the harm oracle checks
    setup: Callable[[], AbstractContextManager[ResourceBundle]]
    system: str
    task: str
    build_tools: Callable[[], list[Callable[..., Any]]]
    build_policy: Callable[[Any], Policy]
    harm_oracle: HarmOracle
    provider: str = "anthropic"
    model: str | None = None


# --- per-run / per-arm / per-scenario results ------------------------------


@dataclass
class RunOutcome:
    """One run's result, judged by the independent oracle.

    ``harmed`` is the oracle's verdict on the *real end-state*; ``proof`` is the
    facts it read. ``verdict`` is Pherix's terminal verdict (governed arm only).
    ``boundary_pushes`` counts how hard the agent pressed the guardrail on the
    governed arm (policy denials fed back + effects gated at commit) — evidence
    the agent genuinely *tried* the unsafe action and was contained, not that it
    simply behaved.
    """

    governed: bool
    harmed: bool
    proof: dict
    verdict: str | None
    boundary_pushes: int
    error: str | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ArmSummary:
    """The aggregate over one arm (all ungoverned runs, or all governed runs)."""

    governed: bool
    runs: int
    harmed: int
    boundary_pushes: int
    outcomes: list[RunOutcome] = field(default_factory=list)

    @property
    def harm_rate(self) -> float:
        return self.harmed / self.runs if self.runs else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["harm_rate"] = self.harm_rate
        return d


@dataclass
class ScenarioResult:
    """Both arms of one scenario — the natural disaster rate vs the residual."""

    name: str
    query: str
    ungoverned: ArmSummary
    governed: ArmSummary

    @property
    def prevented(self) -> int:
        """Disasters Pherix prevented (rate delta × N, floored at 0)."""
        return max(self.ungoverned.harmed - self.governed.harmed, 0)

    @property
    def escaped(self) -> int:
        """Harm that landed *despite* Pherix — policy gaps. The honesty signal."""
        return self.governed.harmed

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "query": self.query,
            "ungoverned": self.ungoverned.to_dict(),
            "governed": self.governed.to_dict(),
            "prevented": self.prevented,
            "escaped": self.escaped,
        }


# --- the runner -------------------------------------------------------------


def _resolve_backend(
    scn: Scenario,
    *,
    api: str | None,
    base_url: str | None,
    model: str | None,
) -> tuple[str, str | None, str | None]:
    """Pick the (api, base_url, model) for a run: explicit override else scenario.

    A scenario names its own backend (``scn.provider`` / ``scn.model``) so a
    mixed Claude+GPT fleet runs in one pass. An explicit override (the CLI's
    ``--openai`` / ``--model``) still wins when supplied. Cloud GPT needs the
    OpenAI base URL; the Anthropic path takes none.
    """
    resolved_api = api or scn.provider
    resolved_model = model if model is not None else scn.model
    if base_url is not None:
        resolved_base = base_url
    elif resolved_api == "openai":
        resolved_base = "https://api.openai.com/v1"
    else:
        resolved_base = None
    return resolved_api, resolved_base, resolved_model


def run_arm(
    scn: Scenario,
    *,
    governed: bool,
    runs: int,
    model: str | None = None,
    api: str | None = None,
    base_url: str | None = None,
    client_factory: Callable[[int], Any] | None = None,
    audit_path: str | None = None,
) -> ArmSummary:
    """Run one arm of a scenario ``runs`` times and judge each by the oracle.

    Each run calls ``scn.setup()`` for a *fresh* real resource (a scratch DB, a
    throwaway repo, an in-memory store) and a fresh tool registry. The governed
    arm wires ``bundle.adapters`` and installs ``scn.build_policy(bundle.probe)``;
    the ungoverned arm passes no adapter/policy and hands ``bundle.handles`` in so
    each call fires straight at the resource and persists (rule 4). After the run
    resolves, ``scn.harm_oracle(bundle.probe)`` reads the real end-state — the
    *same* judge in both arms (rule 3). The backend (api / model) is the
    scenario's own unless overridden.
    """
    res_api, res_base, res_model = _resolve_backend(
        scn, api=api, base_url=base_url, model=model
    )
    outcomes: list[RunOutcome] = []
    for i in range(runs):
        REGISTRY.clear()
        client = client_factory(i) if client_factory else None
        audit = AuditJournal(audit_path) if audit_path else AuditJournal.in_memory()
        with scn.setup() as bundle:
            tools = scn.build_tools()
            common = dict(
                task=scn.task,
                system=scn.system,
                tools=tools,
                client_id=f"{scn.name}-{'gov' if governed else 'ung'}-{i}",
                client=client,
                audit=audit,
                api=res_api,
                base_url=res_base,
                **({"model": res_model} if res_model is not None else {}),
            )
            if governed:
                run = run_agent(
                    adapters=bundle.adapters,
                    policy=scn.build_policy(bundle.probe),
                    **common,
                )
            else:
                run = run_agent(
                    adapters={},
                    governed=False,
                    handles=bundle.handles,
                    **common,
                )
            harmed, proof = scn.harm_oracle(bundle.probe)
            outcomes.append(
                RunOutcome(
                    governed=governed,
                    harmed=harmed,
                    proof=proof,
                    verdict=verdict_for(run) if governed else None,
                    boundary_pushes=_boundary_pushes(run) if governed else 0,
                    error=str(run.error) if run.error else None,
                )
            )
    return ArmSummary(
        governed=governed,
        runs=runs,
        harmed=sum(1 for o in outcomes if o.harmed),
        boundary_pushes=sum(o.boundary_pushes for o in outcomes),
        outcomes=outcomes,
    )


def _boundary_pushes(run: Any) -> int:
    """Policy denials fed back to the model + effects gated at commit."""
    gated = sum(
        1 for e in run.journal if getattr(e.status, "name", str(e.status)) == "GATED"
    )
    return count_gated_calls(run) + gated


def run_scenario(
    scn: Scenario,
    *,
    runs: int,
    model: str | None = None,
    api: str | None = None,
    base_url: str | None = None,
    client_factory: Callable[[int], Any] | None = None,
) -> ScenarioResult:
    """Run both arms of ``scn`` at ``runs`` each — the matched before/after.

    ``api`` / ``model`` default to the scenario's own backend (so a mixed fleet
    runs in one pass); an explicit value overrides for cross-model sweeps.
    """
    ungoverned = run_arm(
        scn,
        governed=False,
        runs=runs,
        model=model,
        api=api,
        base_url=base_url,
        client_factory=client_factory,
    )
    governed = run_arm(
        scn,
        governed=True,
        runs=runs,
        model=model,
        api=api,
        base_url=base_url,
        client_factory=client_factory,
    )
    return ScenarioResult(
        name=scn.name, query=scn.query, ungoverned=ungoverned, governed=governed
    )


# --- scenario discovery (no central registry to merge-conflict on) ----------


def all_scenarios() -> dict[str, Scenario]:
    """Discover every scenario module in this package that exposes ``SCENARIO``.

    Adding a scenario is adding a ``sims/<domain>.py`` that defines a
    module-level ``SCENARIO = Scenario(...)`` — no edit to any shared file, so
    scenarios can be built in parallel. ``scenario`` / ``__main__`` are skipped.
    """
    import examples.dogfood.sims as pkg

    found: dict[str, Scenario] = {}
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name in ("scenario", "__main__"):
            continue
        mod = importlib.import_module(f"{pkg.__name__}.{info.name}")
        scn = getattr(mod, "SCENARIO", None)
        if isinstance(scn, Scenario):
            found[scn.name] = scn
    return dict(sorted(found.items()))


# --- rendering --------------------------------------------------------------


def _example_proof(arm: ArmSummary) -> dict | None:
    """The first harmed run's proof in an arm — for traceability in the report."""
    for o in arm.outcomes:
        if o.harmed:
            return o.proof
    return None


def render_scenario(res: ScenarioResult) -> str:
    u, g = res.ungoverned, res.governed
    gap = "  ⚠ POLICY GAP — proof below" if res.escaped else ""
    lines = [
        "=" * 72,
        f"SCENARIO — {res.name}   ({u.runs} runs/arm)",
        f"  oracle: {res.query}",
        "=" * 72,
        "  ┌─ HEADLINE ───────────────────────────────────────────────┐",
        f"  │  WITHOUT Pherix : {u.harm_rate:>5.1%}  "
        f"({u.harmed}/{u.runs} runs ended in REAL harm)",
        f"  │  WITH Pherix    : {g.harm_rate:>5.1%}  "
        f"({g.harmed}/{g.runs} runs ended in real harm)",
        "  └───────────────────────────────────────────────────────────┘",
        f"  disasters prevented : {res.prevented}   "
        f"(rate {u.harm_rate - g.harm_rate:+.1%})",
        f"  escaped (policy gap): {res.escaped}{gap}",
        f"  agent pushed the boundary (governed): {g.boundary_pushes} "
        f"denied/gated call(s) across {g.runs} runs",
    ]
    up = _example_proof(u)
    if up:
        lines.append(f"  example ungoverned harm: {json.dumps(up, default=str)}")
    gp = _example_proof(g)
    if gp:
        lines.append(f"  ESCAPED harm (governed): {json.dumps(gp, default=str)}")
    lines.append("")
    return "\n".join(lines)


def render_grand_total(results: list[ScenarioResult]) -> str:
    u_runs = sum(r.ungoverned.runs for r in results)
    g_runs = sum(r.governed.runs for r in results)
    u_harm = sum(r.ungoverned.harmed for r in results)
    g_harm = sum(r.governed.harmed for r in results)
    prevented = sum(r.prevented for r in results)
    escaped = sum(r.escaped for r in results)
    u_rate = u_harm / u_runs if u_runs else 0.0
    g_rate = g_harm / g_runs if g_runs else 0.0
    return "\n".join(
        [
            "╔══════════════ GRAND TOTAL — all scenarios ══════════════╗",
            f"   scenarios       : {len(results)}   "
            f"({u_runs} ungoverned + {g_runs} governed runs)",
            f"   WITHOUT Pherix  : {u_rate:>5.1%}  "
            f"({u_harm}/{u_runs} runs ended in real harm)",
            f"   WITH Pherix     : {g_rate:>5.1%}  "
            f"({g_harm}/{g_runs} runs ended in real harm)",
            f"   disasters prevented : {prevented}",
            f"   escaped (policy gaps): {escaped}",
            "╚══════════════════════════════════════════════════════════╝",
        ]
    )


def write_scenario(res: ScenarioResult, out_dir: Any) -> Any:
    """Write the full per-run JSON for one scenario; return the summary path."""
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{res.name}_sim.json"
    path.write_text(json.dumps(res.to_dict(), indent=2, default=str))
    return path
