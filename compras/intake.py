"""Gmail intake over IMAP (read-only): find e-invoice emails, file their XML/PDF, index them by CUFE.

Nothing in the mailbox changes: the folder is opened read-only and bodies are fetched with BODY.PEEK.
Processed messages are remembered by Gmail message id (X-GM-MSGID) in the store.

Usage: python3 -m compras.intake [--since YYYY/MM/DD] [--limit N] [--dry-run]
"""
import argparse
import email
import hashlib
import imaplib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path

from .config import settings
from .fe_parser import InvoiceParseError, parse
from .store import FOLDERS, Store
from .validate import ERROR, validate

# Errors that mean "not ours to post", as opposed to "needs a human look".
REJECT_CODES = {"wrong_receiver", "test_environment"}


@dataclass
class Attachment:
    filename: str
    data: bytes

    @property
    def kind(self) -> str:
        return "xml" if self.filename.lower().endswith(".xml") else "pdf"


@dataclass
class Outcome:
    kind: str                 # filed | duplicate | error | no_invoice
    detail: str = ""
    cufes: list[str] = field(default_factory=list)


def _decode(value) -> str:
    return str(make_header(decode_header(value))) if value else ""


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:150]


def attachments(msg: Message) -> list[Attachment]:
    out = []
    for part in msg.walk():
        name = part.get_filename()
        if not name:
            continue
        name = _decode(name)
        if name.lower().endswith((".xml", ".pdf")):
            out.append(Attachment(name, part.get_payload(decode=True) or b""))
    return out


def _write(folder: Path, att: Attachment) -> tuple[Path, str]:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / _safe(att.filename)
    path.write_bytes(att.data)
    return path, hashlib.sha256(att.data).hexdigest()


def _invoice_folder(documents_dir: Path, status: str, inv) -> Path:
    base = documents_dir / FOLDERS[status] / f"{inv.issued_at:%Y}" / f"{inv.issued_at:%m}"
    folder = base / _safe(f"{inv.issuer_ruc}_{inv.number}")
    return folder if not folder.exists() else base / _safe(inv.cufe)


def classify(inv):
    """(status, reason, issues) for a newly seen invoice."""
    issues = validate(inv)
    rejected = [i for i in issues if i.severity == ERROR and i.code in REJECT_CODES]
    status = "rejected" if rejected else "needs_review"
    return status, "; ".join(i.message for i in rejected) or None, issues


def _pdfs_for(inv, xml_count: int, pdfs: list[Attachment]) -> list[Attachment]:
    if xml_count == 1:
        return pdfs
    return [p for p in pdfs if inv.cufe in p.filename or (inv.number and inv.number in p.filename)]


def process_message(raw: bytes, gmail_msgid: str, store: Store, documents_dir: Path) -> Outcome:
    msg = email.message_from_bytes(raw)
    meta = dict(message_id=msg.get("Message-ID"), sender=_decode(msg.get("From")),
                subject=_decode(msg.get("Subject")), sent_at=msg.get("Date"))
    atts = attachments(msg)
    xmls = [a for a in atts if a.kind == "xml"]
    pdfs = [a for a in atts if a.kind == "pdf"]
    if not xmls:
        store.add_message(gmail_msgid, outcome="no_invoice", detail="sin XML", **meta)
        return Outcome("no_invoice")

    filed, duplicates, errors = [], [], []
    for xml in xmls:
        try:
            inv = parse(xml.data)
        except InvoiceParseError as e:
            folder = documents_dir / FOLDERS["error"] / _safe(gmail_msgid)
            path, digest = _write(folder, xml)
            store.add_document(None, gmail_msgid, "xml", xml.filename, path, digest, len(xml.data))
            (folder / "error.json").write_text(json.dumps({"error": str(e), **meta}, ensure_ascii=False, indent=1))
            errors.append(f"{xml.filename}: {e}")
            continue
        if store.invoice_by_cufe(inv.cufe):
            duplicates.append(inv.cufe)
            continue

        status, reason, issues = classify(inv)
        folder = _invoice_folder(documents_dir, status, inv)
        invoice_id = store.add_invoice(inv, status, reason, folder, issues, gmail_msgid)
        for att in [xml] + _pdfs_for(inv, len(xmls), pdfs):
            path, digest = _write(folder, att)
            store.add_document(invoice_id, gmail_msgid, att.kind, att.filename, path, digest, len(att.data))
        filed.append(inv.cufe)

    if filed:
        outcome = Outcome("filed", cufes=filed)
    elif duplicates:
        outcome = Outcome("duplicate", cufes=duplicates)
    else:
        outcome = Outcome("error", "; ".join(errors))
    store.add_message(gmail_msgid, outcome=outcome.kind, detail=outcome.detail or ",".join(outcome.cufes), **meta)
    store.commit()
    return outcome


def _all_mail(imap) -> str:
    _, boxes = imap.list()
    for line in (b.decode() for b in boxes):
        if "\\All" in line:
            return line.rsplit(' "/" ', 1)[-1]
    return '"[Gmail]/All Mail"'


def _msgids(imap, uids: list[bytes]) -> dict[bytes, str]:
    """UID → X-GM-MSGID, fetched in batches without touching message bodies."""
    out = {}
    for i in range(0, len(uids), 200):
        _, data = imap.uid("FETCH", b",".join(uids[i:i + 200]), "(X-GM-MSGID)")
        for item in data:
            line = item if isinstance(item, bytes) else item[0]
            m = re.search(rb"X-GM-MSGID (\d+).*UID (\d+)|UID (\d+).*X-GM-MSGID (\d+)", line or b"")
            if m:
                msgid, uid = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
                out[uid] = msgid.decode()
    return out


def run(since: str, limit: int | None, dry_run: bool) -> dict:
    cfg = settings()
    if not cfg.gmail_user or not cfg.gmail_password:
        sys.exit("Faltan GMAIL_USER / GMAIL_APP_PASSWORD (ver .env)")
    os.umask(0o077)
    store = Store(cfg.db_path, actor="sistema:intake")
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(cfg.gmail_user, cfg.gmail_password)
    tally = {}
    try:
        imap.select(_all_mail(imap), readonly=True)
        _, data = imap.uid("SEARCH", "X-GM-RAW", f'"filename:xml after:{since}"')
        uids = data[0].split()
        pending = [(u, m) for u, m in _msgids(imap, uids).items() if not store.seen_message(m)]
        pending.sort(key=lambda p: int(p[0]))
        if limit:
            pending = pending[:limit]
        print(f"{len(uids)} correos con XML desde {since}; {len(pending)} por procesar")
        for uid, gmail_msgid in pending:
            if dry_run:
                tally["dry_run"] = tally.get("dry_run", 0) + 1
                continue
            _, msg_data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            outcome = process_message(msg_data[0][1], gmail_msgid, store, cfg.documents_dir)
            tally[outcome.kind] = tally.get(outcome.kind, 0) + 1
            if outcome.kind == "error":
                print(f"  error {gmail_msgid}: {outcome.detail}")
    finally:
        imap.logout()
    print("resultado:", tally, "| facturas por estado:", store.counts())
    store.close()
    return tally


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", default=None, help="fecha Gmail YYYY/MM/DD (por defecto COMPRAS_GMAIL_SINCE)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="solo contar, sin descargar ni guardar")
    args = ap.parse_args()
    run(args.since or settings().since, args.limit, args.dry_run)


if __name__ == "__main__":
    main()
