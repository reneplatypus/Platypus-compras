"""Invoice index (SQLite for Phase 1). The database holds status and metadata; files hold the documents.

Statuses follow the handoff: needs_review → approved → imported, or rejected / error. Each status maps to
a top-level folder under documents/ so the accountant can also browse them on disk.

Audit: every change to an invoice, its lines, a supplier mapping or a user is written to `audit` with the
actor (a user's email, `sistema:<job>`, or `cli:<os user>`) and before → after values. The table is
append-only (triggers refuse UPDATE and DELETE) and hash-chained: each entry's hash covers the previous
entry's hash, so `python3 -m compras.audit verify` detects any entry altered or removed outside the app.
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

FOLDERS = {
    "needs_review": "unposted",
    "approved": "unposted",
    "imported": "posted",
    "rejected": "rejected",
    "error": "errors",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,
    cufe TEXT NOT NULL UNIQUE,
    doc_type TEXT NOT NULL,
    number TEXT,
    issued_at TEXT NOT NULL,
    issuer_ruc TEXT,
    issuer_name TEXT,
    receiver_ruc TEXT,
    net_total TEXT,
    itbms_total TEXT,
    total TEXT,
    status TEXT NOT NULL,
    status_reason TEXT,
    folder TEXT NOT NULL,
    issues TEXT NOT NULL DEFAULT '[]',
    gmail_msgid TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER REFERENCES invoices(id),
    gmail_msgid TEXT,
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS supplier_map (
    issuer_ruc TEXT PRIMARY KEY,
    issuer_name TEXT,
    provider_id TEXT NOT NULL,
    provider_name TEXT,
    method TEXT NOT NULL,          -- name | learned | manual
    score REAL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoice_lines (
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    seq INTEGER NOT NULL,              -- XML line (dSecItem)
    part INTEGER NOT NULL,             -- split of that line across accounts, 1..n
    account TEXT NOT NULL,             -- InterFuerza Internal_Code, e.g. ACC00014
    amount TEXT NOT NULL,
    PRIMARY KEY (invoice_id, seq, part)
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    role TEXT NOT NULL,                -- viewer | preparer | approver | admin
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_codes (
    email TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,   -- ids never reused, so a removed entry leaves a gap
    at TEXT NOT NULL,
    user_email TEXT,
    invoice_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    gmail_msgid TEXT PRIMARY KEY,
    message_id TEXT,
    sender TEXT,
    subject TEXT,
    sent_at TEXT,
    outcome TEXT NOT NULL,
    detail TEXT,
    processed_at TEXT NOT NULL
);
"""


# Columns added after the first release, applied to existing databases on open.
MIGRATIONS = {
    "invoices": [
        ("if_state", "TEXT"),            # posted | possible | not_posted (from the InterFuerza check)
        ("if_purchase_id", "TEXT"),
        ("if_match_note", "TEXT"),
        ("if_checked_at", "TEXT"),
        ("posted_via", "TEXT"),          # match (found in InterFuerza) | manual (marked by a reviewer) | app
        ("reviewed_by", "TEXT"),
        ("reviewed_at", "TEXT"),
        ("approved_by", "TEXT"),
        ("approved_at", "TEXT"),
        ("if_dismissed", "TEXT"),        # purchase id a reviewer said is not this invoice
    ],
    "audit": [
        ("hash", "TEXT"),                # sha256 over the previous entry's hash and this entry's fields
    ],
}

APPEND_ONLY = """
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit es solo de agregar'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit es solo de agregar'); END;
"""

# Bookkeeping fields that change on every run and carry no decision.
UNTRACKED = {"updated_at", "if_checked_at"}
GENESIS = "0" * 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def audit_hash(prev: str, at, user_email, invoice_id, action, detail) -> str:
    payload = json.dumps([prev, at, user_email, invoice_id, action, detail], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


class Store:
    def __init__(self, path: Path, actor: str | None = None):
        self.actor = actor or "desconocido"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        # The web app, the Gmail intake and the InterFuerza check share this file: WAL lets readers
        # continue during a write, and the timeout makes a writer wait instead of failing.
        # check_same_thread=False: FastAPI may open a request's Store in a worker thread and use it in the
        # event loop (async endpoints). Each request has its own Store and uses it one step at a time.
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, sqltype in columns:
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sqltype}")
        self._chain_existing_audit()
        self.db.executescript(APPEND_ONLY)
        self.db.commit()

    def _chain_existing_audit(self):
        """One-time, before the append-only triggers exist: rebuild the audit table with AUTOINCREMENT if it
        was created without it, and hash the entries written before the chain existed."""
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = 'audit_no_update'").fetchone():
            return
        sql = self.db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'audit'").fetchone()[0]
        if "AUTOINCREMENT" not in sql.upper():
            self.db.executescript("""
                ALTER TABLE audit RENAME TO audit_old;
                CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, user_email TEXT,
                    invoice_id INTEGER, action TEXT NOT NULL, detail TEXT, hash TEXT);
                INSERT INTO audit (id, at, user_email, invoice_id, action, detail, hash)
                    SELECT id, at, user_email, invoice_id, action, detail, hash FROM audit_old ORDER BY id;
                DROP TABLE audit_old;
            """)
        prev = GENESIS
        for r in self.db.execute("SELECT * FROM audit ORDER BY id").fetchall():
            prev = audit_hash(prev, r["at"], r["user_email"], r["invoice_id"], r["action"], r["detail"])
            self.db.execute("UPDATE audit SET hash = ? WHERE id = ?", (prev, r["id"]))

    def close(self):
        self.db.close()

    def seen_message(self, gmail_msgid: str) -> bool:
        return self.db.execute("SELECT 1 FROM messages WHERE gmail_msgid = ?", (gmail_msgid,)).fetchone() is not None

    def invoice_by_cufe(self, cufe: str):
        return self.db.execute("SELECT * FROM invoices WHERE cufe = ?", (cufe,)).fetchone()

    def add_invoice(self, inv, status: str, reason: str | None, folder: Path, issues: list, gmail_msgid: str) -> int:
        now = _now()
        cur = self.db.execute(
            """INSERT INTO invoices (cufe, doc_type, number, issued_at, issuer_ruc, issuer_name, receiver_ruc,
                   net_total, itbms_total, total, status, status_reason, folder, issues, gmail_msgid,
                   created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (inv.cufe, inv.doc_type, inv.number, inv.issued_at.isoformat(), inv.issuer_ruc, inv.issuer_name,
             inv.receiver_ruc, str(inv.net_total), str(inv.itbms_total), str(inv.total), status, reason,
             str(folder), json.dumps([i.__dict__ for i in issues], ensure_ascii=False), gmail_msgid, now, now))
        self.audit(self.actor, cur.lastrowid, "alta",
                   json.dumps({"status": [None, status], "folder": [None, str(folder)], "gmail_msgid": gmail_msgid,
                               "cufe": inv.cufe}, ensure_ascii=False))
        return cur.lastrowid

    def add_document(self, invoice_id, gmail_msgid, kind, filename, path, sha256, size):
        self.db.execute(
            "INSERT INTO documents (invoice_id, gmail_msgid, kind, filename, path, sha256, size) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (invoice_id, gmail_msgid, kind, filename, str(path), sha256, size))

    def add_message(self, gmail_msgid, message_id, sender, subject, sent_at, outcome, detail=None):
        self.db.execute(
            """INSERT OR REPLACE INTO messages (gmail_msgid, message_id, sender, subject, sent_at, outcome, detail,
                   processed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (gmail_msgid, message_id, sender, subject, sent_at, outcome, detail, _now()))

    def invoices(self, statuses: tuple[str, ...]):
        marks = ",".join("?" * len(statuses))
        return self.db.execute(f"SELECT * FROM invoices WHERE status IN ({marks}) ORDER BY id", statuses).fetchall()

    def update_invoice(self, invoice_id: int, **fields):
        """Update fields, auditing each one that actually changes as [before, after]."""
        row = self.invoice(invoice_id)
        changes = {k: [row[k], v] for k, v in fields.items() if k not in UNTRACKED and row[k] != v}
        fields["updated_at"] = _now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE invoices SET {sets} WHERE id = ?", (*fields.values(), invoice_id))
        if changes:
            self.audit(self.actor, invoice_id, "cambio", json.dumps(changes, ensure_ascii=False))

    def move_invoice(self, invoice_id: int, old: Path, new: Path, **fields):
        """Move an invoice's folder (e.g. unposted → posted) and keep document paths in step."""
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)
        for parent in (old.parent, old.parent.parent):   # empty month / year folders left behind
            try:
                parent.rmdir()
            except OSError:
                break
        for doc in self.db.execute("SELECT id, path FROM documents WHERE invoice_id = ?", (invoice_id,)).fetchall():
            self.db.execute("UPDATE documents SET path = ? WHERE id = ?", (str(new / Path(doc["path"]).name), doc["id"]))
        self.update_invoice(invoice_id, folder=str(new), **fields)

    def invoice(self, invoice_id: int):
        return self.db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()

    def documents(self, invoice_id: int):
        return self.db.execute("SELECT * FROM documents WHERE invoice_id = ? ORDER BY kind DESC, id",
                               (invoice_id,)).fetchall()

    def document(self, doc_id: int):
        return self.db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()

    def set_status(self, invoice_id: int, status: str, documents_dir: Path, **fields):
        """Change status, moving the folder when the status lives under a different top-level folder."""
        row = self.invoice(invoice_id)
        old = Path(row["folder"])
        top = FOLDERS[status]
        if old.relative_to(documents_dir).parts[0] != top:
            new = documents_dir.joinpath(top, *old.relative_to(documents_dir).parts[1:])
            self.move_invoice(invoice_id, old, new, status=status, **fields)
        else:
            self.update_invoice(invoice_id, status=status, **fields)

    def lines(self, invoice_id: int) -> list:
        return self.db.execute("SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY seq, part",
                               (invoice_id,)).fetchall()

    def replace_lines(self, invoice_id: int, parts: list[tuple[int, int, str, str]]):
        """parts: (seq, part, account, amount). Audited as the full before and after allocation."""
        before = [f"{r['seq']}.{r['part']} {r['account']} {r['amount']}" for r in self.lines(invoice_id)]
        after = [f"{s}.{p} {a} {m}" for s, p, a, m in parts]
        self.db.execute("DELETE FROM invoice_lines WHERE invoice_id = ?", (invoice_id,))
        self.db.executemany("INSERT INTO invoice_lines (invoice_id, seq, part, account, amount) VALUES (?, ?, ?, ?, ?)",
                            [(invoice_id, *p) for p in parts])
        if before != after:
            self.audit(self.actor, invoice_id, "lineas", json.dumps({"lineas": [before, after]}, ensure_ascii=False))

    def audit(self, user_email, invoice_id, action, detail=None):
        # Take the write lock before reading the last hash, so two processes can't fork the chain.
        if not self.db.in_transaction:
            self.db.execute("BEGIN IMMEDIATE")
        last = self.db.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        at = _now()
        digest = audit_hash(last["hash"] if last else GENESIS, at, user_email, invoice_id, action, detail)
        self.db.execute("INSERT INTO audit (at, user_email, invoice_id, action, detail, hash) VALUES (?, ?, ?, ?, ?, ?)",
                        (at, user_email, invoice_id, action, detail, digest))

    def audit_log(self, limit=200, offset=0, actor=None, action=None):
        where, args = [], []
        if actor:
            where.append("user_email LIKE ?"); args.append(f"%{actor}%")
        if action:
            where.append("action = ?"); args.append(action)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        return self.db.execute(f"SELECT * FROM audit {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                               (*args, limit, offset)).fetchall()

    def history(self, invoice_id: int):
        return self.db.execute("SELECT * FROM audit WHERE invoice_id = ? ORDER BY id DESC", (invoice_id,)).fetchall()

    def search(self, status=None, if_state=None, supplier=None, month=None, limit=50, offset=0):
        where, args = [], []
        if status:
            where.append("status = ?"); args.append(status)
        if if_state == "unchecked":
            where.append("if_state IS NULL")
        elif if_state:
            where.append("if_state = ?"); args.append(if_state)
        if supplier:
            where.append("(issuer_name LIKE ? OR issuer_ruc LIKE ?)"); args += [f"%{supplier}%"] * 2
        if month:
            where.append("substr(issued_at, 1, 7) = ?"); args.append(month)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.db.execute(f"SELECT COUNT(*), COALESCE(SUM(CAST(total AS REAL)), 0) FROM invoices {clause}",
                                args).fetchone()
        rows = self.db.execute(f"SELECT * FROM invoices {clause} ORDER BY issued_at DESC, id DESC LIMIT ? OFFSET ?",
                               (*args, limit, offset)).fetchall()
        return rows, total[0], total[1]

    # users and sessions

    def user_by_email(self, email: str):
        return self.db.execute("SELECT * FROM users WHERE email = ? AND active = 1", (email.lower(),)).fetchone()

    def users(self):
        return self.db.execute("SELECT * FROM users ORDER BY active DESC, name").fetchall()

    def upsert_user(self, email: str, name: str, role: str, active: bool = True):
        email = email.lower()
        old = self.db.execute("SELECT name, role, active FROM users WHERE email = ?", (email,)).fetchone()
        self.db.execute(
            """INSERT INTO users (email, name, role, active, created_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(email) DO UPDATE SET name = excluded.name, role = excluded.role, active = excluded.active""",
            (email, name, role, int(active), _now()))
        new = {"name": name, "role": role, "active": int(active)}
        changes = {k: [old[k] if old else None, v] for k, v in new.items() if not old or old[k] != v}
        if changes:
            self.audit(self.actor, None, "usuario", json.dumps({"email": email, **changes}, ensure_ascii=False))

    def supplier_map(self) -> dict:
        return {r["issuer_ruc"]: r for r in self.db.execute("SELECT * FROM supplier_map")}

    def set_supplier(self, issuer_ruc, issuer_name, provider_id, provider_name, method, score):
        """Record a supplier mapping; manual ones are never overwritten by automatic matching."""
        row = self.db.execute("SELECT * FROM supplier_map WHERE issuer_ruc = ?", (issuer_ruc,)).fetchone()
        if row and row["method"] == "manual" and method != "manual":
            return
        self.db.execute(
            """INSERT OR REPLACE INTO supplier_map (issuer_ruc, issuer_name, provider_id, provider_name, method, score,
                   updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (issuer_ruc, issuer_name, provider_id, provider_name, method, score, _now()))
        if not row or (row["provider_id"], row["method"]) != (provider_id, method):
            self._audit_supplier(issuer_ruc, issuer_name, row, provider_id, provider_name, method)

    def sync_auto_suppliers(self, wanted: dict):
        """Make the automatic mappings equal `wanted` (issuer_ruc → (issuer_name, provider_id, provider_name,
        method, score)), leaving manual ones alone and auditing only real changes."""
        current = {ruc: r for ruc, r in self.supplier_map().items() if r["method"] != "manual"}
        for ruc, row in current.items():
            if ruc not in wanted:
                self.db.execute("DELETE FROM supplier_map WHERE issuer_ruc = ?", (ruc,))
                self._audit_supplier(ruc, row["issuer_name"], row, None, None, None)
        for ruc, (name, pid, pname, method, score) in wanted.items():
            self.set_supplier(ruc, name, pid, pname, method, score)

    def _audit_supplier(self, ruc, name, old, provider_id, provider_name, method):
        before = f"{old['provider_id']} {old['provider_name']} ({old['method']})" if old else None
        after = f"{provider_id} {provider_name} ({method})" if provider_id else None
        self.audit(self.actor, None, "proveedor",
                   json.dumps({"ruc": ruc, "emisor": name, "proveedor": [before, after]}, ensure_ascii=False))

    def commit(self):
        self.db.commit()

    def counts(self) -> dict:
        rows = self.db.execute("SELECT status, COUNT(*) n FROM invoices GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}
