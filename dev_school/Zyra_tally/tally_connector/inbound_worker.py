"""Polls TallyPrime for changes (Tally -> Odoo) and publishes them to the
tally.incoming.* queues. Tally's XML gateway has no push/webhook mechanism,
so this is a plain interval poll using each collection's ALTERID cursor
(see state_store.py and sync_engine.py's poll_and_publish)."""
import logging
import time

import rabbitmq_client

_logger = logging.getLogger(__name__)

_CATEGORIES = ["ledger", "stock_item", "voucher"]


def run_once(config, sync_engine, state_store):
    with rabbitmq_client.RabbitMQClient(config) as client:
        client.declare_topology()
        total = 0
        for category in _CATEGORIES:
            total += sync_engine.poll_and_publish(category, state_store, client)
        return total


def run_forever(config, sync_engine, state_store):
    _logger.info("Inbound worker starting -- polling every %ss", config.poll_interval_seconds)
    while True:
        try:
            published = run_once(config, sync_engine, state_store)
            if published:
                _logger.info("Inbound poll published %s event(s)", published)
        except Exception:
            _logger.exception("Inbound poll failed -- will retry next interval")
        time.sleep(config.poll_interval_seconds)
