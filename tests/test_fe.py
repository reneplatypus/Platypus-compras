import glob
import os
import unittest
from decimal import Decimal
from pathlib import Path

from compras.costing import purchase_amounts
from compras.fe_parser import InvoiceParseError, parse
from compras.validate import validate

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLES = os.environ.get("COMPRAS_SAMPLES", "/opt/interfuerza/samples")
D = Decimal


def load(name):
    return parse((FIXTURES / name).read_bytes())


class ParseTest(unittest.TestCase):
    def test_wrapped_invoice_with_namespaces(self):
        inv = load("factura_isc_oti.xml")
        self.assertEqual(inv.cufe, "FE01200000000000-1-000001-0000002026101500000012340010110000000001")
        self.assertEqual(inv.doc_type, "01")
        self.assertFalse(inv.is_credit)
        self.assertEqual(inv.issuer_ruc, "0000000-1-000001")
        self.assertEqual(inv.receiver_ruc, "155687144-2-2019")
        self.assertEqual(len(inv.lines), 2)
        first = inv.lines[0]
        self.assertEqual((first.net, first.itbms, first.isc, first.oti), (D("25.00"), D("1.75"), D("1.25"), D("0.02")))
        self.assertEqual(first.cpbs, "8311")
        self.assertEqual(inv.oti_total, D("0.02"))
        self.assertEqual(inv.due_dates[0][1], D("32.02"))
        self.assertTrue(inv.authorized)

    def test_credit_note_reference(self):
        inv = load("nota_credito_otro_ruc.xml")
        self.assertTrue(inv.is_credit)
        self.assertEqual(inv.referenced_cufes, ["FE01200000000000-1-000001-0000002026101500000012340010110000000001"])
        self.assertFalse(inv.authorized)

    def test_rejects_non_invoice_xml(self):
        with self.assertRaises(InvoiceParseError):
            parse(b"<factura><total>1</total></factura>")
        with self.assertRaises(InvoiceParseError):
            parse(b"<rFE><dId>")


class CostingTest(unittest.TestCase):
    def test_isc_and_oti_folded_into_line_cost(self):
        amounts = purchase_amounts(load("factura_isc_oti.xml"))
        self.assertEqual(amounts.itbms, D("1.75"))
        self.assertEqual([ln.cost for ln in amounts.lines], [D("26.27"), D("4.00")])
        self.assertEqual(amounts.lines[0].folded_taxes, D("1.27"))
        self.assertEqual(amounts.subtotal + amounts.itbms, amounts.total)
        self.assertEqual(amounts.residual, D("0.00"))

    def test_header_discount_spread_over_lines(self):
        amounts = purchase_amounts(load("nota_credito_otro_ruc.xml"))
        self.assertEqual(amounts.residual, D("-1.00"))
        self.assertEqual([ln.cost for ln in amounts.lines], [D("9.67"), D("19.33")])
        self.assertEqual(sum(ln.cost for ln in amounts.lines), amounts.subtotal)


class ValidateTest(unittest.TestCase):
    def test_clean_invoice(self):
        self.assertEqual(validate(load("factura_isc_oti.xml")), [])

    def test_wrong_receiver_and_missing_protocol(self):
        codes = {i.code for i in validate(load("nota_credito_otro_ruc.xml"))}
        self.assertEqual(codes, {"wrong_receiver", "no_protocol"})


@unittest.skipUnless(os.path.isdir(SAMPLES), "no real samples on this machine")
class RealSamplesTest(unittest.TestCase):
    """Every downloaded supplier XML must parse and reconcile to the cent."""

    def test_samples(self):
        paths = sorted(glob.glob(os.path.join(SAMPLES, "*", "*.xml")))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(sample=os.path.basename(os.path.dirname(path))):
                inv = parse(Path(path).read_bytes())
                amounts = purchase_amounts(inv)
                self.assertEqual(sum(ln.cost for ln in amounts.lines) + amounts.itbms, amounts.total)
                warnings = [i.code for i in validate(inv) if i.code != "wrong_receiver" and i.severity == "warning"]
                self.assertNotIn("total_mismatch", warnings)


if __name__ == "__main__":
    unittest.main()
