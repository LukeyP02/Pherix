# Defining a Pherix policy

This is the reference for the **policy axis** — the one of Pherix's four axes
that answers *"what do we allow?"*. It is written for the operator who is about
to govern a real agent: what the primitives are, how a policy is evaluated, how
to preview what a policy would do before it bites, and how to ship one.

There are two ways to write a policy, and they produce the *same* object:

1. **Python** — import `Policy`, `Cap`, and the rule templates, and compose them.
   Full power; this is what the engine runs.
2. **The governance builder** (`site/governance.html`) — compose a policy
   visually, watch a live preview of its verdicts, and export either a JSON spec
   (loaded by `pherix.governance.from_spec`) or a runnable Python module.

The builder covers the *base* — the primitives every deployment assumes work.
The edge (a bespoke predicate over three tables and a clock) is hand-written
Python. Both meet at the same `Policy`.

## The mental model

A policy is a **predicate over the journal**. Concretely, every rule is a
function

```
rule(effect, ctx) -> Allow | Deny(reason)
```

The engine folds that predicate forward over the transaction's effect journal.
There is no separate "policy engine" — a policy is just the bundle of predicates
the fold consults at each effect. That is why a denial is cheap to explain (one
rule, one effect, one reason) and why the *audit* of a policy decision falls out
for free: the journal already records what was evaluated.

Three layers are consulted per effect, in order, and the **first `Deny` wins**:

1. **allow / deny tool-name lists** — the coarse gate. `deny` always beats
   `allow`. An empty allow-list means *allow all* (deny-list still applies); a
   non-empty allow-list means *only these tools*.
2. **rules** — the predicates above, in registration order.
3. **caps** — spend limits; see below.

## The primitives

### allow / deny lists

The bluntest instrument. `deny={"drop_table"}` forbids a tool outright;
`allow={"read_file", "sql_select"}` permits *only* those (everything else is
denied). Use the allow-list for a read-only or tightly-scoped agent; use the
deny-list to carve a few dangerous tools out of an otherwise-open agent.

### Caps — `Cap.count` and `Cap.sum`

A cap is a rule whose verdict depends on a **running total over the journal so
far**, not just the current effect. Two shapes:

- `Cap.count(tool="send_email", max=5)` — at most 5 `send_email` calls per
  transaction.
- `Cap.sum(tool="charge", via=lambda a: a["amount"], max=1000)` — the cumulative
  `amount` across all `charge` calls may not exceed 1000.

The cap denies the effect that *would push the total over* `max`. In the
serialisable spec a sum cap names its field (`field="amount"`) instead of
carrying a `via` lambda; a missing field on a given effect contributes `0` (the
cap fails open for that one effect rather than crashing).

### The human gate

Reversible effects run live and are undoable. **Irreversible** effects (an
adapter whose `supports_rollback()` is `False`, e.g. an outbound HTTP POST) are
*staged*: they do not fire until `commit()`. At commit, an irreversible effect
that has a registered **compensator** auto-commits (the compensator is its
rollback path); one with **no compensator gates** — `commit()` blocks and
requires an explicit `approve_irreversible()` from a human or a higher-trust
policy.

The gate is therefore not a policy *rule* — it is a property of the effect
(irreversible + no compensator). The builder surfaces it as the `gate`
disposition, and the `gate_irreversibles` toggle lets you preview a deployment
that has the gate turned off.

### World-state rules — the `ctx.read` templates

The interesting rules are the ones that depend on the *live world*, not just the
effect's arguments. The canonical example is `refund_if_paid`:

> A `refund_order` call may fire **only if** the order's status is `'paid'` right
> now.

The rule reads the live status through `ctx.read(resource, key)` — a mediator the
runtime threads in over its adapters. Because the value is read *at evaluation
time*, the same rule can return different verdicts at different moments. That is
the whole point of the next section.

## Stage-time vs commit-time — why the policy is evaluated twice

Pherix evaluates the policy **twice**:

- **stage-time** — when the agent calls the tool, before the effect is journalled.
  Cheap, fails fast: a clearly-forbidden call never even runs.
- **commit-time** — after every effect is folded into the journal, just before the
  adapters commit. The whole journal is re-walked and every rule re-evaluated.

For an args-only rule the two evaluations are identical, so the second walk is
free insurance. For a **world-state** rule they can *diverge*, and that
divergence is the safety property:

> The world can change between stage-time and commit-time. If an order is
> `'paid'` when the agent stages the refund, but a concurrent actor flips it to
> `'refunded'` before commit, the stage-time read returns `'paid'` (Allow) and
> the commit-time read returns `'refunded'` (Deny). The refund never fires.

This is the classic **time-of-check / time-of-use (TOCTOU)** problem, and the
twice-evaluated bracket is how Pherix closes it: the predicate
`P(effect, world)` is re-checked against the world *as it stands at commit*, not
a stale snapshot from when the agent first asked.

## Previewing a policy — the explainer

Before a policy governs a live agent, you want to know what it *would* do. The
preview is a **dry-run of the policy**: fold a candidate policy over a sample
journal and capture every verdict, with no side effects. It is the same
traversal `pherix.dry_run` performs (`Policy.collect_verdicts`), so what the
preview shows is what the runtime does — that equivalence is pinned by tests
(`tests/test_governance_js_conformance.py` even checks the browser's JS preview
against the Python engine, case for case).

In Python:

```python
from pherix.governance import preview, EffectScenario
from pherix.governance.templates import STARTER_TEMPLATES

result = preview(
    STARTER_TEMPLATES[0],  # the spend-capped starter
    [
        EffectScenario(tool="charge", args={"amount": 600}),
        EffectScenario(tool="charge", args={"amount": 600}),  # 1200 > 1000
    ],
)
for row in result.rows:
    print(row.index, row.tool, row.disposition, row.reasons)
# 0 charge allow []
# 1 charge cap ['would exceed sum cap ...']
```

Each effect gets one of four dispositions: **allow**, **deny** (a rule or the
deny-list refused it), **cap** (a spend cap tripped), or **gate** (irreversible,
no compensator). World-state rules read from a sample `world` map you pass in, so
you can preview both sides of a TOCTOU flip by previewing against two worlds.

## Starter templates

A buyer should start from a running example, not a blank page.
`pherix.governance.templates.STARTER_TEMPLATES` ships four:

| Template | What it does |
|---|---|
| `spend-capped` | Caps spend (`sum`) and call count (`count`); otherwise open. The safety net for a billing/notification agent. |
| `read-only` | Allow-list of read tools only; everything else denied. Swap in your own read tool names. |
| `approve-irreversibles` | Reversible work runs freely; anything uncompensable gates at commit for a human. The default posture on production. |
| `refund-guarded` | The `refund_if_paid` world-state rule — refund only if the order is `'paid'` right now, TOCTOU-safe. |

Copy one, edit it in the builder or in Python, and ship.

## The serialisable spec — exporting and loading

A `Policy` is live Python (caps carry lambdas, rules are closures), so it cannot
be saved to a file or loaded into a browser. The **spec** is its serialisable
shadow:

```python
from pherix.governance import PolicySpec, from_spec, to_python

spec = PolicySpec.from_dict(json.loads(open("policy.json").read()))
policy = from_spec(spec)        # → a real Policy the engine runs
print(to_python(spec))          # → a runnable .py module that builds the same Policy
```

The round-trip is load-bearing and tested: `from_spec(spec)` builds a policy that
produces exactly the verdicts the preview showed, and `to_python(spec)` emits a
module that, when imported, builds a verdict-identical policy. Pick whichever
artifact your team prefers to review — JSON in a config repo, or Python next to
your tool definitions.

### What the spec does *not* cover

Arbitrary hand-written rule closures are deliberately not serialisable — only the
**named templates** in `TEMPLATE_REGISTRY` round-trip. If you need a bespoke
predicate, write it in Python with `@policy.rule` and register it directly; the
builder is for the base, your code is for the edge. Adding a new template is a
two-language change (register the Python factory *and* its JS twin in
`site/policy-eval.js`), and the conformance test refuses to pass until the two
agree — which is exactly the guard that keeps "preview == reality" true.
