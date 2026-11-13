"""Opt-in integration tests against the REAL TallyPrime server -- read-only.
Skipped unless RUN_LIVE_TALLY_TESTS=1 is set, so the normal test run never
depends on network/a live external system. Run explicitly with:

    RUN_LIVE_TALLY_TESTS=1 TALLY_GATEWAY_URL=http://192.168.2.55:9000/ \\
        TALLY_COMPANY_NAME="PearlSoft Logistics" python3 -m unittest tests.test_live_tally

This intentionally only exercises the read/poll side. Verifying the
write/import side against a real company is a deliberate, one-off action
(see TALLY_INTEGRATION.md) -- not something a routine test run should do
every time, since it creates real records."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if os.environ.get("RUN_LIVE_TALLY_TESTS") != "1":
    raise unittest.SkipTest("RUN_LIVE_TALLY_TESTS not set -- skipping live Tally integration tests")

from config import config
from tally_client import TallyClient


class TestLiveTallyRead(unittest.TestCase):
    def setUp(self):
        self.client = TallyClient(config)

    def test_poll_ledgers_returns_real_records(self):
        records = self.client.poll_changes("ledger", 0)
        self.assertGreater(len(records), 0)
        named = [r for r in records if r.get("name")]
        self.assertTrue(any(r.get("PARENT") == "Sundry Debtors" for r in named))

    def test_poll_stock_items_handles_placeholder_charrefs_without_crashing(self):
        records = self.client.poll_changes("stock_item", 0)
        self.assertGreater(len(records), 0)

    def test_find_by_name_locates_a_known_test_record(self):
        record = self.client.find_by_name("ledger", "Connector Test Customer 1")
        self.assertIsNotNone(record)
        self.assertEqual(record["PARENT"], "Sundry Debtors")

    def test_poll_since_future_alterid_returns_nothing_new(self):
        records = self.client.poll_changes("ledger", 10 ** 9)
        self.assertEqual(records, [], "no ledger should have an ALTERID beyond a billion")


if __name__ == "__main__":
    unittest.main()
