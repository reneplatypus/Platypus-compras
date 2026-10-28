"""End-to-end through the FastAPI app (async form endpoints included), on a throwaway data folder."""
import importlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from tests.test_intake import INVOICE, email_with
from tests.test_review import ACCOUNTS, PROVIDERS, PURCHASES

try:
    from fastapi.testclient import TestClient
except (ImportError, RuntimeError):   # web dependencies (fastapi, httpx) live in .venv
    TestClient = None


@unittest.skipIf(TestClient is None, "run with .venv/bin/python for web tests")
class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name)
        os.environ.update(COMPRAS_DATA_DIR=str(data), COMPRAS_SECRET="s" * 40, COMPRAS_ENV_FILE=str(data / "none"))
        cache = data / "interfuerza"
        cache.mkdir()
        (cache / "accounts.json").write_text(json.dumps(ACCOUNTS))
        (cache / "providers.json").write_text(json.dumps(PROVIDERS))
        (cache / "purchases.json").write_text(json.dumps(PURCHASES))

        from compras import auth
        from compras.intake import process_message
        from compras.store import Store
        import compras.web.app as web
        cls.web = importlib.reload(web)
        store = Store(data / "compras.db", actor="test")
        out = process_message(email_with(("f.xml", INVOICE, "xml")), "1", store, data / "documents")
        cls.invoice_id = store.invoice_by_cufe(out.cufes[0])["id"]
        store.update_invoice(cls.invoice_id, if_state="not_posted")
        store.set_supplier(store.invoice(cls.invoice_id)["issuer_ruc"], "PROVEEDOR", "P0100", "Proveedor", "manual", 1)
        store.upsert_user("rene@platypusinc.net", "René", "approver")
        store.commit()
        codes = []
        auth.request_code(store, "rene@platypusinc.net", send=lambda e, c: codes.append(c))
        cls.token = auth.verify_code(store, "rene@platypusinc.net", codes[0])
        store.close()
        cls.db = data / "compras.db"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def client(self):
        c = TestClient(self.web.app, base_url="http://testserver", follow_redirects=False)
        c.cookies.set(self.web.COOKIE, self.token)
        return c

    def post(self, path, data, origin="http://testserver"):
        # Encoded by hand: split lines repeat field names, which httpx's data= drops when given as pairs.
        pairs = data.items() if isinstance(data, dict) else data
        return self.client().post(path, content=urlencode(list(pairs)), headers={
            "Origin": origin, "Content-Type": "application/x-www-form-urlencoded"})

    def test_1_save_lines_then_approve_through_the_app(self):
        i = self.invoice_id
        r = self.post(f"/facturas/{i}/lineas", {"account_1": "ACC00014", "amount_1": "26.27",
                                                 "account_2": "ACC00014", "amount_2": "4.00"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("ok=", r.headers["location"])
        r = self.post(f"/facturas/{i}/aprobar", {"account_1": "ACC00014", "amount_1": "20.00",
                                                  "account_2": "ACC00014", "amount_2": "4.00"})
        self.assertIn("error=", r.headers["location"])            # 20.00 ≠ 26.27: refused, nothing saved
        r = self.post(f"/facturas/{i}/aprobar", [("account_1", "ACC00014"), ("amount_1", "20.00"),
                                                  ("account_1", "ACC00001"), ("amount_1", "6.27"),
                                                  ("account_2", "ACC00001"), ("amount_2", "4.00")])
        self.assertIn("ok=", r.headers["location"])
        db = sqlite3.connect(self.db)
        self.assertEqual(db.execute("SELECT status, approved_by FROM invoices WHERE id = ?", (i,)).fetchone(),
                         ("approved", "rene@platypusinc.net"))
        actions = [a for (a,) in db.execute("SELECT action FROM audit WHERE invoice_id = ? ORDER BY id", (i,))]
        self.assertEqual((actions[0], actions[-1], actions.count("lineas")), ("alta", "aprobar", 2))
        users = {u for (u,) in db.execute("SELECT user_email FROM audit WHERE action IN ('lineas', 'aprobar')")}
        self.assertEqual(users, {"rene@platypusinc.net"})
        page = self.client().get(f"/facturas/{i}").text
        self.assertIn("status: needs_review → approved", page)

    def test_2_cross_origin_post_is_ignored(self):
        r = self.post(f"/facturas/{self.invoice_id}/reabrir", {}, origin="http://evil.example")
        self.assertEqual(r.headers["location"], "/")
        status = sqlite3.connect(self.db).execute("SELECT status FROM invoices WHERE id = ?",
                                                  (self.invoice_id,)).fetchone()[0]
        self.assertEqual(status, "approved")

    def test_3_audit_page_reports_intact_chain(self):
        page = self.client().get("/auditoria").text
        self.assertIn("Cadena de auditoría íntegra", page)

    def test_4_login_required(self):
        r = TestClient(self.web.app, follow_redirects=False).get("/facturas")
        self.assertEqual(r.headers["location"], "/ingreso")


class AuditChainTest(unittest.TestCase):
    def setUp(self):
        from compras.store import Store
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "a.db"
        self.store = Store(self.path, actor="test")
        for n in range(3):
            self.store.audit("test", None, "x", str(n))
        self.store.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_entries_cannot_be_edited_or_deleted(self):
        with self.assertRaisesRegex(sqlite3.IntegrityError, "solo de agregar"):
            self.store.db.execute("UPDATE audit SET detail = 'otro' WHERE id = 1")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "solo de agregar"):
            self.store.db.execute("DELETE FROM audit WHERE id = 2")

    def test_verify_detects_tampering_even_without_triggers(self):
        from compras.audit import verify
        self.assertEqual(verify(self.store), [])
        self.store.db.executescript("DROP TRIGGER audit_no_update; DROP TRIGGER audit_no_delete;"
                                    "UPDATE audit SET detail = 'otro' WHERE id = 1; DELETE FROM audit WHERE id = 2;")
        problems = verify(self.store)
        self.assertTrue(any("1 alterada" in p for p in problems))
        self.assertTrue(any("faltan entradas entre 1 y 3" in p for p in problems))

    def test_verify_detects_removed_newest_entries(self):
        from compras.audit import verify
        self.store.db.executescript("DROP TRIGGER audit_no_delete; DELETE FROM audit WHERE id = 3;")
        self.assertEqual(verify(self.store), ["faltan las últimas entradas (3–3)"])

    def test_old_table_is_rebuilt_and_chained(self):
        from compras.audit import verify
        from compras.store import Store
        path = Path(self.tmp.name) / "old.db"
        db = sqlite3.connect(path)
        db.executescript("CREATE TABLE audit (id INTEGER PRIMARY KEY, at TEXT NOT NULL, user_email TEXT,"
                         " invoice_id INTEGER, action TEXT NOT NULL, detail TEXT);"
                         "INSERT INTO audit (at, user_email, action) VALUES ('2026-10-28', 'a', 'ingreso');")
        db.commit()
        db.close()
        old = Store(path)
        self.assertIn("AUTOINCREMENT", old.db.execute("SELECT sql FROM sqlite_master WHERE name = 'audit'").fetchone()[0])
        self.assertEqual(verify(old), [])
        old.close()


if __name__ == "__main__":
    unittest.main()
