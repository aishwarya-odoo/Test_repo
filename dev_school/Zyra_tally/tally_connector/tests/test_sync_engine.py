import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sync_engine import SyncEngine, strip_tally_code_suffix, to_tally_date


class FakeConfig:
    odoo_company_id = 1


class TestHelpers(unittest.TestCase):
    def test_strip_tally_code_suffix(self):
        self.assertEqual(strip_tally_code_suffix("Acme Corp [ CRD00002]"), "Acme Corp")
        self.assertEqual(strip_tally_code_suffix("Plain Name"), "Plain Name")
        self.assertIsNone(strip_tally_code_suffix(None))

    def test_to_tally_date(self):
        self.assertEqual(to_tally_date("2026-04-01"), "20260401")
        self.assertEqual(to_tally_date(None), __import__("datetime").date.today().strftime("%Y%m%d"))


class TestOutboundDispatch(unittest.TestCase):
    def setUp(self):
        self.client = mock.MagicMock()
        self.engine = SyncEngine(FakeConfig(), self.client)
        self.rabbitmq = mock.MagicMock()

    def test_customer_created_pushes_ledger_under_sundry_debtors(self):
        self.client.find_by_name.return_value = {"GUID": "guid-1"}
        envelope = {"event_type": "customer.created", "data": {"name": "ABC Co", "phone": "123"}}
        result = self.engine.handle_outbound_event(envelope, self.rabbitmq)
        self.assertTrue(result)
        self.client.push_ledger.assert_called_once()
        args, kwargs = self.client.push_ledger.call_args
        self.assertEqual(args[0], "ABC Co")
        self.assertEqual(args[1], "Sundry Debtors")
        self.assertEqual(kwargs["action"], "Create")

    def test_vendor_updated_pushes_ledger_under_sundry_creditors_with_alter(self):
        self.client.find_by_name.return_value = {"GUID": "guid-2"}
        envelope = {"event_type": "vendor.updated", "data": {"name": "XYZ Supplier"}}
        self.engine.handle_outbound_event(envelope, self.rabbitmq)
        args, kwargs = self.client.push_ledger.call_args
        self.assertEqual(args[1], "Sundry Creditors")
        self.assertEqual(kwargs["action"], "Alter")

    def test_missing_name_does_not_push(self):
        envelope = {"event_type": "customer.created", "data": {}}
        result = self.engine.handle_outbound_event(envelope, self.rabbitmq)
        self.assertFalse(result)
        self.client.push_ledger.assert_not_called()

    def test_unknown_event_type_is_dropped_without_error(self):
        envelope = {"event_type": "something.unexpected", "data": {}}
        result = self.engine.handle_outbound_event(envelope, self.rabbitmq)
        self.assertTrue(result)

    def test_confirm_publishes_tally_id_after_successful_push(self):
        self.client.find_by_name.return_value = {"GUID": "guid-123", "name": "ABC Co"}
        envelope = {"event_type": "customer.created", "data": {"name": "ABC Co"}}
        self.engine.handle_outbound_event(envelope, self.rabbitmq)
        self.rabbitmq.publish.assert_called_once()
        routing_key, published_envelope = self.rabbitmq.publish.call_args[0][:2]
        self.assertEqual(routing_key, "tally.customer.updated")
        self.assertEqual(published_envelope["data"]["tally_id"], "guid-123")

    def test_confirm_lookup_failure_does_not_crash_the_push(self):
        self.client.find_by_name.side_effect = RuntimeError("network blip")
        envelope = {"event_type": "customer.created", "data": {"name": "ABC Co"}}
        result = self.engine.handle_outbound_event(envelope, self.rabbitmq)
        self.assertTrue(result)
        self.rabbitmq.publish.assert_not_called()

    def test_product_created_pushes_stock_item(self):
        self.client.find_by_name.return_value = {"GUID": "guid-9"}
        envelope = {"event_type": "product.created", "data": {"name": "Widget", "uom": "Units"}}
        result = self.engine.handle_outbound_event(envelope, self.rabbitmq)
        self.assertTrue(result)
        self.client.push_stock_item.assert_called_once_with("Widget", "Units", action="Create", hsn_code=None)

    def test_sales_invoice_pushes_voucher_with_lines(self):
        envelope = {
            "event_type": "sales_invoice.created",
            "data": {
                "partner": {"name": "ABC Co"}, "amount_total": 118.0, "invoice_date": "2026-04-01",
                "lines": [{"product": "Widget", "quantity": 2, "unit_price": 50, "subtotal": 100}],
            },
        }
        result = self.engine.handle_outbound_event(envelope, self.rabbitmq)
        self.assertTrue(result)
        self.client.push_sales_purchase_voucher.assert_called_once()
        args, kwargs = self.client.push_sales_purchase_voucher.call_args
        self.assertEqual(args[0], "Sales")
        self.assertEqual(kwargs["party_ledger"], "ABC Co")
        self.assertEqual(kwargs["line_ledger"], "Sales Accounts")

    def test_customer_payment_pushes_receipt_voucher(self):
        envelope = {
            "event_type": "customer_payment.created",
            "data": {"partner": {"name": "ABC Co"}, "amount": 100.0, "journal": "Cash", "date": "2026-04-01"},
        }
        result = self.engine.handle_outbound_event(envelope, self.rabbitmq)
        self.assertTrue(result)
        self.client.push_payment_receipt_voucher.assert_called_once()
        args, kwargs = self.client.push_payment_receipt_voucher.call_args
        self.assertEqual(args[0], "Receipt")
        self.assertEqual(kwargs["bank_ledger"], "Cash")


class TestInboundClassify(unittest.TestCase):
    def setUp(self):
        self.client = mock.MagicMock()
        self.engine = SyncEngine(FakeConfig(), self.client)

    def test_ledger_under_sundry_debtors_becomes_customer_event(self):
        record = {"name": "Acme Corp [ CRD00002]", "PARENT": "Sundry Debtors", "GUID": "g1",
                  "PARTYGSTIN": None, "ADDRESS.LIST": ["Line1", "Line2"], "PINCODE": "682030",
                  "LEDGERMOBILE": None, "EMAIL": None}
        events = list(self.engine._classify("ledger", record))
        self.assertEqual(len(events), 1)
        event_type, data = events[0]
        self.assertEqual(event_type, "tally.customer.updated")
        self.assertEqual(data["name"], "Acme Corp")
        self.assertEqual(data["street"], "Line1")
        self.assertEqual(data["street2"], "Line2")

    def test_ledger_under_unrelated_group_is_ignored(self):
        record = {"name": "Sales Accounts", "PARENT": "Income", "GUID": "g2"}
        events = list(self.engine._classify("ledger", record))
        self.assertEqual(events, [])

    def test_voucher_classification_by_type(self):
        for vch_type, expected_event in (
            ("Sales", "tally.sales_invoice.created"),
            ("Purchase", "tally.purchase_invoice.created"),
            ("Receipt", "tally.receipt.created"),
            ("Payment", "tally.payment.created"),
        ):
            record = {"VOUCHERTYPENAME": vch_type, "PARTYLEDGERNAME": "ABC Co", "GUID": "g3",
                      "VOUCHERNUMBER": "1", "AMOUNT": -100.0}
            events = list(self.engine._classify("voucher", record))
            self.assertEqual(events[0][0], expected_event, vch_type)

    def test_unrecognized_voucher_type_is_ignored(self):
        record = {"VOUCHERTYPENAME": "Journal", "PARTYLEDGERNAME": "ABC Co", "GUID": "g4"}
        events = list(self.engine._classify("voucher", record))
        self.assertEqual(events, [])


class TestPollAndPublish(unittest.TestCase):
    def setUp(self):
        self.client = mock.MagicMock()
        self.engine = SyncEngine(FakeConfig(), self.client)
        self.rabbitmq = mock.MagicMock()
        self.state_store = mock.MagicMock()
        self.state_store.get_last_alterid.return_value = 100

    def test_publishes_events_and_advances_cursor(self):
        self.client.poll_changes.return_value = [
            {"name": "Acme Corp", "PARENT": "Sundry Debtors", "GUID": "g1", "ALTERID": 105},
            {"name": "Unrelated", "PARENT": "Income", "GUID": "g2", "ALTERID": 110},
        ]
        published = self.engine.poll_and_publish("ledger", self.state_store, self.rabbitmq)
        self.assertEqual(published, 1)
        self.rabbitmq.publish.assert_called_once()
        self.state_store.set_last_alterid.assert_called_with("ledger", 110)

    def test_deleted_records_are_skipped_but_still_advance_cursor(self):
        self.client.poll_changes.return_value = [
            {"name": "Gone", "PARENT": "Sundry Debtors", "GUID": "g1", "ALTERID": 120, "ISDELETED": True},
        ]
        published = self.engine.poll_and_publish("ledger", self.state_store, self.rabbitmq)
        self.assertEqual(published, 0)
        self.rabbitmq.publish.assert_not_called()
        self.state_store.set_last_alterid.assert_called_with("ledger", 120)


if __name__ == "__main__":
    unittest.main()
