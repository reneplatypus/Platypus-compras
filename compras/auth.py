"""Login with a 6-digit code sent by email, and cookie sessions (same model as the tenant portal).

Codes and session tokens are stored only as HMAC hashes. Unknown emails get no code and no hint.
With COMPRAS_DEV_LOGIN=1 the code is printed to the server log instead of emailed.
"""
import hashlib
import hmac
import os
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from .store import Store

CODE_TTL = timedelta(minutes=10)
RESEND_AFTER = timedelta(seconds=60)
SESSION_TTL = timedelta(hours=12)
MAX_ATTEMPTS = 5


def _secret() -> bytes:
    value = os.environ.get("COMPRAS_SECRET", "")
    if len(value) < 32:
        raise RuntimeError("COMPRAS_SECRET falta o es muy corto (mínimo 32 caracteres)")
    return value.encode()


def _hash(value: str) -> str:
    return hmac.new(_secret(), value.encode(), hashlib.sha256).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def send_code_email(email: str, code: str) -> None:
    if os.environ.get("COMPRAS_DEV_LOGIN") == "1":
        print(f"[dev] código de ingreso para {email}: {code}", flush=True)
        return
    msg = EmailMessage()
    msg["Subject"] = f"Código de ingreso a Compras Platypus: {code}"
    msg["From"] = os.environ["GMAIL_USER"]
    msg["To"] = email
    msg.set_content(f"Su código de ingreso es {code}. Vence en 10 minutos.\n\n"
                    "Si no lo solicitó, ignore este correo.")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)


def request_code(store: Store, email: str, send=send_code_email) -> None:
    email = email.strip().lower()
    if not store.user_by_email(email):
        return
    row = store.db.execute("SELECT expires_at FROM login_codes WHERE email = ?", (email,)).fetchone()
    if row and datetime.fromisoformat(row["expires_at"]) - CODE_TTL + RESEND_AFTER > _now():
        return   # one code a minute
    code = f"{secrets.randbelow(10 ** 6):06d}"
    store.db.execute("INSERT OR REPLACE INTO login_codes (email, code_hash, expires_at, attempts) VALUES (?, ?, ?, 0)",
                     (email, _hash(email + ":" + code), (_now() + CODE_TTL).isoformat()))
    store.commit()
    send(email, code)


def verify_code(store: Store, email: str, code: str) -> str | None:
    """Session token on success, else None."""
    email = email.strip().lower()
    row = store.db.execute("SELECT * FROM login_codes WHERE email = ?", (email,)).fetchone()
    user = store.user_by_email(email)
    if not row or not user or datetime.fromisoformat(row["expires_at"]) < _now() or row["attempts"] >= MAX_ATTEMPTS:
        return None
    if not hmac.compare_digest(row["code_hash"], _hash(email + ":" + code.strip())):
        store.db.execute("UPDATE login_codes SET attempts = attempts + 1 WHERE email = ?", (email,))
        store.commit()
        return None
    store.db.execute("DELETE FROM login_codes WHERE email = ?", (email,))
    token = secrets.token_urlsafe(32)
    store.db.execute("INSERT INTO sessions (token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                     (_hash(token), user["id"], (_now() + SESSION_TTL).isoformat(), _now().isoformat()))
    store.audit(email, None, "ingreso")
    store.commit()
    return token


def user_for_token(store: Store, token: str | None):
    if not token:
        return None
    row = store.db.execute(
        "SELECT u.*, s.expires_at AS session_expires FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND u.active = 1", (_hash(token),)).fetchone()
    if not row or datetime.fromisoformat(row["session_expires"]) < _now():
        return None
    return row


def logout(store: Store, token: str | None) -> None:
    if token:
        store.db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))
        store.commit()
