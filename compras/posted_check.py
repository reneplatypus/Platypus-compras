"""Mark which invoices are already in InterFuerza (read-only), so the UI can filter posted / not posted.

InterFuerza purchases carry no CUFE or supplier RUC, so an invoice is matched on its total (to the cent),
date and supplier. Suppliers are mapped RUC → InterFuerza provider by name similarity, or learned when one
provider keeps matching an issuer's invoices on total and date. Each purchase matches at most one invoice.

States: posted (confident), possible (same total nearby, reviewer should confirm), not_posted.

Usage: python3 -m compras.posted_check [--cached]   (--cached re-scores the last download without calling the API)
"""
import difflib
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .config import settings
from .fe_parser import CREDIT_TYPES
from .interfuerza import Client
from .store import FOLDERS, Store

CANCELED = {"ABORTED", "DELETED", "VOID", "CANCELED", "CANCELLED"}
NOISE = {"S", "A", "SA", "INC", "CORP", "CORPORATION", "CO", "CIA", "COMPANIA", "Y", "DE", "DEL", "LA", "EL",
         "LOS", "LAS", "THE", "SRL", "RL", "LTD", "LIMITED", "PTY", "INTERNAT", "INTERNATIONAL"}
NAME_MATCH = 0.75     # lower thresholds paired IEH Evergo with Claro and NGU Holding with Milly Holding
WINDOW_DAYS = 60      # how far apart invoice and purchase dates may be to count as candidates
CLOSE_DAYS = 10       # within this, a matching supplier + total is treated as posted
OTHER_SUPPLIER_DAYS = 3   # a different supplier with the same total only counts this close (small totals repeat)
CENT = Decimal("0.01")


@dataclass
class Purchase:
    id: str
    provider_id: str
    provider_name: str
    date: date
    total: Decimal
    text: str            # comment + line descriptions, uppercased, for invoice-number hits


@dataclass
class InvoiceRef:
    cufe: str
    issuer_ruc: str
    issuer_name: str
    number: str
    date: date
    total: Decimal


def tokens(name: str) -> set[str]:
    plain = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().upper()
    return {t for t in re.split(r"[^A-Z0-9]+", plain) if t and t not in NOISE}


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    # "CASA DEL TELEFONO NO 5" is not "NO 6", but both are the generic "La Casa del Teléfono, S.A.":
    # numbers only have to agree when both names carry one.
    na, nb = {t for t in ta if t.isdigit()}, {t for t in tb if t.isdigit()}
    if na and nb and na != nb:
        return 0.0
    jaccard = len(ta & tb) / len(ta | tb)
    ratio = difflib.SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    shorter = min(ta, tb, key=len)
    # "EMPRESAS MELO S A COMPAÑIA ..." vs "Empresas Melo S.A": the short name fully inside the long one.
    contained = 0.9 if len(shorter) >= 2 and shorter <= (ta & tb) else 0.0
    return max(jaccard, ratio, contained)


def purchases_from(records: list) -> list[Purchase]:
    out = []
    for rec in records:
        head = rec.get("Purchase") or rec.get("Quote") or {}
        if (head.get("Status") or "").upper() in CANCELED or head.get("Type", "PURCHASE") != "PURCHASE":
            continue
        text = " ".join([head.get("Comentario") or ""] + [ln.get("Descripcion") or "" for ln in rec.get("Lines") or []])
        out.append(Purchase(id=head["id"], provider_id=head.get("Proveedor") or "", provider_name=head.get("Nombre") or "",
                            date=date.fromisoformat(head["Date"][:10]), total=Decimal(head.get("Total") or "0"),
                            text=text.upper()))
    return out


def _number_hit(number: str, text: str) -> bool:
    core = (number or "").lstrip("0")
    return len(core) >= 3 and re.search(rf"(?<!\d){re.escape(core)}(?!\d)", text) is not None


def map_by_name(invoices: list[InvoiceRef], providers: dict[str, str], known: dict) -> dict:
    """issuer_ruc → (provider_id, score) for issuers not mapped yet."""
    out = {}
    for ruc, name in {i.issuer_ruc: i.issuer_name for i in invoices}.items():
        if ruc in known:
            continue
        best = max(((similarity(name, pname), pid) for pid, pname in providers.items()), default=(0.0, None))
        if best[0] >= NAME_MATCH:
            out[ruc] = (best[1], round(best[0], 2))
    return out


def _candidates(inv: InvoiceRef, by_total: dict) -> list[Purchase]:
    return [p for p in by_total.get(inv.total.quantize(CENT), []) if abs((p.date - inv.date).days) <= WINDOW_DAYS]


def learn_mappings(invoices: list[InvoiceRef], by_total: dict, known: dict) -> dict:
    """issuer_ruc → (provider_id, share) when one provider keeps matching an issuer on exact total ± 3 days."""
    hits = defaultdict(Counter)
    matched = Counter()
    for inv in invoices:
        if inv.issuer_ruc in known:
            continue
        near = {p.provider_id for p in _candidates(inv, by_total) if abs((p.date - inv.date).days) <= 3}
        if near:
            matched[inv.issuer_ruc] += 1
            hits[inv.issuer_ruc].update(near)
    out = {}
    for ruc, counter in hits.items():
        pid, n = counter.most_common(1)[0]
        if n >= 2 and n / matched[ruc] >= 0.6:
            out[ruc] = (pid, round(n / matched[ruc], 2))
    return out


def match(invoices: list[InvoiceRef], purchases: list[Purchase], supplier_of: dict[str, str]) -> dict:
    """cufe → (state, purchase_id, note). supplier_of: issuer_ruc → InterFuerza provider id."""
    by_total = defaultdict(list)
    for p in purchases:
        by_total[p.total.quantize(CENT)].append(p)

    pairs = []
    for inv in invoices:
        for p in _candidates(inv, by_total):
            days = abs((p.date - inv.date).days)
            supplier_ok = supplier_of.get(inv.issuer_ruc) == p.provider_id or \
                similarity(inv.issuer_name, p.provider_name) >= NAME_MATCH
            number_hit = _number_hit(inv.number, p.text)
            if not supplier_ok and not number_hit and days > OTHER_SUPPLIER_DAYS:
                continue
            posted = (supplier_ok and days <= CLOSE_DAYS) or (number_hit and (supplier_ok or days <= CLOSE_DAYS))
            rank = (posted, number_hit, supplier_ok, -days)
            note = f"compra {p.id} ({p.provider_name}, {p.date}, {p.total})" + \
                   ("; nº de factura en la compra" if number_hit else "") + \
                   ("" if supplier_ok else "; proveedor distinto")
            pairs.append((rank, inv.cufe, p.id, "posted" if posted else "possible", note))

    results, used = {}, set()
    for rank, cufe, pid, state, note in sorted(pairs, key=lambda x: x[0], reverse=True):
        if cufe in results or pid in used:
            continue
        results[cufe] = (state, pid, note)
        used.add(pid)
    for inv in invoices:
        results.setdefault(inv.cufe, ("not_posted", None, None))
    return results


def _swap_top(folder: Path, documents_dir: Path, top: str) -> Path:
    rest = folder.relative_to(documents_dir).parts[1:]
    return documents_dir.joinpath(top, *rest)


def _folder_move(row, state, purchase_id, documents_dir: Path):
    """(new folder, fields) when the check changes where an invoice belongs, else None.

    A confident match takes an invoice under review to posted/. If a later check no longer finds the
    purchase (e.g. it was canceled), an invoice that got there by matching goes back to review.
    Invoices approved or posted by this app, and rejected ones, are left alone.
    """
    folder = Path(row["folder"])
    if row["status"] == "needs_review" and state == "posted":
        return _swap_top(folder, documents_dir, FOLDERS["imported"]), dict(
            status="imported", posted_via="match", status_reason=f"Ya registrada en InterFuerza: compra {purchase_id}")
    if row["status"] == "imported" and row["posted_via"] == "match" and state != "posted":
        return _swap_top(folder, documents_dir, FOLDERS["needs_review"]), dict(
            status="needs_review", posted_via=None, status_reason=None)
    return None


def _refs(rows) -> list[InvoiceRef]:
    return [InvoiceRef(cufe=r["cufe"], issuer_ruc=r["issuer_ruc"], issuer_name=r["issuer_name"], number=r["number"],
                       date=date.fromisoformat(r["issued_at"][:10]), total=Decimal(r["total"])) for r in rows]


def main():
    os.umask(0o077)
    cfg = settings()
    store = Store(cfg.db_path)
    rows = store.invoices(("needs_review", "approved", "rejected", "imported"))
    credit_rows = [r for r in rows if r["doc_type"] in CREDIT_TYPES]
    invoices = _refs([r for r in rows if r["doc_type"] not in CREDIT_TYPES])
    since = min(i.date for i in invoices) - timedelta(days=WINDOW_DAYS)

    cache = cfg.data_dir / "interfuerza"
    if "--cached" in sys.argv:
        providers = json.loads((cache / "providers.json").read_text())
        records = json.loads((cache / "purchases.json").read_text())
    else:
        client = Client()
        providers = {p["Proveedor"]: p["Nombre"] for p in client.all_pages("providers", "providers")}
        records = list(client.all_pages("purchases", "purchases",
                                        filters=[{"field": "Date", "type": ">=", "value": since.isoformat()}]))
        cache.mkdir(exist_ok=True)
        (cache / "providers.json").write_text(json.dumps(providers, ensure_ascii=False))
        (cache / "purchases.json").write_text(json.dumps(records, ensure_ascii=False))
    purchases = purchases_from(records)
    print(f"InterFuerza: {len(providers)} proveedores, {len(purchases)} compras vigentes desde {since}")

    store.clear_auto_suppliers()
    known = {ruc: m["provider_id"] for ruc, m in store.supplier_map().items()}
    by_total = defaultdict(list)
    for p in purchases:
        by_total[p.total.quantize(CENT)].append(p)
    names = {i.issuer_ruc: i.issuer_name for i in invoices}
    for ruc, (pid, score) in map_by_name(invoices, providers, known).items():
        store.set_supplier(ruc, names[ruc], pid, providers[pid], "name", score)
    known = {ruc: m["provider_id"] for ruc, m in store.supplier_map().items()}
    for ruc, (pid, share) in learn_mappings(invoices, by_total, known).items():
        store.set_supplier(ruc, names[ruc], pid, providers.get(pid), "learned", share)
    known = {ruc: m["provider_id"] for ruc, m in store.supplier_map().items()}

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results = match(invoices, purchases, known)
    by_cufe = {r["cufe"]: r for r in rows}
    moved = Counter()
    for cufe, (state, pid, note) in results.items():
        row = by_cufe[cufe]
        store.update_invoice(row["id"], if_state=state, if_purchase_id=pid, if_match_note=note, if_checked_at=now)
        move = _folder_move(row, state, pid, cfg.documents_dir)
        if move:
            folder, fields = move
            store.move_invoice(row["id"], Path(row["folder"]), folder, **fields)
            moved[fields["status"]] += 1
    for r in credit_rows:
        store.update_invoice(r["id"], if_state=None, if_purchase_id=None, if_checked_at=now,
                             if_match_note="Notas de crédito de compra: el token no tiene permiso de lectura")
    store.commit()
    print("estado InterFuerza:", dict(Counter(s for s, _, _ in results.values())),
          f"| proveedores mapeados: {len(known)} de {len(names)} | notas de crédito sin verificar: {len(credit_rows)}",
          f"| movidas a posted: {moved['imported']}, devueltas a revisión: {moved['needs_review']}")
    store.close()


if __name__ == "__main__":
    main()
