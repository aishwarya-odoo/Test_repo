"""Idempotency key generation.

The actual "has this already been processed?" check is a DB lookup against
tally.sync.log (added in Phase 3, once that model exists) -- this module
only owns the key format, so every publisher/consumer builds it the same
way: source:model:record_id:event_type. Two different events for the same
record (e.g. created then updated) get different keys since event_type is
part of it; the same event replayed (e.g. a redelivered message) gets the
identical key both times, which is what makes the Phase 3 lookup work.
"""


def build_key(source, model, record_id, event_type):
    return f"{source}:{model}:{record_id}:{event_type}"
