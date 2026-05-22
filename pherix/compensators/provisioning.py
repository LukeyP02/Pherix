"""Infrastructure provisioning compensators.

  create_resource → delete_resource   (spin up infra → tear it down)
  scale_up        → scale_down        (raise capacity → lower it back)

``create_resource → delete_resource`` is a clean left-inverse keyed by
``resource_id``: the caller mints the id, hands it to ``create``, and
``delete`` removes exactly that resource on rollback.

``scale_up → scale_down`` is the catalog's example of an inverse that is
only correct when the caller carries the **before-value in the args**.
Scaling is relative — "scale up" without knowing the prior replica count
cannot be reversed. So ``scale_up`` takes both ``from_replicas`` and
``to_replicas``; the compensator ``scale_down`` scales the same target back
to ``from_replicas``. The before-state lives in the args precisely because
the engine fires the compensator with the action's args and nothing else.
"""

from __future__ import annotations

from pherix.core.tools import tool


def register_create_delete_resource(client, *, resource: str = "provisioning"):
    """Register ``create_resource`` and its left-inverse ``delete_resource``.

    ``client`` must expose::

        client.create_resource(resource_id, kind, spec) -> object
        client.delete_resource(resource_id) -> object

    Reverses by ``resource_id``.
    """

    @tool(resource=resource, reversible=False, injects_handle=False)
    def delete_resource(resource_id, kind, spec):
        return client.delete_resource(resource_id)

    @tool(
        resource=resource,
        reversible=False,
        injects_handle=False,
        compensator="delete_resource",
    )
    def create_resource(resource_id, kind, spec):
        return client.create_resource(resource_id, kind, spec)

    return create_resource, delete_resource


def register_scale_up_down(client, *, resource: str = "provisioning"):
    """Register ``scale_up`` and its left-inverse ``scale_down``.

    ``client`` must expose::

        client.scale(target, replicas) -> object

    The action carries both endpoints — ``from_replicas`` (the before-value)
    and ``to_replicas`` (the after-value) — so the compensator can restore
    the exact prior capacity. ``scale_down`` re-scales ``target`` back to
    ``from_replicas``. Reverses by ``(target, from_replicas)``.
    """

    @tool(resource=resource, reversible=False, injects_handle=False)
    def scale_down(target, from_replicas, to_replicas):
        # Restore the before-value carried in the action's args.
        return client.scale(target, from_replicas)

    @tool(
        resource=resource,
        reversible=False,
        injects_handle=False,
        compensator="scale_down",
    )
    def scale_up(target, from_replicas, to_replicas):
        return client.scale(target, to_replicas)

    return scale_up, scale_down
