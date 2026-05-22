"""The serialisable policy spec + its round-trip into a real ``Policy``.

A :class:`Policy` is a bundle of *live Python callables* — caps carry a ``via``
lambda, rules are closures over ``ctx.read``. That is perfect for the engine and
useless for a UI: closures can't be saved to JSON, diffed in a PR, or rebuilt in
a browser. :class:`PolicySpec` is the serialisable shadow of that bundle. The map
between them:

    PolicySpec  ──from_spec──▶  Policy        (load — what the engine runs)
    PolicySpec  ──to_dict────▶  JSON          (persist / ship / load into the UI)
    PolicySpec  ──to_python──▶  .py source    (the "give me runnable Python" export)

The spec covers the *base* every buyer assumes works: allow/deny tool lists,
``Cap.count`` / ``Cap.sum`` caps (with a serialisable ``field`` extractor in place
of the raw ``via`` callable), the human gate for irreversibles, and a small
catalog of **named** world-state rule templates (see
:mod:`pherix.governance.templates`). Arbitrary hand-written ``@policy.rule``
closures are deliberately *not* serialisable — a buyer who needs the edge writes
Python; the UI covers the base.

Round-trip guarantee (pinned by ``tests/test_governance_spec.py``): for any spec
built from supported primitives,

    from_dict(to_dict(spec)) == spec                     # JSON round-trips
    from_spec(spec)          runs in the engine unchanged # load round-trips
    exec(to_python(spec))    builds a verdict-equivalent Policy
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from pherix.core.policy import Cap, Policy

# A cap is one of these two shapes; ``field`` (sum only) names the arg whose
# numeric value accumulates — the serialisable stand-in for ``Cap.sum``'s ``via``
# callable.
CapKind = Literal["count", "sum"]


@dataclass
class CapSpec:
    """A serialisable spend cap.

    ``kind="count"`` caps the number of times ``tool`` fires within a txn;
    ``kind="sum"`` caps the cumulative ``args[field]`` numeric contribution.
    A ``sum`` cap with a missing/blank field on a given effect contributes
    ``0`` (the cap fails *open* for that effect rather than crashing) — the
    JS mirror does the same so the preview matches.
    """

    kind: CapKind
    tool: str
    max: float | int
    field: str | None = None  # required for kind="sum", ignored for "count"

    def __post_init__(self) -> None:
        if self.kind == "sum" and not self.field:
            raise ValueError("a sum cap requires a 'field' (the arg to sum)")

    def to_cap(self) -> Any:
        if self.kind == "count":
            return Cap.count(tool=self.tool, max=int(self.max))
        fld = self.field
        return Cap.sum(
            tool=self.tool,
            via=lambda a, f=fld: float(a.get(f, 0) or 0),
            max=self.max,
        )


@dataclass
class RuleSpec:
    """A serialisable reference to a named rule template + its params.

    ``template`` keys into :data:`pherix.governance.templates.TEMPLATE_REGISTRY`;
    ``params`` are the keyword args that template factory takes. Because the
    template is named (not an inline closure), the rule round-trips through JSON
    and the JS mirror can reproduce its verdict.
    """

    template: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class PolicySpec:
    """The whole governable policy, as data.

    Maps onto :meth:`Policy.with_rules` field-for-field, with two additions the
    ``Policy`` dataclass itself doesn't carry:

    - :attr:`gate_irreversibles` — the human gate. This is a *runtime* behaviour
      (the staged/gated lane keys off ``adapter.supports_rollback()`` + whether a
      compensator is registered), not a ``Policy`` field, so ``from_spec`` does
      not encode it into the returned ``Policy``. The preview surfaces it: an
      irreversible effect with no compensator shows ``gate`` when this is on.
    - :attr:`name` / :attr:`description` — UI / catalog metadata.
    """

    name: str = "untitled-policy"
    description: str = ""
    allow: list[str] | None = None
    deny: list[str] = field(default_factory=list)
    caps: list[CapSpec] = field(default_factory=list)
    rules: list[RuleSpec] = field(default_factory=list)
    gate_irreversibles: bool = True

    def __post_init__(self) -> None:
        # Coerce raw dicts into the typed sub-specs so a caller (the UI, a test)
        # can construct a PolicySpec from plain JSON-shaped dicts directly.
        self.caps = [c if isinstance(c, CapSpec) else CapSpec(**c) for c in self.caps]
        self.rules = [
            r if isinstance(r, RuleSpec) else RuleSpec(**r) for r in self.rules
        ]

    # -- serialisation ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Plain-JSON form — what the UI downloads and what the engine loads."""
        return {
            "name": self.name,
            "description": self.description,
            "allow": list(self.allow) if self.allow is not None else None,
            "deny": list(self.deny),
            "caps": [asdict(c) for c in self.caps],
            "rules": [asdict(r) for r in self.rules],
            "gate_irreversibles": self.gate_irreversibles,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PolicySpec":
        return cls(
            name=d.get("name", "untitled-policy"),
            description=d.get("description", ""),
            allow=(list(d["allow"]) if d.get("allow") is not None else None),
            deny=list(d.get("deny", [])),
            caps=[CapSpec(**c) for c in d.get("caps", [])],
            rules=[RuleSpec(**r) for r in d.get("rules", [])],
            gate_irreversibles=bool(d.get("gate_irreversibles", True)),
        )


# -- the load side -----------------------------------------------------------


def to_spec(d: dict[str, Any]) -> PolicySpec:
    """Parse a JSON dict into a :class:`PolicySpec` (alias of ``from_dict``)."""
    return PolicySpec.from_dict(d)


def from_spec(spec: PolicySpec | dict[str, Any]) -> Policy:
    """Build a runnable :class:`Policy` from a spec — the load round-trip.

    Caps become real :class:`Cap` instances; rule templates are looked up in
    :data:`TEMPLATE_REGISTRY` and instantiated with their params. The returned
    ``Policy`` is exactly what the runtime evaluates — there is no governance
    shim in the hot path, the spec just *constructs* a normal ``Policy``.
    """
    # Local import breaks the spec ↔ templates cycle (templates imports the
    # spec dataclasses for its STARTER_TEMPLATES at module load).
    from pherix.governance.templates import TEMPLATE_REGISTRY

    if isinstance(spec, dict):
        spec = PolicySpec.from_dict(spec)

    rules = []
    for r in spec.rules:
        factory = TEMPLATE_REGISTRY.get(r.template)
        if factory is None:
            raise ValueError(
                f"unknown rule template {r.template!r}; known templates: "
                f"{sorted(TEMPLATE_REGISTRY)}"
            )
        rules.append(factory(**r.params))

    return Policy.with_rules(
        allow=set(spec.allow) if spec.allow is not None else None,
        deny=set(spec.deny),
        rules=rules,
        caps=[c.to_cap() for c in spec.caps],
    )


# -- the "give me runnable Python" export ------------------------------------


def to_python(spec: PolicySpec | dict[str, Any]) -> str:
    """Emit a standalone Python module that builds the same ``Policy``.

    The buyer who would rather own code than JSON gets a file they can read,
    edit, and import. The emitted module calls the *same* public surface
    (``Policy.with_rules``, ``Cap.count/sum``, the template factories), so what
    it builds is verdict-identical to :func:`from_spec` — pinned by
    ``tests/test_governance_spec.py``.
    """
    if isinstance(spec, dict):
        spec = PolicySpec.from_dict(spec)

    lines: list[str] = [
        '"""Generated by Pherix governance UI — edit freely, this is just code."""',
        "",
        "from pherix.core.policy import Cap, Policy",
    ]
    template_names = sorted({r.template for r in spec.rules})
    if template_names:
        lines.append(
            "from pherix.governance.templates import "
            + ", ".join(template_names)
        )
    lines += ["", ""]

    cap_lines = []
    for c in spec.caps:
        if c.kind == "count":
            cap_lines.append(
                f"    Cap.count(tool={c.tool!r}, max={int(c.max)}),"
            )
        else:
            cap_lines.append(
                f"    Cap.sum(tool={c.tool!r}, "
                f"via=lambda a: float(a.get({c.field!r}, 0) or 0), "
                f"max={c.max!r}),"
            )

    rule_lines = []
    for r in spec.rules:
        kw = ", ".join(f"{k}={v!r}" for k, v in r.params.items())
        rule_lines.append(f"    {r.template}({kw}),")

    allow_repr = (
        "{" + ", ".join(repr(t) for t in spec.allow) + "}"
        if spec.allow is not None
        else "None"
    )
    deny_repr = (
        "{" + ", ".join(repr(t) for t in spec.deny) + "}" if spec.deny else "set()"
    )

    lines.append(f"# {spec.name} — {spec.description}".rstrip(" —"))
    lines.append("policy = Policy.with_rules(")
    lines.append(f"    allow={allow_repr},")
    lines.append(f"    deny={deny_repr},")
    lines.append("    rules=[")
    lines += [f"    {rl}" for rl in rule_lines]
    lines.append("    ],")
    lines.append("    caps=[")
    lines += [f"    {cl}" for cl in cap_lines]
    lines.append("    ],")
    lines.append(")")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "CapSpec",
    "PolicySpec",
    "RuleSpec",
    "from_spec",
    "to_python",
    "to_spec",
]
