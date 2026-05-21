"""Capability policy — a predicate fold over the journal.

Slice 6 turns ``Policy`` from a tool-name allow/deny list into a real
predicate over ``(effect, ctx)``. The fold runs twice:

- **stage-time** — when the runtime intercepts a tool call, before the
  effect is journalled and (for reversibles) before the adapter applies it.
  Cheap, fails fast.
- **commit-time** — after every effect has been folded forward into the
  journal, just before adapter brackets commit. Re-walks the journal and
  re-evaluates every applicable rule against every effect.

For Slice 6's args-only rules the two evaluations are identical. The
commit-time bracket lands as architecture so Slice 6.5's world-state-aware
rules slot in without engine surgery — fill in :meth:`PolicyContext.read`,
write the tests, no structural change.

The whole engine is one shape: a rule is a callable
``(effect, ctx) -> Allow | Deny(reason)``. ``Cap.count`` and ``Cap.sum``
are rules whose context-carried running total turns the predicate from
"this single effect" into "this effect against the journal so far." The
runtime owns ``ctx``; rules read through it.

Backwards-compat:
- ``Policy()`` / ``Policy(allow=...)`` / ``Policy(deny=...)`` keep working.
- ``policy.check(tool)`` keeps working — evaluates allow/deny only.
- ``PolicyViolation`` keeps the ``tool`` / ``reason`` attributes; gains
  ``where`` / ``rule`` / ``effect_index``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Sequence

from pherix.core.effects import Effect


Where = Literal["stage", "commit"]

# A read mediator answers "what is the live value at (resource, key) right
# now?" for world-state-aware rules (#7). The runtime owns the adapter map and
# the live connections, so it constructs one of these and threads it into every
# PolicyContext. Kept as a bare callable (not a Protocol) so the runtime can
# supply a closure over its adapters with zero ceremony, and a test can supply
# a one-line lambda / fake to prove a rule's behaviour at the policy layer
# without standing up the whole runtime.
ReadMediator = Callable[[str, Any], Any]


class PolicyViolation(Exception):
    """Raised when a rule denies an effect (stage-time or commit-time).

    Attributes:
        tool: the tool name of the denied effect (None if the violation is
            not bound to a specific effect — kept Optional so future shapes
            stay backwards-compatible).
        reason: the ``Deny()`` message from the rule that fired.
        where: ``"stage"`` if denied before journalling, ``"commit"`` if
            denied during the commit-time re-walk.
        rule: the :class:`PolicyRule` or :class:`_Cap` whose evaluation
            returned ``Deny``. ``None`` for the legacy allow/deny path.
        effect_index: the journal index of the denied effect. ``None`` at
            stage-time (the effect has not been indexed yet).
    """

    def __init__(
        self,
        reason: str,
        *,
        tool: str | None = None,
        where: Where = "stage",
        rule: Any = None,
        effect_index: int | None = None,
    ):
        self.tool = tool
        self.reason = reason
        self.where = where
        self.rule = rule
        self.effect_index = effect_index

        msg = "policy denied"
        if tool is not None:
            msg += f" tool {tool!r}"
        msg += f": {reason}"
        if rule is not None:
            rule_name = getattr(rule, "name", repr(rule))
            msg += f" (rule={rule_name}, where={where})"
        super().__init__(msg)


# -- verdicts ----------------------------------------------------------------


@dataclass(frozen=True)
class Allow:
    """The rule permits this effect. Singleton-ish; instances are cheap."""


@dataclass(frozen=True)
class Deny:
    """The rule denies this effect; ``reason`` becomes ``PolicyViolation.reason``."""

    reason: str


Verdict = Allow | Deny


# -- rules -------------------------------------------------------------------


@dataclass
class PolicyRule:
    """A registered rule: a callable ``(effect, ctx) -> Allow | Deny``.

    The wrapper exists so rules carry a stable ``name`` for
    :attr:`PolicyViolation.rule` and so the engine can treat ``@policy.rule``
    callables identically to :class:`Cap` instances.
    """

    fn: Callable[[Effect, "PolicyContext"], Verdict]
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = getattr(self.fn, "__name__", repr(self.fn))

    def evaluate(self, effect: Effect, ctx: "PolicyContext") -> Verdict:
        return self.fn(effect, ctx)


# -- spend caps --------------------------------------------------------------


@dataclass
class _CountCap:
    """Cap on the number of times a tool fires within a single txn."""

    tool: str
    max: int

    @property
    def name(self) -> str:
        return f"Cap.count(tool={self.tool!r}, max={self.max})"

    def applies_to(self, effect: Effect) -> bool:
        return effect.tool == self.tool

    def contribution(self, effect: Effect) -> int:
        return 1

    def evaluate(self, effect: Effect, ctx: "PolicyContext") -> Verdict:
        if not self.applies_to(effect):
            return Allow()
        running = ctx.cap_running(self)
        if running + 1 > self.max:
            return Deny(
                f"would exceed count cap (max={self.max}) for tool "
                f"{self.tool!r}; already at {running}"
            )
        return Allow()


@dataclass
class _SumCap:
    """Cap on the cumulative numeric contribution of a tool within a txn.

    ``via(args)`` extracts the contribution from each fire's args dict.
    The cap denies as soon as ``sum + contribution > max``.
    """

    tool: str
    via: Callable[[dict], float | int]
    max: float | int

    @property
    def name(self) -> str:
        return f"Cap.sum(tool={self.tool!r}, max={self.max})"

    def applies_to(self, effect: Effect) -> bool:
        return effect.tool == self.tool

    def contribution(self, effect: Effect) -> float | int:
        return self.via(effect.args)

    def evaluate(self, effect: Effect, ctx: "PolicyContext") -> Verdict:
        if not self.applies_to(effect):
            return Allow()
        running = ctx.cap_running(self)
        candidate = running + self.contribution(effect)
        if candidate > self.max:
            return Deny(
                f"would exceed sum cap (max={self.max}) for tool "
                f"{self.tool!r}; running={running}, contribution="
                f"{self.contribution(effect)}"
            )
        return Allow()


class Cap:
    """Namespace for spend-cap primitives.

    Caps are themselves rules: they register alongside ``@policy.rule``
    callables and the engine treats them identically. The runtime carries
    per-cap running totals on :class:`PolicyContext`; the cap's check is
    "would this effect push the running total above ``max``?" — if yes,
    ``Deny``.
    """

    @staticmethod
    def count(*, tool: str, max: int) -> _CountCap:
        return _CountCap(tool=tool, max=max)

    @staticmethod
    def sum(
        *,
        tool: str,
        via: Callable[[dict], float | int],
        max: float | int,
    ) -> _SumCap:
        return _SumCap(tool=tool, via=via, max=max)


# -- evaluation context ------------------------------------------------------


class PolicyContext:
    """Runtime-owned object passed to every rule evaluation.

    Carries the journal so far, the per-cap running totals, the ``where``
    label (stage vs commit), and the :meth:`read` placeholder that Slice
    6.5 fills in for world-state-aware rules. The runtime is the owner;
    rules read through it. Mutating ``journal`` from inside a rule is not
    supported — the journal is exposed as a tuple snapshot per access.
    """

    def __init__(
        self,
        *,
        journal: Sequence[Effect],
        where: Where,
        reader: ReadMediator | None = None,
    ):
        # Live reference to the runtime's journal list — not a copy. ``journal``
        # mutates as effects are appended, so a rule that reads ``ctx.journal``
        # at stage-time sees the partial journal-so-far (the current effect is
        # NOT in it yet — it's the candidate being evaluated), and at
        # commit-time sees the full journal. The :attr:`journal` property
        # returns an immutable tuple snapshot so a misbehaving rule cannot
        # mutate the source.
        self._journal = journal
        self.where: Where = where
        # Slice 6.5 / #7: the world-state read mediator. The runtime owns the
        # adapter map and the live connections, so it supplies a callable
        # ``(resource, key) -> value`` that queries the right adapter's *live*
        # committed state. ``None`` (the default) means no reader is bound —
        # constructing a context for args-only rules costs nothing and a rule
        # that nonetheless calls ``ctx.read`` gets a clear error rather than a
        # silent wrong answer. See :meth:`read`.
        self._reader = reader
        # Per-cap running totals keyed by ``id(cap)``. Object identity is
        # the right key because two structurally-identical caps (e.g.
        # ``Cap.count(tool='x', max=3)`` constructed twice) should be
        # independent buckets — they were registered as distinct rules.
        self._cap_totals: dict[int, float | int] = {}

    @property
    def journal(self) -> tuple[Effect, ...]:
        """The journal so far, frozen at this moment in time."""
        return tuple(self._journal)

    def cap_running(self, cap: Any) -> float | int:
        return self._cap_totals.get(id(cap), 0)

    def cap_add(self, cap: Any, value: float | int) -> None:
        self._cap_totals[id(cap)] = self.cap_running(cap) + value

    def reset_caps(self) -> None:
        """Clear all per-cap totals — used between the stage and commit walks."""
        self._cap_totals.clear()

    def read(self, resource: str, key: Any) -> Any:
        """Read live world-state through the runtime-supplied mediator (#7).

        A rule that needs to check live adapter state — e.g. "refund order
        42 only if ``order.status='paid'`` *right now*" — calls
        ``ctx.read(resource, key)``. The runtime owns the adapter map and the
        live connections, so it binds a :data:`ReadMediator` (a callable
        ``(resource, key) -> value``) into the context at construction; this
        method dispatches through it.

        The read is *live*: for the SQL adapter the mediator runs a ``SELECT``
        against the adapter's connection, so it reflects whatever committed
        state exists at the moment of the call. That is exactly what makes the
        commit-time re-walk able to **diverge** from stage-time — if the world
        moved between the two evaluations of the same predicate
        ``P(effect, world_state)``, ``ctx.read`` returns the new value and the
        rule's verdict can flip. This is the whole point of the twice-evaluated
        bracket (TOCTOU safety): the predicate is re-checked against the
        *current* world, not a stale snapshot.

        Raises :class:`RuntimeError` if no reader is bound — a rule that needs
        world state must be run by a runtime (or a test) that supplied one.
        Silent ``None`` would let a refund-if-paid rule pass against a phantom
        "not paid" reading; the loud error is the honest failure mode.
        """
        if self._reader is None:
            raise RuntimeError(
                "PolicyContext.read called but no read mediator is bound. "
                "World-state-aware rules require the runtime (or test) to "
                "construct PolicyContext with reader=<callable (resource, key) "
                "-> value>; the runtime threads its adapter map in as that "
                "callable."
            )
        return self._reader(resource, key)


# -- the policy itself -------------------------------------------------------


@dataclass
class Policy:
    """A bundle of allow/deny lists + rules + caps.

    The Slice 1 shape (``Policy(allow=..., deny=...)``) is preserved
    verbatim — those fields evaluate first inside :meth:`evaluate` and
    inside :meth:`check`. The Slice 6 additions are purely additive:
    pass ``rules=[...]`` and ``caps=[...]`` via :meth:`with_rules`, or
    register them post-hoc with the :meth:`rule` decorator and
    :meth:`add_cap`.

    Deny always wins over allow; rules and caps fire in registration
    order; the first ``Deny`` short-circuits with a
    :class:`PolicyViolation`.
    """

    allow: set[str] | None = None
    deny: set[str] = field(default_factory=set)
    rules: list[PolicyRule] = field(default_factory=list)
    caps: list[Any] = field(default_factory=list)

    # -- construction ---------------------------------------------------

    @classmethod
    def allow_all(cls) -> "Policy":
        return cls()

    @classmethod
    def with_rules(
        cls,
        *,
        rules: Sequence[Callable[[Effect, PolicyContext], Verdict]] | None = None,
        caps: Sequence[Any] | None = None,
        allow: set[str] | None = None,
        deny: set[str] | None = None,
    ) -> "Policy":
        """Compose a policy declaratively.

        ``rules`` is a sequence of plain callables ``(effect, ctx) ->
        Allow | Deny`` — they are wrapped into :class:`PolicyRule`
        automatically. ``caps`` is a sequence of :class:`_CountCap` /
        :class:`_SumCap` (constructed via :meth:`Cap.count` /
        :meth:`Cap.sum`).
        """
        p = cls(allow=allow, deny=set(deny) if deny is not None else set())
        for fn in rules or ():
            p.rules.append(PolicyRule(fn=fn))
        for cap in caps or ():
            p.caps.append(cap)
        return p

    # -- registration (decorator-style) ---------------------------------

    def rule(
        self,
        fn: Callable[[Effect, PolicyContext], Verdict],
    ) -> Callable[[Effect, PolicyContext], Verdict]:
        """Register ``fn`` as a rule on this policy instance.

        Usable as a decorator::

            policy = Policy.allow_all()

            @policy.rule
            def no_enterprise_updates(effect, ctx):
                if effect.tool == "update_user" and effect.args.get("tier") == "enterprise":
                    return Deny("enterprise tier off-limits")
                return Allow()

        Returns ``fn`` unchanged so the decorated function stays callable
        outside Pherix for unit-testing the predicate directly.
        """
        self.rules.append(PolicyRule(fn=fn))
        return fn

    def add_cap(self, cap: Any) -> None:
        """Imperatively add a cap to this policy."""
        self.caps.append(cap)

    # -- legacy entry point (backwards-compat) --------------------------

    def check(self, tool: str) -> None:
        """Tool-name allow/deny check (Slice 1 shape).

        Preserved verbatim for backwards-compatibility — call-sites in
        :mod:`pherix.core.replay` use this entry point and the existing
        252 tests assert it. Rules and caps are *not* fired by this
        method (they need a full :class:`Effect` and a
        :class:`PolicyContext`); for the Slice 6 path call
        :meth:`evaluate` from the runtime instead.
        """
        if tool in self.deny:
            raise PolicyViolation("tool is deny-listed", tool=tool)
        if self.allow is not None and tool not in self.allow:
            raise PolicyViolation("tool is not in the allow-list", tool=tool)

    def permits(self, tool: str) -> bool:
        try:
            self.check(tool)
            return True
        except PolicyViolation:
            return False

    # -- the Slice 6 entry point ---------------------------------------

    def evaluate(
        self,
        effect: Effect,
        ctx: PolicyContext,
        *,
        where: Where | None = None,
    ) -> None:
        """Evaluate every applicable rule against ``effect``.

        Folds three layers in order:
          1. allow/deny tool-name lists (Slice 1 shape).
          2. registered rules (D2).
          3. caps (D4) — and on Allow, accumulates the cap's contribution.

        Raises :class:`PolicyViolation` on the first ``Deny`` verdict.
        The exception carries the registered rule (so the caller can
        introspect which one fired) plus the ``where`` label.

        The ``where`` kwarg overrides ``ctx.where`` for the duration of
        the call — the runtime can reuse one ``ctx`` instance across
        stage-time and commit-time walks (passing the right ``where``
        explicitly each time) rather than constructing two contexts.
        """
        if where is not None:
            ctx.where = where
        active_where: Where = ctx.where

        # 1. allow/deny — D6 shape from Slice 1.
        if effect.tool in self.deny:
            raise PolicyViolation(
                "tool is deny-listed",
                tool=effect.tool,
                where=active_where,
                effect_index=(
                    effect.index if active_where == "commit" else None
                ),
            )
        if self.allow is not None and effect.tool not in self.allow:
            raise PolicyViolation(
                "tool is not in the allow-list",
                tool=effect.tool,
                where=active_where,
                effect_index=(
                    effect.index if active_where == "commit" else None
                ),
            )

        # 2. registered rules.
        for rule in self.rules:
            verdict = rule.evaluate(effect, ctx)
            if isinstance(verdict, Deny):
                raise PolicyViolation(
                    verdict.reason,
                    tool=effect.tool,
                    where=active_where,
                    rule=rule,
                    effect_index=(
                        effect.index if active_where == "commit" else None
                    ),
                )

        # 3. caps — evaluate, then accumulate on Allow so the next effect
        # sees the running total.
        for cap in self.caps:
            verdict = cap.evaluate(effect, ctx)
            if isinstance(verdict, Deny):
                raise PolicyViolation(
                    verdict.reason,
                    tool=effect.tool,
                    where=active_where,
                    rule=cap,
                    effect_index=(
                        effect.index if active_where == "commit" else None
                    ),
                )
            if cap.applies_to(effect):
                ctx.cap_add(cap, cap.contribution(effect))

    def evaluate_journal(
        self,
        txn: Any,
        ctx: PolicyContext,
    ) -> None:
        """Commit-time re-walk: re-evaluate every rule against every effect.

        Resets per-cap totals (the walk re-accumulates from zero so the
        same rule semantics apply as stage-time) then folds forward over
        ``txn.effects``. ``ctx.where`` flips to ``"commit"`` for the
        duration. The first ``Deny`` raises :class:`PolicyViolation`
        with ``where='commit'`` and the offending effect's index.
        """
        ctx.reset_caps()
        for effect in txn.effects:
            self.evaluate(effect, ctx, where="commit")

    # -- Slice 7: capture-mode evaluation (no short-circuit, no raise) -----

    def try_evaluate(
        self,
        effect: Effect,
        ctx: PolicyContext,
        *,
        where: Where | None = None,
    ) -> list["PolicyVerdict"]:
        """Capture-mode counterpart of :meth:`evaluate`.

        Walks every rule and every cap against ``effect``; never raises;
        returns one :class:`PolicyVerdict` per rule/cap evaluation. The
        allow/deny tool-name lists contribute at most one extra verdict —
        and only on Deny (Allow on the allow-list layer is implicit and
        produces nothing, matching :meth:`evaluate`'s "everyone gets
        through unless allow/deny says otherwise" semantics).

        Caps still only accumulate on Allow. A denied cap's contribution
        does NOT advance the running total, so the running total at the
        end of the walk is identical to what :meth:`evaluate` would
        produce for the same prefix of Allow-yielding effects. This is
        the load-bearing equality between raise-mode and capture-mode:
        rule predicates that *would* fire in :meth:`evaluate` fire here
        too with the same arguments.
        """
        if where is not None:
            ctx.where = where
        active_where: Where = ctx.where
        verdicts: list[PolicyVerdict] = []

        # 1. allow/deny — capture as Deny verdict when it bites; allow-list
        # passes are implicit (no entry).
        if effect.tool in self.deny:
            verdicts.append(
                PolicyVerdict(
                    allow=False,
                    rule=None,
                    effect_index=effect.index,
                    where=active_where,
                    tool=effect.tool,
                    reason="tool is deny-listed",
                )
            )
        elif self.allow is not None and effect.tool not in self.allow:
            verdicts.append(
                PolicyVerdict(
                    allow=False,
                    rule=None,
                    effect_index=effect.index,
                    where=active_where,
                    tool=effect.tool,
                    reason="tool is not in the allow-list",
                )
            )

        # 2. registered rules — one verdict each, regardless of outcome.
        for rule in self.rules:
            v = rule.evaluate(effect, ctx)
            if isinstance(v, Deny):
                verdicts.append(
                    PolicyVerdict(
                        allow=False,
                        rule=rule,
                        effect_index=effect.index,
                        where=active_where,
                        tool=effect.tool,
                        reason=v.reason,
                    )
                )
            else:
                verdicts.append(
                    PolicyVerdict(
                        allow=True,
                        rule=rule,
                        effect_index=effect.index,
                        where=active_where,
                        tool=effect.tool,
                    )
                )

        # 3. caps — one verdict each; accumulate only on Allow.
        for cap in self.caps:
            v = cap.evaluate(effect, ctx)
            if isinstance(v, Deny):
                verdicts.append(
                    PolicyVerdict(
                        allow=False,
                        rule=cap,
                        effect_index=effect.index,
                        where=active_where,
                        tool=effect.tool,
                        reason=v.reason,
                    )
                )
            else:
                verdicts.append(
                    PolicyVerdict(
                        allow=True,
                        rule=cap,
                        effect_index=effect.index,
                        where=active_where,
                        tool=effect.tool,
                    )
                )
                if cap.applies_to(effect):
                    ctx.cap_add(cap, cap.contribution(effect))

        return verdicts

    def collect_verdicts(
        self,
        txn: Any,
        ctx: PolicyContext,
    ) -> list["PolicyVerdict"]:
        """Commit-time capture walk over the whole journal.

        Resets per-cap totals (matching :meth:`evaluate_journal`'s
        re-accumulate-from-zero semantics) then folds forward through
        every effect with :meth:`try_evaluate`. Returns the flat list of
        every verdict produced; never raises on ``Deny``. Used by
        :func:`pherix.dry_run` as the commit-time policy bracket.
        """
        ctx.reset_caps()
        out: list[PolicyVerdict] = []
        for effect in txn.effects:
            out.extend(self.try_evaluate(effect, ctx, where="commit"))
        return out


# -- Slice 7: capture-mode verdict carrier ---------------------------------


@dataclass
class PolicyVerdict:
    """One evaluation of one rule (or cap, or the allow/deny list) against
    one effect, captured rather than raised.

    Emitted by :meth:`Policy.try_evaluate` (per stage-time tool call) and
    by :meth:`Policy.collect_verdicts` (per commit-time journal walk).
    Aggregated into :class:`pherix.core.dry_run.DryRunResult.policy_verdicts`.

    The :attr:`rule` field is the live rule object (a
    :class:`PolicyRule`, or the cap returned by :meth:`Cap.count` /
    :meth:`Cap.sum`), or ``None`` for verdicts attributable to the
    allow/deny tool-name lists (those have no per-rule identity to
    surface). :attr:`rule_name` is the convenience handle for printing /
    asserting in tests.
    """

    allow: bool
    rule: Any | None
    effect_index: int
    where: Where
    tool: str
    reason: str | None = None

    @property
    def rule_name(self) -> str | None:
        if self.rule is None:
            return None
        return getattr(self.rule, "name", None)


# -- #7: world-state-aware rules + the SQL read mediator -------------------


def sql_reader(adapters: dict[str, Any]) -> ReadMediator:
    """Build a :data:`ReadMediator` over an adapter map for SQL reads.

    The returned callable takes ``(resource, key)`` where ``key`` is a
    ``(table, pk_column, pk_value, value_column)`` tuple, and returns the live
    value of ``value_column`` for that row — or ``None`` if the row is absent.

    The read goes through the adapter's *own* connection, so it sees the
    transaction's view of committed state at the instant of the call. Identifier
    parts (``table``, ``pk_column``, ``value_column``) come from the rule
    definition — never from agent input — so interpolating them into the SQL is
    safe by construction (SQLite cannot parameterise identifiers regardless);
    the ``pk_value`` is always bound as a parameter.

    The runtime threads ``sql_reader(self._adapters)`` into ``PolicyContext`` so
    rules can call ``ctx.read("sql", (table, pk_col, pk_val, val_col))``. A
    resource with no SQL-shaped adapter (no ``.conn``) raises a clear error —
    the rule asked for a read the bound resource cannot serve.
    """

    def _read(resource: str, key: Any) -> Any:
        adapter = adapters.get(resource)
        if adapter is None:
            raise RuntimeError(
                f"ctx.read: no adapter registered for resource {resource!r}"
            )
        conn = getattr(adapter, "conn", None)
        if conn is None:
            raise RuntimeError(
                f"ctx.read: adapter for resource {resource!r} has no SQL "
                f"connection (.conn); world-state reads need a SQL-shaped "
                f"adapter for this key form"
            )
        table, pk_col, pk_val, value_col = key
        row = conn.execute(
            f"SELECT {value_col} FROM {table} WHERE {pk_col} = ?",
            (pk_val,),
        ).fetchone()
        if row is None:
            return None
        return row[0]

    return _read


def refund_if_paid(
    *,
    tool: str = "refund_order",
    table: str = "orders",
    id_arg: str = "order_id",
    pk_column: str = "id",
    status_column: str = "status",
    paid_value: str = "paid",
    resource: str = "sql",
) -> Callable[[Effect, "PolicyContext"], Verdict]:
    """The canonical #7 rule: refund order N only if it is 'paid' *right now*.

    Returns a rule ``(effect, ctx) -> Allow | Deny`` suitable for
    ``policy.rule(...)`` / ``Policy.with_rules(rules=[...])``. It applies only
    to ``tool`` calls; for every other tool it is a no-op ``Allow``.

    The rule reads the order's *live* status via ``ctx.read`` at evaluation
    time. Because the runtime evaluates the policy **twice** — once at
    stage-time, once at commit-time — the same predicate is checked against the
    world as it stands at each moment. If the order is ``'paid'`` when the
    agent stages the refund but a concurrent actor flips it to ``'refunded'``
    (or any non-paid state) before commit, the stage-time read returns
    ``'paid'`` (Allow) and the commit-time read returns the new value (Deny).
    That divergence is the TOCTOU protection the twice-evaluated bracket exists
    to provide — an args-only rule cannot see it because the args never changed;
    only the world did.

    The order id is read from ``effect.args[id_arg]``; the rule denies (rather
    than crashing) if that arg is missing, so a misconfigured tool call fails
    safe.
    """

    def _rule(effect: Effect, ctx: "PolicyContext") -> Verdict:
        if effect.tool != tool:
            return Allow()
        if id_arg not in effect.args:
            return Deny(
                f"refund_if_paid: tool {tool!r} has no {id_arg!r} arg to "
                f"identify the order"
            )
        order_id = effect.args[id_arg]
        live_status = ctx.read(
            resource, (table, pk_column, order_id, status_column)
        )
        if live_status != paid_value:
            return Deny(
                f"refund_if_paid: order {order_id!r} is {live_status!r}, not "
                f"{paid_value!r} — refusing to refund a non-paid order "
                f"(checked live at {ctx.where}-time)"
            )
        return Allow()

    _rule.__name__ = f"refund_if_paid({tool})"
    return _rule


__all__ = [
    "Allow",
    "Cap",
    "Deny",
    "Policy",
    "PolicyContext",
    "PolicyRule",
    "PolicyVerdict",
    "PolicyViolation",
    "ReadMediator",
    "Verdict",
    "refund_if_paid",
    "sql_reader",
]
