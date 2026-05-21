"""Payment compensators — the highest-stakes inverses in the catalog.

Money moving the wrong way is the canonical "I wish the agent's tool call
had been transactional" disaster, so these pairs are the headline of the
catalog.

  charge  → refund            (capture funds → return them)
  payout  → reverse_payout    (send funds out → claw them back)

Both reverse by the **idempotency key** the caller supplies to the action.
This is not an accident of the engine: it is exactly how real payment APIs
(Stripe, Adyen, …) want you to do it. The caller mints one idempotency key
per logical charge; the charge and its refund both key off it, so a retry
of either is a no-op at the provider and the refund unambiguously targets
the charge it is inverting — even though, per the engine contract, the
compensator only ever sees the *args*, never the charge's return value.
"""

from __future__ import annotations

from pherix.core.tools import tool


def register_charge_refund(client, *, resource: str = "payments"):
    """Register ``charge`` and its left-inverse ``refund``.

    ``client`` is duck-typed and must expose::

        client.charge(idempotency_key, amount_cents, currency) -> object
        client.refund(idempotency_key) -> object

    ``charge`` declares ``refund`` as its compensator, so a charge that has
    fired is auto-undone on rollback — no human approval needed.
    """

    @tool(resource=resource, reversible=False, injects_handle=False)
    def refund(idempotency_key, amount_cents, currency="usd"):
        # Reverses by the idempotency key alone; ``amount_cents`` /
        # ``currency`` are present only because the runtime fires the
        # compensator with the action's full arg set (full refund semantics).
        return client.refund(idempotency_key)

    @tool(
        resource=resource,
        reversible=False,
        injects_handle=False,
        compensator="refund",
    )
    def charge(idempotency_key, amount_cents, currency="usd"):
        return client.charge(idempotency_key, amount_cents, currency)

    return charge, refund


def register_payout_reverse(client, *, resource: str = "payments"):
    """Register ``payout`` and its left-inverse ``reverse_payout``.

    ``client`` must expose::

        client.payout(payout_id, amount_cents, destination) -> object
        client.reverse_payout(payout_id) -> object

    A payout is funds leaving the platform to a destination account;
    reversing claws them back. Reverses by ``payout_id``.
    """

    @tool(resource=resource, reversible=False, injects_handle=False)
    def reverse_payout(payout_id, amount_cents, destination):
        return client.reverse_payout(payout_id)

    @tool(
        resource=resource,
        reversible=False,
        injects_handle=False,
        compensator="reverse_payout",
    )
    def payout(payout_id, amount_cents, destination):
        return client.payout(payout_id, amount_cents, destination)

    return payout, reverse_payout
