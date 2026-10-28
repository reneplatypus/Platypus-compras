"""Invoice index (SQLite for Phase 1). The database holds status and metadata; files hold the documents.

Statuses follow the handoff: needs_review → approved → imported, or rejected / error. Each status maps to
a top-level folder under documents/ so the accountant can also browse them on disk.
"""
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
    id INTEGER PRIMARY KEY,
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
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, sqltype in columns:
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sqltype}")
        self.db.commit()

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
        fields["updated_at"] = _now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE invoices SET {sets} WHERE id = ?", (*fields.values(), invoice_id))

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
        """parts: (seq, part, account, amount)."""
        self.db.execute("DELETE FROM invoice_lines WHERE invoice_id = ?", (invoice_id,))
        self.db.executemany("INSERT INTO invoice_lines (invoice_id, seq, part, account, amount) VALUES (?, ?, ?, ?, ?)",
                            [(invoice_id, *p) for p in parts])

    def audit(self, user_email, invoice_id, action, detail=None):
        self.db.execute("INSERT INTO audit (at, user_email, invoice_id, action, detail) VALUES (?, ?, ?, ?, ?)",
                        (_now(), user_email, invoice_id, action, detail))

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
        self.db.execute(
            """INSERT INTO users (email, name, role, active, created_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(email) DO UPDATE SET name = excluded.name, role = excluded.role, active = excluded.active""",
            (email.lower(), name, role, int(active), _now()))

    def supplier_map(self) -> dict:
        return {r["issuer_ruc"]: r for r in self.db.execute("SELECT * FROM supplier_map")}

    def clear_auto_suppliers(self):
        """Drop name/learned mappings so each check recomputes them; manual ones stay."""
        self.db.execute("DELETE FROM supplier_map WHERE method != 'manual'")

    def set_supplier(self, issuer_ruc, issuer_name, provider_id, provider_name, method, score):
        """Record a supplier mapping; manual ones are never overwritten by automatic matching."""
        row = self.db.execute("SELECT method FROM supplier_map WHERE issuer_ruc = ?", (issuer_ruc,)).fetchone()
        if row and row["method"] == "manual" and method != "manual":
            return
        self.db.execute(
            """INSERT OR REPLACE INTO supplier_map (issuer_ruc, issuer_name, provider_id, provider_name, method, score,
                   updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (issuer_ruc, issuer_name, provider_id, provider_name, method, score, _now()))

    def commit(self):
        self.db.commit()

    def counts(self) -> dict:
        rows = self.db.execute("SELECT status, COUNT(*) n FROM invoices GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}
