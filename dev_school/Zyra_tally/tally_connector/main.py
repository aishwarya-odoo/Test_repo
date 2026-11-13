"""Entry point. Run as:
    python main.py outbound   # consumes tally.odoo.* -> pushes to Tally
    python main.py inbound    # polls Tally -> publishes tally.incoming.*
Each runs forever in its own process; supervisord.conf runs both as
separate programs so either can crash/restart independently."""
import logging
import sys

from config import config
from state_store import StateStore
from sync_engine import SyncEngine
from tally_client import TallyClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("outbound", "inbound"):
        print(__doc__)
        sys.exit(1)

    client = TallyClient(config)
    engine = SyncEngine(config, client)

    if sys.argv[1] == "outbound":
        import outbound_worker
        outbound_worker.run_forever(config, engine)
    else:
        import inbound_worker
        state_store = StateStore(config.state_file_path)
        inbound_worker.run_forever(config, engine, state_store)


if __name__ == "__main__":
    main()
