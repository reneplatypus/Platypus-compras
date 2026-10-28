"""Audit log tools.

Usage:
    python3 -m compras.audit verify        # check the hash chain; exits 1 if any entry was altered or removed
    python3 -m compras.audit show [N]      # last N entries (default 30)
"""
import json
import sys

from .config import settings
from .store import GENESIS, Store, audit_hash


def verify(store: Store) -> list[str]:
    """Problems found in the chain; empty when intact."""
    problems, prev, prev_id = [], GENESIS, 0
    for r in store.db.execute("SELECT * FROM audit ORDER BY id"):
        if r["id"] != prev_id + 1:
            problems.append(f"faltan entradas entre {prev_id} y {r['id']}")
        expected = audit_hash(prev, r["at"], r["user_email"], r["invoice_id"], r["action"], r["detail"])
        if r["hash"] != expected:
            problems.append(f"entrada {r['id']} alterada (hash no coincide)")
        prev, prev_id = r["hash"] or expected, r["id"]
    # The newest entries leave no gap when removed; SQLite's id counter still remembers them.
    seq = store.db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'audit'").fetchone()
    if seq and seq[0] > prev_id:
        problems.append(f"faltan las últimas entradas ({prev_id + 1}–{seq[0]})")
    return problems


def describe(detail: str | None) -> str:
    """Readable form of an entry's detail: 'campo: antes → después; ...'."""
    if not detail or not detail.startswith("{"):
        return detail or ""
    try:
        data = json.loads(detail)
    except ValueError:
        return detail
    out = []
    for key, value in data.items():
        if isinstance(value, list) and len(value) == 2:
            before, after = ("; ".join(v) if isinstance(v, list) else v for v in value)
            out.append(f"{key}: {before if before not in (None, '') else '—'} → {after if after not in (None, '') else '—'}")
        else:
            out.append(f"{key}: {value}")
    return " · ".join(out)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    store = Store(settings().db_path)
    if argv[:1] == ["verify"]:
        count = store.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        problems = verify(store)
        print(f"{count} entradas; " + ("cadena íntegra" if not problems else "PROBLEMAS:\n  " + "\n  ".join(problems)))
        store.close()
        sys.exit(1 if problems else 0)
    elif argv[:1] == ["show"]:
        n = int(argv[1]) if len(argv) > 1 else 30
        for r in reversed(store.audit_log(limit=n)):
            print(f"{r['id']:>6} {r['at']} {r['user_email'] or '-':28} {r['action']:22} "
                  f"{r['invoice_id'] or '':>5} {describe(r['detail'])[:160]}")
    else:
        sys.exit(__doc__)
    store.close()


if __name__ == "__main__":
    main()
