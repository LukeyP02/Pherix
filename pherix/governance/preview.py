"""The policy explainer — a dry-run of a candidate policy's verdicts.

Given a spec and a sample journal (a list of :class:`EffectScenario`), show what
the policy would **allow / deny / gate / cap** — *before* it governs a real
agent. This is a traversal of a journal against a candidate policy, exactly the
shape :func:`pherix.dry_run` runs; we reuse the engine's own
:meth:`Policy.collect_verdicts` so the preview cannot diverge from runtime
behaviour by construction.

Two things the preview adds on top of the raw verdicts:

- **World-state for templates.** A rule like ``refund_if_paid`` calls
  ``ctx.read(resource, key)``. The preview supplies a dict-backed
  :data:`~pherix.core.policy.ReadMediator` from a sample ``world`` map, so a rule
  can be previewed deterministically and offline. A key that's absent reads
  ``None`` — the same answer the live SQL reader gives for an absent row.
- **Disposition.** The runtime distinguishes a policy *denial* from a *gate*
  (an irreversible effect that blocks at commit for human approval). A gate is
  not a policy verdict — it is a property of the effect (irreversible + no
  compensator). The preview folds both into a single per-effect badge:
  ``deny`` > ``cap`` > ``gate`` > ``allow``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from pherix.core.effects import Effect
from pherix.core.policy import Policy, PolicyContext, PolicyVerdict
from pherix.governance.spec import PolicySpec, from_spec

Disposition = Literal["allow", "deny", "cap", "gate"]


@dataclass
class EffectScenario:
    """One sample tool call to preview the policy against.

    The serialisable cousin of an :class:`Effect` — just the fields a policy
    reads. ``reversible`` + ``compensator`` drive the gate disposition; the rest
    feed the rules and caps.
    """

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    resource: str = "sql"
    reversible: bool = True
    compensator: str | None = None


@dataclass
class EffectVerdict:
    """The preview's per-effect summary: one badge + the reasons behind it."""

    index: int
    tool: str
    disposition: Disposition
    reasons: list[str] = field(default_factory=list)
    # Every raw verdict the engine produced for this effect, for the detail view.
    verdicts: list[PolicyVerdict] = field(default_factory=list)


@dataclass
class PreviewResult:
    """What the UI renders: a row per effect, plus the clean/aggregate signals."""

    rows: list[EffectVerdict]
    verdicts: list[PolicyVerdict]
    is_clean: bool

    @property
    def counts(self) -> dict[str, int]:
        out = {"allow": 0, "deny": 0, "cap": 0, "gate": 0}
        for r in self.rows:
            out[r.disposition] += 1
        return out


class _Journal:
    """Minimal stand-in for a Transaction — ``collect_verdicts`` only reads
    ``.effects``. A real txn would also carry adapters and state; the preview
    needs none of that, the journal alone answers the policy question."""

    def __init__(self, effects: list[Effect]) -> None:
        self.effects = effects


def _canon(resource: str, key: Any) -> str:
    """Canonical string key for the world map — must match ``JSON.stringify``
    in the JS mirror, so: a list (not tuple), no whitespace."""
    if isinstance(key, (list, tuple)):
        key = list(key)
    return json.dumps([resource, key], separators=(",", ":"))


def _world_reader(world: list[dict] | None):
    """Build a :data:`ReadMediator` over a sample world map.

    ``world`` is a list of ``{"resource", "key", "value"}`` entries (the shape the
    UI authors). An unmatched read returns ``None`` — identical to the live SQL
    reader's answer for an absent row, so a ``refund_if_paid`` rule denies a
    phantom order rather than crashing.
    """
    table: dict[str, Any] = {}
    for entry in world or []:
        table[_canon(entry["resource"], entry["key"])] = entry["value"]

    def _read(resource: str, key: Any) -> Any:
        return table.get(_canon(resource, key))

    return _read


def preview(
    spec: PolicySpec | dict[str, Any] | Policy,
    scenario: list[EffectScenario | dict[str, Any]],
    *,
    world: list[dict] | None = None,
    gate_irreversibles: bool | None = None,
) -> PreviewResult:
    """Fold ``spec`` over ``scenario`` and return the verdicts the engine gives.

    ``spec`` may be a :class:`PolicySpec`, its JSON dict, or an already-built
    :class:`Policy` (the conformance test passes the live policy directly). The
    journal is built in order; ``collect_verdicts`` runs the commit-time walk
    (caps re-accumulate from zero over the ordered journal), which is the
    canonical "what would this policy do to this whole sequence" question.
    """
    if isinstance(spec, Policy):
        policy = spec
        gate_default = True
    else:
        if isinstance(spec, dict):
            spec = PolicySpec.from_dict(spec)
        policy = from_spec(spec)
        gate_default = spec.gate_irreversibles
    gate = gate_default if gate_irreversibles is None else gate_irreversibles

    scen = [
        s if isinstance(s, EffectScenario) else EffectScenario(**s)
        for s in scenario
    ]

    effects = [
        Effect(
            txn_id="preview",
            index=i,
            tool=s.tool,
            args=s.args,
            resource=s.resource,
            reversible=s.reversible,
            compensator=s.compensator,
        )
        for i, s in enumerate(scen)
    ]

    ctx = PolicyContext(
        journal=effects, where="stage", reader=_world_reader(world)
    )
    verdicts = policy.collect_verdicts(_Journal(effects), ctx)

    by_index: dict[int, list[PolicyVerdict]] = {i: [] for i in range(len(effects))}
    for v in verdicts:
        by_index[v.effect_index].append(v)

    rows: list[EffectVerdict] = []
    for i, s in enumerate(scen):
        vs = by_index[i]
        denies = [v for v in vs if not v.allow]
        non_cap = [v for v in denies if not _is_cap(v.rule)]
        caps = [v for v in denies if _is_cap(v.rule)]

        if non_cap:
            disposition: Disposition = "deny"
            reasons = [v.reason or "denied" for v in non_cap]
        elif caps:
            disposition = "cap"
            reasons = [v.reason or "cap exceeded" for v in caps]
        elif gate and not s.reversible and s.compensator is None:
            disposition = "gate"
            reasons = [
                "irreversible effect with no compensator — blocks at commit "
                "for human approval"
            ]
        else:
            disposition = "allow"
            reasons = []

        rows.append(
            EffectVerdict(
                index=i,
                tool=s.tool,
                disposition=disposition,
                reasons=reasons,
                verdicts=vs,
            )
        )

    return PreviewResult(
        rows=rows,
        verdicts=verdicts,
        is_clean=all(v.allow for v in verdicts),
    )


def _is_cap(rule: Any) -> bool:
    """A cap (``_CountCap`` / ``_SumCap``) carries ``contribution``; a
    ``PolicyRule`` does not. That structural tell separates a cap denial from a
    rule denial without importing the private cap classes."""
    return rule is not None and hasattr(rule, "contribution")


__all__ = [
    "Disposition",
    "EffectScenario",
    "EffectVerdict",
    "PreviewResult",
    "preview",
]
