"""MQAdapter — irreversible publish/pub-sub adapter + a publish tool harness.

Publishing a message is irreversible in the same sense an outbound HTTP POST
is: once a broker accepts ``publish(topic, message)`` and fans it out to
subscribers, there is no before-image to restore — you cannot un-send it. So
``supports_rollback() -> False``, and the effect is staged (deferred to
``commit()``) rather than fired live. The honest undo is a *compensator*: a
second publish of a tombstone / cancellation message, or a broker-side delete
if the broker exposes one. That is a semantic inverse, not a state rollback.

The adapter is HTTPAdapter-shaped. The value over a bare adapter is the
harness: :func:`publish_tool` registers a publish ``@tool`` against an
*injectable* broker (any duck-typed object exposing ``publish(topic,
message)``), and :func:`tombstone_compensator` registers the matching
cancellation publish so a rolled-back publish is followed by a tombstone on the
same topic. Both are testable against a tiny in-memory fake broker — no real
broker, no network.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

from pherix.core.adapters.base import SnapshotHandle
from pherix.core.adapters.http import IrreversibleAdapterError
from pherix.core.effects import Effect
from pherix.core.tools import tool


@runtime_checkable
class Broker(Protocol):
    """The minimal duck-typed broker contract the harness needs.

    Any object with a ``publish(topic, message)`` method satisfies it — a real
    Kafka/RabbitMQ/SNS client wrapper, or the in-memory fake the tests use.
    """

    def publish(self, topic: str, message: Any) -> Any: ...


class MQAdapter:
    """``ResourceAdapter`` over a message broker (irreversible).

    Conforms to :class:`ResourceAdapter` only — a broker has no
    transaction-scope lifecycle Pherix can drive. ``supports_rollback() ->
    False`` routes publishes down the staging lane; the tool fires at
    commit-time with no injected handle.
    """

    name = "mq"

    def supports_rollback(self) -> bool:
        return False

    def snapshot(self, effect: Effect) -> SnapshotHandle:
        raise IrreversibleAdapterError(
            "MQAdapter.snapshot() must not be called: a published message has no "
            "before-image. Publishes are staged and fired at commit-time; the "
            "runtime must never request a snapshot here."
        )

    def apply(self, effect: Effect, tool_fn: Callable[..., Any]) -> Any:
        # No handle injected — publish tools declare injects_handle=False and
        # own the broker call (via their bound broker). The adapter passes the
        # journalled args through as kwargs.
        return tool_fn(**effect.args)

    def restore(self, handle: SnapshotHandle) -> None:
        raise IrreversibleAdapterError(
            "MQAdapter.restore() must not be called: a sent message cannot be "
            "un-sent. Unwind a fired publish via a compensator (a tombstone / "
            "cancellation publish), not via snapshot/restore."
        )

    def read_version(self, key: tuple) -> object:
        raise IrreversibleAdapterError(
            "MQAdapter.read_version() must not be called: irreversible effects "
            "are isolated-by-construction via staging — there is no version."
        )

    def write_version(self, key: tuple) -> object:
        raise IrreversibleAdapterError(
            "MQAdapter.write_version() must not be called: see read_version."
        )


# --- the harness --------------------------------------------------------------


def publish_tool(
    name: str,
    *,
    broker: Broker,
    compensator: str | None = None,
    resource: str = "mq",
) -> Callable[..., Any]:
    """Register and return an irreversible publish tool.

    The agent calls the tool with ``topic`` and ``message`` (and optionally
    ``message`` defaulting); both are journalled as the effect's ``args``. On
    rollback after the publish fired, the runtime invokes ``compensator`` with
    those *same* args (it passes ``args=effect.args``), so the paired
    compensator sees the original ``topic`` / ``message`` and can publish a
    tombstone keyed on them.

    ``broker`` is injectable — a real client wrapper or a fake. ``compensator``
    is the name of a registered tool that semantically cancels this publish;
    Pherix asserts its presence at stage-time and fires it on rollback.
    """

    @tool(
        resource=resource,
        reversible=False,
        injects_handle=False,
        name=name,
        compensator=compensator,
    )
    def _publish(topic: str, message: Any) -> Any:
        return broker.publish(topic, message)

    return _publish


def tombstone_compensator(
    name: str,
    *,
    broker: Broker,
    tombstone: Callable[[Any], Any] | None = None,
    resource: str = "mq",
) -> Callable[..., Any]:
    """Register and return a compensator that cancels a prior publish.

    Pair this with :func:`publish_tool` by passing ``compensator=name``. On
    rollback the runtime calls it with the original publish's ``topic`` /
    ``message``; it publishes a cancellation onto the *same topic*. ``tombstone``
    maps the original message to its cancellation payload — by default it wraps
    the original as ``{"tombstone": <message>}`` so a subscriber can recognise
    and ignore the prior message. A broker that supports true deletion can pass
    a ``tombstone`` that returns a delete-marker the broker honours.

    This is a *semantic left-inverse*, not a state restore: it does not undo the
    fact that the original message was delivered — it publishes the opposite
    action so downstream state converges back. That is the honest best a
    pub/sub system can offer.
    """
    make_tombstone = (
        tombstone if tombstone is not None else (lambda m: {"tombstone": m})
    )

    @tool(
        resource=resource,
        reversible=False,
        injects_handle=False,
        name=name,
    )
    def _tombstone(topic: str, message: Any) -> Any:
        return broker.publish(topic, make_tombstone(message))

    return _tombstone
