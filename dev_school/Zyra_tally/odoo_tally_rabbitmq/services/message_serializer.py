"""Builds the versioned JSON envelope used for every message on tally.sync,
both directions. See idempotency.py for how idempotency_key is derived.
"""
import uuid
from datetime import datetime, timezone

SCHEMA_VERSION = "1.0"


def build_envelope(event_type, source, company_id, data, idempotency_key, message_id=None):
    """source is "odoo" or "tally" -- carried through so a consumer can
    refuse to re-publish an event it originated, breaking the update loop
    the spec warns about (Odoo update -> Tally -> Tally event -> Odoo ->
    (must not) publish again)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "message_id": message_id or str(uuid.uuid4()),
        "idempotency_key": idempotency_key,
        "source": source,
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "company_id": company_id,
        "data": data,
    }
