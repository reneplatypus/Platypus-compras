import json
import os
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from compras import auth, review
from compras.intake import process_message
from compras.store import Store
from tests.test_intake import INVOICE, email_with

ACCOUNTS = [
    {"Internal_Code": "ACC00014", "Codigo": "6001", "Name": "Telecomunicaciones", "Type": "Expenses", "Inactive": "NO"},
    {"Internal_Code": "ACC00001", "Codigo": "5005", "Name": "Servicios de Instalación", "Type": "Costs Of Sales",
     "Inactive": "NO"},
    {"Internal_Code": "ACC00099", "Codigo": "6999", "Name": "Vieja", "Type": "Expenses", "Inactive": "YES"},
]
PROVIDERS = {"P0100": "Proveedor de Prueba, S.A."}
PURCHASES = [{"Purchase": {"id": "1", "Proveedor": "P0100"}, "Lines": [{"Account": "ACC00014"}, {"Account": "ACC00014"},
                                                                       {"Account": "ACC00099"}]}]


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.docs = self.dir / "documents"
        cache = self.dir / "interfuerza"
        cache.mkdir()
        (cache / "accounts.json").write_text(json.dumps(ACCOUNTS))
        (cache / "providers.json").write_text(json.dumps(PROVIDERS))
        (cache / "purchases.json").write_text(json.dumps(PURCHASES))
        self.master = review.MasterData(cache)
        self.store = Store(self.dir / "t.db")
        out = process_message(email_with(("f.xml", INVOICE, "xml")), "1", self.store, self.docs)
        self.id = self.store.invoice_by_cufe(out.cufes[0])["id"]
        self.store.update_invoice(self.id, if_state="not_posted")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def map_provider(self):
        review.set_provider(self.store, self.master, self.id, "laura@x", "P0100")

    def test_suggestion_comes_from_provider_history_and_skips_inactive(self):
        self.map_provider()
        info = review.detail(self.store, self.master, self.id)
        self.assertEqual(info["suggested"], ["ACC00014"])
        self.assertEqual([ln.parts for ln in info["lines"]], [[("ACC00014", D("26.27"))], [("ACC00014", D("4.00"))]])

    def test_split_line_across_expense_and_cost_of_sales_then_approve(self):
        self.map_provider()
        parts = review.parse_parts([("account_1", "ACC00014"), ("amount_1", "20.00"),
                                    ("account_1", "ACC00001"), ("amount_1", "6.27"),
                                    ("account_2", "ACC00001"), ("amount_2", "4.00")])
        review.save_lines(self.store, self.master, self.id, "laura@x", parts)
        review.approve(self.store, self.master, self.id, "laura@x", self.docs)
        row = self.store.invoice(self.id)
        self.assertEqual((row["status"], row["approved_by"]), ("approved", "laura@x"))
        self.assertEqual([(l["seq"], l["part"], l["account"]) for l in self.store.lines(self.id)],
                         [(1, 1, "ACC00014"), (1, 2, "ACC00001"), (2, 1, "ACC00001")])
        with self.assertRaises(review.ReviewError):
            review.save_lines(self.store, self.master, self.id, "laura@x", parts)   # locked once approved

    def test_parts_must_add_up_to_line_cost(self):
        parts = review.parse_parts([("account_1", "ACC00014"), ("amount_1", "20.00"),
                                    ("account_2", "ACC00001"), ("amount_2", "4.00")])
        with self.assertRaisesRegex(review.ReviewError, "Línea 1: las partes suman 20.00"):
            review.save_lines(self.store, self.master, self.id, "laura@x", parts)

    def test_approve_needs_provider_saved_lines_and_no_posted_match(self):
        with self.assertRaisesRegex(review.ReviewError, "Guarde"):
            review.approve(self.store, self.master, self.id, "laura@x", self.docs)
        parts = [(1, "ACC00014", D("26.27")), (2, "ACC00014", D("4.00"))]
        review.save_lines(self.store, self.master, self.id, "laura@x", parts)
        with self.assertRaisesRegex(review.ReviewError, "proveedor"):
            review.approve(self.store, self.master, self.id, "laura@x", self.docs)
        self.map_provider()
        self.store.update_invoice(self.id, if_state="possible", if_purchase_id="00010")
        with self.assertRaisesRegex(review.ReviewError, "InterFuerza podría"):
            review.approve(self.store, self.master, self.id, "laura@x", self.docs)
        review.dismiss_match(self.store, self.id, "laura@x")
        review.approve(self.store, self.master, self.id, "laura@x", self.docs)
        self.assertEqual(self.store.invoice(self.id)["if_dismissed"], "00010")

    def test_reject_and_reopen_move_folders(self):
        review.reject(self.store, self.id, "laura@x", "Gasto personal", self.docs)
        row = self.store.invoice(self.id)
        self.assertEqual(Path(row["folder"]).relative_to(self.docs).parts[0], "rejected")
        self.assertTrue(all(Path(d["path"]).is_file() for d in self.store.documents(self.id)))
        review.reopen(self.store, self.id, "laura@x", self.docs)
        self.assertEqual(Path(self.store.invoice(self.id)["folder"]).relative_to(self.docs).parts[0], "unposted")

    def test_mark_posted_moves_to_posted(self):
        review.mark_posted(self.store, self.id, "laura@x", "02038", self.docs)
        row = self.store.invoice(self.id)
        self.assertEqual((row["status"], row["posted_via"], row["if_purchase_id"]), ("imported", "manual", "02038"))
        self.assertEqual(Path(row["folder"]).relative_to(self.docs).parts[0], "posted")


class AuthTest(unittest.TestCase):
    def setUp(self):
        os.environ["COMPRAS_SECRET"] = "x" * 40
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.store.upsert_user("laura@platypusinc.net", "Laura Pineda", "approver")
        self.sent = []

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def send(self, email, code):
        self.sent.append((email, code))

    def test_code_login_and_session(self):
        auth.request_code(self.store, "Laura@Platypusinc.net ", send=self.send)
        email, code = self.sent[0]
        self.assertIsNone(auth.verify_code(self.store, email, "000000" if code != "000000" else "111111"))
        token = auth.verify_code(self.store, email, code)
        self.assertEqual(auth.user_for_token(self.store, token)["name"], "Laura Pineda")
        self.assertIsNone(auth.verify_code(self.store, email, code))     # single use
        auth.logout(self.store, token)
        self.assertIsNone(auth.user_for_token(self.store, token))

    def test_unknown_email_gets_nothing(self):
        auth.request_code(self.store, "nadie@example.com", send=self.send)
        self.assertEqual(self.sent, [])

    def test_attempts_are_limited(self):
        auth.request_code(self.store, "laura@platypusinc.net", send=self.send)
        code = self.sent[0][1]
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(auth.MAX_ATTEMPTS):
            auth.verify_code(self.store, "laura@platypusinc.net", wrong)
        self.assertIsNone(auth.verify_code(self.store, "laura@platypusinc.net", code))


if __name__ == "__main__":
    unittest.main()
