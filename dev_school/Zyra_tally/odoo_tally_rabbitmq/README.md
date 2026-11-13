# Odoo Tally RabbitMQ Integration

Two-way synchronization between Odoo 19 Community and TallyPrime, using RabbitMQ
as the messaging layer. Odoo never calls Tally's HTTP/XML API directly -- all
communication is asynchronous through RabbitMQ, consumed on the Tally side by
a separate connector worker service (`tally_connector/`, not part of this
Odoo addon).

## Status

Implementation in progress, phase by phase:

- [x] Phase 1 -- Module foundation
- [x] Phase 2 -- RabbitMQ infrastructure
- [x] Phase 3 -- Odoo outbox
- [x] Phase 4 -- Customer/vendor sync
- [x] Phase 5 -- Product sync
- [x] Phase 6 -- Invoice sync
- [x] Phase 7 -- Payment sync
- [x] Phase 8 -- Conflict management
- [x] Phase 9 -- Dashboard
- [x] Phase 10 -- Testing
- [x] Phase 11 -- Documentation

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) -- how the outbox, RabbitMQ, and inbound
  flows fit together; reliability and conflict-detection design.
- [INSTALLATION.md](INSTALLATION.md) -- installing and upgrading the module,
  running the test suite.
- [CONFIGURATION.md](CONFIGURATION.md) -- `tally.integration.config` fields,
  security groups.
- [RABBITMQ.md](RABBITMQ.md) -- exchange/queue/routing-key topology,
  dead-lettering, publishing guarantees.
- [TALLY_INTEGRATION.md](TALLY_INTEGRATION.md) -- the contract the separate
  `tally_connector` worker must fulfill (not built in this addon).
- [SYNC_MAPPING.md](SYNC_MAPPING.md) -- how each model matches an Odoo
  record to a Tally object, and loop prevention.
- [API_SCHEMA.md](API_SCHEMA.md) -- the JSON envelope and full event
  catalog, both directions.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) -- common failure modes and how
  to diagnose them.

## Configuration

Settings menu: **Tally Integration > Configuration**. One `tally.integration.config`
record per company. Use **Test RabbitMQ Connection** to verify connectivity
before enabling synchronization. See [CONFIGURATION.md](CONFIGURATION.md)
for the full field reference.
