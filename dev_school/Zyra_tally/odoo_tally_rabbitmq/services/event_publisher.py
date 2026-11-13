"""Publishes one outbox event: builds the idempotency key + envelope and
routes it through a RabbitMQClient already connected via declare_topology().

Routing key convention:
  * source="odoo"  -> event_type is bare ("customer.created") -> routed as
    "odoo.<event_type>" (matches OUTGOING_QUEUE_BINDINGS).
  * source="tally" -> event_type already carries the "tally." prefix
    ("tally.customer.created") -> routed as-is (matches
    INCOMING_QUEUE_BINDINGS). This module only ever publishes source="odoo";
    source="tally" messages are published by the external connector, but the
    routing convention is documented here since both sides must agree on it.
"""
from . import idempotency, message_serializer


def routing_key_for(event_type, source):
    if source == "tally":
        return event_type
    return f"odoo.{event_type}"


def publish_event(client, event_type, source, company_id, model, record_id, data):
    key = idempotency.build_key(source, model, record_id, event_type)
    envelope = message_serializer.build_envelope(
        event_type=event_type,
        source=source,
        company_id=company_id,
        data=data,
        idempotency_key=key,
    )
    client.publish(routing_key_for(event_type, source), envelope, message_id=envelope["message_id"])
    return envelope
