# Installation

## Requirements

- Odoo 19 Community.
- A reachable RabbitMQ broker (this project's `docker-compose.yml` runs one
  as the `rabbitmq` service, reachable from the `odoo` container at hostname
  `rabbitmq`, port `5672`; from the host machine it is published on
  `${RABBITMQ_PORT}`, default `5672`, with the management UI on
  `${RABBITMQ_MGMT_PORT}`, default `15672`).
- The `pika` Python package inside the Odoo container (declared in
  `__manifest__.py`'s `external_dependencies`; this project's
  `docker/odoo.Dockerfile` installs it into the `eyegen-odoo:19-pika` image
  tag used by `docker-compose.yml`).
- A separately-run `tally_connector` worker (not part of this addon) if you
  need the Tally side of the sync to actually happen -- see
  [TALLY_INTEGRATION.md](TALLY_INTEGRATION.md).

## Steps

1. Confirm `pika` is importable inside the Odoo container:
   ```bash
   docker compose exec odoo python3 -c "import pika; print(pika.__version__)"
   ```
   If this fails, add `pika` to whatever mechanism installs Python
   dependencies into the Odoo image (`docker/odoo.Dockerfile` in this
   project) and rebuild.
2. Confirm the module directory is mounted and visible:
   ```bash
   docker compose exec odoo ls /mnt/extra-addons/peopleconnect-odoo/addons/odoo_tally_rabbitmq
   ```
3. Install the module:
   - Apps > Update Apps List, then search "Odoo Tally RabbitMQ Integration"
     and click Install, **or**
   - `docker compose exec odoo odoo -d <your-db> -i odoo_tally_rabbitmq --stop-after-init`
4. Restart the Odoo container so the live web/cron process picks up the
   module's Python code (installing via the UI on an already-running server
   loads the new code as part of that request; installing via `-i` from the
   CLI requires a restart of the long-running service afterward):
   ```bash
   docker compose restart odoo
   ```
5. Go to **Tally Integration > Configuration** and create one
   `tally.integration.config` record per company -- see
   [CONFIGURATION.md](CONFIGURATION.md).
6. Use **Test RabbitMQ Connection** on that record to confirm connectivity
   before turning on **Synchronization Enabled**.

## Upgrading

After pulling in code changes to this module:

```bash
docker compose exec odoo odoo -d <your-db> -u odoo_tally_rabbitmq --stop-after-init
docker compose restart odoo
```

The `-u` step reloads views/security/data and runs any migrations; the
restart is what makes the live server's Python process pick up edited
`.py` files (Odoo does not hot-reload source in an already-running worker).

## Running the test suite

```bash
docker compose exec odoo odoo -d <your-db> -u odoo_tally_rabbitmq \
  --test-enable --test-tags /odoo_tally_rabbitmq \
  --http-port=8169 --gevent-port=8172 --stop-after-init
```

The alternate `--http-port`/`--gevent-port` avoid clashing with a
live server already bound to 8069/8072. `tests/test_rabbitmq_integration.py`
talks to the real RabbitMQ broker rather than mocking it -- it skips itself
(rather than failing) if the broker at `rabbitmq:5672` is unreachable.
