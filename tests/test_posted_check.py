import unittest
from collections import defaultdict
from datetime import date
from decimal import Decimal as D
from pathlib import Path

from compras.posted_check import (InvoiceRef, Purchase, _folder_move, learn_mappings, match, purchases_from,
                                  similarity)


def inv(cufe, ruc="1-1-1", name="GEO F NOVEY INC", number="0000123456", day=10, total="29.93"):
    return InvoiceRef(cufe, ruc, name, number, date(2026, 10, day), D(total))


def pur(pid, provider="P0006", name="Geo F. Novey, INC", day=10, total="29.93", text=""):
    return Purchase(pid, provider, name, date(2026, 10, day), D(total), text.upper())


class SimilarityTest(unittest.TestCase):
    def test_legal_suffixes_and_punctuation_ignored(self):
        self.assertGreaterEqual(similarity("GEO F NOVEY INC", "Geo F. Novey, INC"), 0.9)
        self.assertGreaterEqual(similarity("COMERCIAL UNILAYS PANAMA S A", "Comercial Unilays Panama, S.A."), 0.9)
        self.assertLess(similarity("RIBA SMITH S A", "Geo F. Novey, INC"), 0.6)

    def test_numbers_must_agree(self):
        self.assertEqual(similarity("LA CASA DEL TELEFONO NO 5  S A", "LA CASA DEL TELEFONO NO 6 SA"), 0.0)
        self.assertGreaterEqual(similarity("LA CASA DEL TELEFONO NO 6  S A", "La Casa del Teléfono, S.A."), 0.75)

    def test_short_name_contained_in_long_one(self):
        self.assertGreaterEqual(similarity("EMPRESAS MELO, S.A. COMPAÑÍA DE MATERIALES", "Empresas Melo S.A"), 0.75)

    def test_unrelated_holdings_do_not_match(self):
        self.assertLess(similarity("NGU HOLDING, S. DE R.L.", "Milly Holding, S.A."), 0.75)
        self.assertLess(similarity("IEH EVERGO PANAMA, S.A.", "Claro Panama, S.A."), 0.75)


class MatchTest(unittest.TestCase):
    def test_same_supplier_total_and_date_is_posted(self):
        result = match([inv("A")], [pur("00010", day=11)], {})
        self.assertEqual(result["A"][:2], ("posted", "00010"))

    def test_same_total_other_supplier_is_only_possible(self):
        result = match([inv("A")], [pur("00010", provider="P0099", name="Otra Empresa")], {})
        self.assertEqual(result["A"][0], "possible")

    def test_other_supplier_far_away_is_ignored(self):
        result = match([inv("A")], [pur("00010", provider="P0099", name="Otra Empresa", day=20)], {})
        self.assertEqual(result["A"][0], "not_posted")

    def test_invoice_number_in_comment_wins(self):
        purchases = [pur("00010", day=12), pur("00011", day=10, text="Factura 123456")]
        self.assertEqual(match([inv("A")], purchases, {})["A"][1], "00011")

    def test_each_purchase_matches_one_invoice(self):
        result = match([inv("A", day=10), inv("B", day=10)], [pur("00010")], {})
        self.assertEqual(sorted(s for s, _, _ in result.values()), ["not_posted", "posted"])

    def test_no_candidate_is_not_posted(self):
        self.assertEqual(match([inv("A")], [pur("00010", total="30.00")], {})["A"][0], "not_posted")

    def test_mapping_by_ruc_overrides_name(self):
        invoice = inv("A", ruc="15430-249-148718", name="ACE INTERNAT HARDWARE CORP")
        purchase = pur("00010", provider="P0042", name="Do it Center")
        self.assertEqual(match([invoice], [purchase], {})["A"][0], "possible")
        self.assertEqual(match([invoice], [purchase], {"15430-249-148718": "P0042"})["A"][0], "posted")


class LearnTest(unittest.TestCase):
    def test_learns_provider_from_repeated_matches(self):
        ace = dict(ruc="15430-249-148718", name="ACE INTERNAT HARDWARE CORP")
        invoices = [inv("A", total="5.77", **ace), inv("B", total="19.24", day=20, **ace)]
        by_total = defaultdict(list)
        for p in [pur("1", provider="P0042", name="Do it Center", total="5.77"),
                  pur("2", provider="P0042", name="Do it Center", total="19.24", day=21)]:
            by_total[p.total].append(p)
        self.assertEqual(learn_mappings(invoices, by_total, {}), {"15430-249-148718": ("P0042", 1.0)})


class FolderMoveTest(unittest.TestCase):
    docs = Path("/data/documents")

    def row(self, status, posted_via=None, top="unposted"):
        return {"status": status, "posted_via": posted_via, "folder": str(self.docs / top / "2026/10/1-1-1_0001")}

    def test_confident_match_moves_to_posted(self):
        folder, fields = _folder_move(self.row("needs_review"), "posted", "00010", self.docs)
        self.assertEqual(folder, self.docs / "posted/2026/10/1-1-1_0001")
        self.assertEqual((fields["status"], fields["posted_via"]), ("imported", "match"))

    def test_possible_stays_in_review(self):
        self.assertIsNone(_folder_move(self.row("needs_review"), "possible", "00010", self.docs))

    def test_lost_match_returns_to_review(self):
        folder, fields = _folder_move(self.row("imported", "match", "posted"), "not_posted", None, self.docs)
        self.assertEqual(folder, self.docs / "unposted/2026/10/1-1-1_0001")
        self.assertEqual(fields["status"], "needs_review")

    def test_app_posted_and_rejected_untouched(self):
        self.assertIsNone(_folder_move(self.row("imported", "app", "posted"), "not_posted", None, self.docs))
        self.assertIsNone(_folder_move(self.row("rejected", top="rejected"), "posted", "00010", self.docs))


class PurchasesFromTest(unittest.TestCase):
    def test_skips_canceled(self):
        records = [{"Purchase": {"id": "1", "Proveedor": "P1", "Nombre": "X", "Status": "ABORTED", "Date": "2026-01-01",
                                 "Total": "1.00", "Type": "PURCHASE"}, "Lines": []},
                   {"Purchase": {"id": "2", "Proveedor": "P1", "Nombre": "X", "Status": "CLOSED", "Date": "2026-01-01",
                                 "Total": "1.00", "Type": "PURCHASE", "Comentario": "fac 99"},
                    "Lines": [{"Descripcion": "tornillo"}]}]
        out = purchases_from(records)
        self.assertEqual([p.id for p in out], ["2"])
        self.assertIn("TORNILLO", out[0].text)


if __name__ == "__main__":
    unittest.main()
