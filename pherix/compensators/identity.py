"""Identity & comms compensators.

  invite      → revoke_invite     (send an org/team invite → rescind it)
  grant_role  → revoke_role       (grant a permission → take it back)
  send_email  → (GATE, no comp)   (you cannot unsend an email — honest gate)

The first two are clean left-inverses keyed by a shared id. The third is
the catalog's worked example of *honesty about what cannot be undone*: an
email, once delivered, is in the recipient's inbox forever. There is no
semantic inverse, so the action is registered with **no compensator** and
``commit()`` gates on it — a human (or higher-trust policy) must call
``approve_irreversible()`` to let it through. The "undo" is the gate
itself: the chance to refuse *before* the irreversible thing happens.
"""

from __future__ import annotations

from pherix.core.tools import tool


def register_invite_revoke(client, *, resource: str = "identity"):
    """Register ``invite`` and its left-inverse ``revoke_invite``.

    ``client`` must expose::

        client.invite(invite_id, email, org) -> object
        client.revoke_invite(invite_id) -> object

    Reverses by ``invite_id`` — the caller mints it, so the invite and its
    revocation share the key the compensator receives in the args.
    """

    @tool(resource=resource, reversible=False, injects_handle=False)
    def revoke_invite(invite_id, email, org):
        return client.revoke_invite(invite_id)

    @tool(
        resource=resource,
        reversible=False,
        injects_handle=False,
        compensator="revoke_invite",
    )
    def invite(invite_id, email, org):
        return client.invite(invite_id, email, org)

    return invite, revoke_invite


def register_grant_revoke_role(client, *, resource: str = "identity"):
    """Register ``grant_role`` and its left-inverse ``revoke_role``.

    ``client`` must expose::

        client.grant_role(principal, role) -> object
        client.revoke_role(principal, role) -> object

    Reverses by ``(principal, role)`` — both carried in the args, so the
    compensator removes exactly the grant the action added.
    """

    @tool(resource=resource, reversible=False, injects_handle=False)
    def revoke_role(principal, role):
        return client.revoke_role(principal, role)

    @tool(
        resource=resource,
        reversible=False,
        injects_handle=False,
        compensator="revoke_role",
    )
    def grant_role(principal, role):
        return client.grant_role(principal, role)

    return grant_role, revoke_role


def register_send_email_gate(client, *, resource: str = "identity"):
    """Register ``send_email`` with **no compensator** — it gates at commit.

    ``client`` must expose ``client.send_email(to, subject, body) -> object``.

    There is no honest left-inverse of a delivered email, so Pherix does not
    pretend one exists. The action stages, and ``commit()`` blocks until a
    human calls ``approve_irreversible(effect_id)``. Returns the action only
    (there is no compensator to return).
    """

    @tool(resource=resource, reversible=False, injects_handle=False)
    def send_email(to, subject, body):
        return client.send_email(to, subject, body)

    return send_email
