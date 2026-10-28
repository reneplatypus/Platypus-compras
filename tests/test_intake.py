import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

from compras.intake import process_message
from compras.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
INVOICE = (FIXTURES / "factura_isc_oti.xml").read_bytes()
OTHER_RUC = (FIXTURES / "nota_credito_otro_ruc.xml").read_bytes()


def email_with(*files):
    msg = EmailMessage()
    msg["From"] = "facturas@proveedor.example"
    msg["Subject"] = "Factura electrónica"
    msg["Message-ID"] = "<test@example>"
    msg.set_content("Adjunto su factura")
    for name, data, subtype in files:
        msg.add_attachment(data, maintype="application", subtype=subtype, filename=name)
    return msg.as_bytes()


class IntakeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "test.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_files_xml_and_pdf_into_unposted(self):
        raw = email_with(("FE01.xml", INVOICE, "xml"), ("FE01.pdf", b"%PDF-1.4", "pdf"))
        out = process_message(raw, "111", self.store, self.dir)
        self.assertEqual(out.kind, "filed")
        row = self.store.invoice_by_cufe(out.cufes[0])
        self.assertEqual(row["status"], "needs_review")
        folder = Path(row["folder"])
        self.assertEqual(folder.relative_to(self.dir).parts[:3], ("unposted", "2026", "10"))
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["FE01.pdf", "FE01.xml"])
        self.assertTrue(self.store.seen_message("111"))

    def test_same_cufe_twice_is_duplicate(self):
        raw = email_with(("a.xml", INVOICE, "xml"))
        process_message(raw, "111", self.store, self.dir)
        out = process_message(raw, "222", self.store, self.dir)
        self.assertEqual(out.kind, "duplicate")
        self.assertEqual(self.store.counts(), {"needs_review": 1})

    def test_not_addressed_to_platypus_is_rejected(self):
        out = process_message(email_with(("nc.xml", OTHER_RUC, "xml")), "333", self.store, self.dir)
        row = self.store.invoice_by_cufe(out.cufes[0])
        self.assertEqual(row["status"], "rejected")
        self.assertIn("8-000-000", row["status_reason"])
        self.assertEqual(Path(row["folder"]).relative_to(self.dir).parts[0], "rejected")

    def test_unparseable_xml_goes_to_errors(self):
        out = process_message(email_with(("x.xml", b"<otro/>", "xml")), "444", self.store, self.dir)
        self.assertEqual(out.kind, "error")
        self.assertTrue((self.dir / "errors" / "444" / "error.json").is_file())


if __name__ == "__main__":
    unittest.main()
