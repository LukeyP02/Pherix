"""The vetted compensator catalog — common action/inverse pairs.

A *compensator* is the semantic left-inverse of an irreversible tool:
``compensator ∘ tool ≈ identity``. Some side-effects cannot be snapshotted
and restored (charge a card, send an invite, create a cloud resource); the
only honest "undo" is to run an opposite action. This package ships a
catalog of those opposite actions, each tested as a true left-inverse —
the fiddly correctness done once so a buyer does not get it subtly wrong.

Every entry is a **factory** that takes a duck-typed client and registers
the action tool plus its compensator tool, returning the wrapped callables::

    from pherix.compensators import register_charge_refund
    charge, refund = register_charge_refund(stripe_client)

Two structural facts the factories are designed around (both load-bearing,
both verified by the engine in ``pherix/core/runtime.py``):

1. **The compensator receives the action's args, not its return value.**
   On rollback the runtime builds a synthetic effect with
   ``args=effect.args`` and fires the compensator with those. So every pair
   reverses off a *shared key carried in the args* — the idempotency-key
   pattern: the caller passes e.g. ``idempotency_key`` / ``resource_id`` /
   ``invite_id`` into the action, and the compensator reverses by that same
   key. A compensator that needed the action's *return* value could not be
   wired this way.

2. **No compensator means the action gates.** For genuinely un-undoable
   actions (you cannot unsend an email or an SMS) the factory registers the
   action with *no* compensator, so ``commit()`` blocks until a human calls
   ``approve_irreversible()``. The honest undo is a human gate, not a fake
   inverse — that is the project's stance, made concrete here.

The client is duck-typed: this package never imports ``stripe`` /
``PyGithub`` / ``boto3`` / etc. The buyer injects their real client; tests
inject a fake in-memory one. The kernel stays dependency-free.
"""

from __future__ import annotations

from pherix.compensators.identity import (
    register_grant_revoke_role,
    register_invite_revoke,
    register_send_email_gate,
)
from pherix.compensators.payments import (
    register_charge_refund,
    register_payout_reverse,
)
from pherix.compensators.provisioning import (
    register_create_delete_resource,
    register_scale_up_down,
)
from pherix.compensators.saas import (
    register_jira_create_delete_issue,
    register_github_label,
    register_github_pr,
    register_sendgrid_contact,
    register_slack_message,
    register_stripe_customer,
    register_twilio_sms_gate,
)

__all__ = [
    # payments
    "register_charge_refund",
    "register_payout_reverse",
    # identity / comms
    "register_invite_revoke",
    "register_grant_revoke_role",
    "register_send_email_gate",
    # provisioning
    "register_create_delete_resource",
    "register_scale_up_down",
    # saas
    "register_github_pr",
    "register_github_label",
    "register_slack_message",
    "register_stripe_customer",
    "register_sendgrid_contact",
    "register_twilio_sms_gate",
    "register_jira_create_delete_issue",
]
