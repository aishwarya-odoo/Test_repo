import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state_store import StateStore


class TestStateStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "nested", "state.json")

    def test_missing_file_starts_at_zero(self):
        store = StateStore(self.path)
        self.assertEqual(store.get_last_alterid("ledger"), 0)

    def test_set_and_persist_across_instances(self):
        store = StateStore(self.path)
        store.set_last_alterid("ledger", 42)
        self.assertTrue(os.path.exists(self.path))

        reopened = StateStore(self.path)
        self.assertEqual(reopened.get_last_alterid("ledger"), 42)

    def test_categories_are_independent(self):
        store = StateStore(self.path)
        store.set_last_alterid("ledger", 10)
        store.set_last_alterid("voucher", 20)
        self.assertEqual(store.get_last_alterid("ledger"), 10)
        self.assertEqual(store.get_last_alterid("voucher"), 20)

    def test_corrupt_file_starts_fresh_instead_of_crashing(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            f.write("{not valid json")
        store = StateStore(self.path)
        self.assertEqual(store.get_last_alterid("ledger"), 0)


if __name__ == "__main__":
    unittest.main()
