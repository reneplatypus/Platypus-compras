"""Re-run validation on invoices not yet approved, after a rule change, moving folders as needed.

Usage: python3 -m compras.revalidate
"""
import json
from pathlib import Path

from .config import settings
from .fe_parser import parse
from .intake import _invoice_folder, classify
from .store import Store


def main():
    cfg = settings()
    store = Store(cfg.db_path, actor="sistema:revalidate")
    moved = updated = 0
    for row in store.invoices(("needs_review", "rejected")):
        folder = Path(row["folder"])
        xml = next(p for p in folder.iterdir() if p.suffix.lower() == ".xml")
        inv = parse(xml.read_bytes())
        status, reason, issues = classify(inv)
        fields = dict(status=status, status_reason=reason,
                      issues=json.dumps([i.__dict__ for i in issues], ensure_ascii=False))
        if status != row["status"]:
            store.move_invoice(row["id"], folder, _invoice_folder(cfg.documents_dir, status, inv), **fields)
            moved += 1
        else:
            store.update_invoice(row["id"], **fields)
            updated += 1
    store.commit()
    print(f"movidas {moved}, actualizadas {updated} | por estado: {store.counts()}")
    store.close()


if __name__ == "__main__":
    main()
