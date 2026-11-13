"""Offline tests for tally_xml.py -- no network calls. The canned XML
strings below are real responses captured against a live TallyPrime server
during development (see odoo_tally_rabbitmq/TALLY_INTEGRATION.md), not
hand-invented, so a regression here means the parser genuinely diverged
from what Tally actually sends."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tally_xml

_REAL_CREATE_SUCCESS = """<RESPONSE>
 <CREATED>1</CREATED>
 <ALTERED>0</ALTERED>
 <DELETED>0</DELETED>
 <LASTVCHID>0</LASTVCHID>
 <LASTMID>0</LASTMID>
 <COMBINED>0</COMBINED>
 <IGNORED>0</IGNORED>
 <ERRORS>0</ERRORS>
 <CANCELLED>0</CANCELLED>
</RESPONSE>"""

_REAL_CREATE_FAILURE = """<RESPONSE>
 <LINEERROR>No Valid Names!</LINEERROR>
 <CREATED>0</CREATED>
 <ALTERED>0</ALTERED>
 <DELETED>0</DELETED>
 <LASTVCHID>0</LASTVCHID>
 <LASTMID>0</LASTMID>
 <COMBINED>0</COMBINED>
 <IGNORED>0</IGNORED>
 <ERRORS>1</ERRORS>
 <CANCELLED>0</CANCELLED>
</RESPONSE>"""

_UNKNOWN_REQUEST = "<RESPONSE>Unknown Request, cannot be processed</RESPONSE>"

_REAL_LEDGER_COLLECTION = """<ENVELOPE>
 <HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER>
 <BODY><DESC><CMPINFO></CMPINFO></DESC>
  <DATA>
   <COLLECTION ISMSTDEPTYPE="Yes" MSTDEPTYPE="8">
    <LEDGER NAME="Connector Test Customer 1" RESERVEDNAME="">
     <ADDRESS.LIST TYPE="String">
      <ADDRESS>456 Industrial Area</ADDRESS>
      <ADDRESS>Ernakulam</ADDRESS>
     </ADDRESS.LIST>
     <GUID TYPE="String">218173f3-4b3c-4fcb-be0f-41367b9e67b1-000000fb</GUID>
     <EMAIL TYPE="String">connector-test@example.com</EMAIL>
     <PARENT TYPE="String">Sundry Debtors</PARENT>
     <LEDGERMOBILE TYPE="String">9999999999</LEDGERMOBILE>
     <ISDELETED TYPE="Logical"></ISDELETED>
     <ALTERID TYPE="Number"> 741</ALTERID>
     <OPENINGBALANCE TYPE="Amount">₹ 0.00</OPENINGBALANCE>
     <LANGUAGENAME.LIST>
      <NAME.LIST TYPE="String"><NAME>Connector Test Customer 1</NAME></NAME.LIST>
      <LANGUAGEID TYPE="Number"> 1033</LANGUAGEID>
     </LANGUAGENAME.LIST>
    </LEDGER>
   </COLLECTION>
  </DATA>
 </BODY>
</ENVELOPE>"""

# &#4; below is real, verified-on-the-wire bytes -- TallyPrime emits an
# invalid-per-XML-1.0 control character reference as its "not set"
# placeholder marker. See _strip_invalid_xml_charrefs in tally_xml.py.
_REAL_STOCKITEM_WITH_PLACEHOLDERS = """<ENVELOPE>
 <BODY><DATA>
  <COLLECTION>
   <STOCKITEM NAME="Adhesive Compound" RESERVEDNAME="">
    <GUID TYPE="String">218173f3-4b3c-4fcb-be0f-41367b9e67b1-000000c3</GUID>
    <BASEUNITS TYPE="String">L</BASEUNITS>
    <ISDELETED TYPE="Logical"></ISDELETED>
    <ALTERID TYPE="Number"> 721</ALTERID>
    <GSTHSNCODE TYPE="String">&#4; Not Found</GSTHSNCODE>
   </STOCKITEM>
  </COLLECTION>
 </DATA></BODY>
</ENVELOPE>"""


class TestParseImportResponse(unittest.TestCase):
    def test_parses_successful_create(self):
        result = tally_xml.parse_import_response(_REAL_CREATE_SUCCESS)
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["errors"], 0)

    def test_parses_business_error(self):
        result = tally_xml.parse_import_response(_REAL_CREATE_FAILURE)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["line_error"], "No Valid Names!")

    def test_parses_unknown_request(self):
        result = tally_xml.parse_import_response(_UNKNOWN_REQUEST)
        self.assertEqual(result["errors"], 1)
        self.assertIn("Unknown Request", result["line_error"])


class TestParseCollectionResponse(unittest.TestCase):
    def test_parses_ledger_fields(self):
        records = tally_xml.parse_collection_response(_REAL_LEDGER_COLLECTION, "LEDGER")
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["name"], "Connector Test Customer 1")
        self.assertEqual(record["GUID"], "218173f3-4b3c-4fcb-be0f-41367b9e67b1-000000fb")
        self.assertEqual(record["PARENT"], "Sundry Debtors")
        self.assertEqual(record["EMAIL"], "connector-test@example.com")
        self.assertEqual(record["LEDGERMOBILE"], "9999999999")
        self.assertEqual(record["ALTERID"], 741)
        self.assertEqual(record["ADDRESS.LIST"], ["456 Industrial Area", "Ernakulam"])
        self.assertIsNone(record["ISDELETED"])

    def test_placeholder_text_normalized_to_none(self):
        records = tally_xml.parse_collection_response(_REAL_STOCKITEM_WITH_PLACEHOLDERS, "STOCKITEM")
        self.assertIsNone(records[0]["GSTHSNCODE"])
        self.assertEqual(records[0]["BASEUNITS"], "L")


class TestBuildEnvelopes(unittest.TestCase):
    def test_import_envelope_escapes_and_wraps(self):
        envelope = tally_xml.build_import_envelope("A & B Co", "<LEDGER NAME=\"X\"/>")
        self.assertIn("A &amp; B Co", envelope)
        self.assertIn("<LEDGER NAME=\"X\"/>", envelope)
        self.assertIn("<TALLYREQUEST>Import Data</TALLYREQUEST>", envelope)

    def test_ledger_xml_includes_name_list(self):
        xml = tally_xml.build_ledger_xml("Acme & Co", "Sundry Debtors", mobile="123", email="a@b.com")
        self.assertIn("<NAME.LIST><NAME>Acme &amp; Co</NAME></NAME.LIST>", xml)
        self.assertIn("<PARENT>Sundry Debtors</PARENT>", xml)
        self.assertIn("<LEDGERMOBILE>123</LEDGERMOBILE>", xml)

    def test_stock_item_xml_includes_name_list(self):
        xml = tally_xml.build_stock_item_xml("Widget", "Units")
        self.assertIn("<NAME.LIST><NAME>Widget</NAME></NAME.LIST>", xml)
        self.assertIn("<BASEUNITS>Units</BASEUNITS>", xml)

    def test_collection_envelope_with_filter(self):
        envelope = tally_xml.build_collection_envelope(
            "PearlSoft Logistics", "MyLedgers", "Ledger", ["NAME", "ALTERID"],
            filter_formula="$AlterID > 100",
        )
        self.assertIn("$AlterID &gt; 100", envelope)
        self.assertIn("<FILTER>ConnectorFilter</FILTER>", envelope)
        self.assertIn("<FETCH>NAME,ALTERID</FETCH>", envelope)


class TestCleanAmount(unittest.TestCase):
    def test_strips_currency_symbol_and_parses_positive(self):
        self.assertEqual(tally_xml._clean_amount("₹ 56.00"), 56.0)

    def test_negative_amount(self):
        self.assertEqual(tally_xml._clean_amount("-₹ 56.00"), -56.0)

    def test_blank_defaults_to_zero(self):
        self.assertEqual(tally_xml._clean_amount(""), 0.0)
        self.assertEqual(tally_xml._clean_amount(None), 0.0)


if __name__ == "__main__":
    unittest.main()
