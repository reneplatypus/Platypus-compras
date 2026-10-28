"""Review screen for supplier invoices.

Run (development, localhost only):
    COMPRAS_DEV_LOGIN=1 .venv/bin/uvicorn compras.web.app:app --host 127.0.0.1 --port 8400
"""
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import auth, review
from ..config import settings
from ..store import Store

HERE = Path(__file__).parent
COOKIE = "compras_sesion"
PAGE = 50

STATUS_LABELS = {"needs_review": "Por revisar", "approved": "Aprobada", "imported": "En InterFuerza",
                 "rejected": "Rechazada", "error": "Error"}
IF_LABELS = {"posted": "Registrada", "possible": "Posible", "not_posted": "No registrada", None: "Sin verificar"}

cfg = settings()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(STATUS_LABELS=STATUS_LABELS, IF_LABELS=IF_LABELS)


def money(value) -> str:
    try:
        return f"{Decimal(str(value)):,.2f}"
    except Exception:
        return str(value)


templates.env.filters["money"] = money


@app.middleware("http")
async def security(request: Request, call_next):
    # Same-origin check for every form post (cookies are also SameSite=Strict).
    if request.method == "POST":
        origin = request.headers.get("origin") or request.headers.get("referer")
        if not origin or urlparse(origin).netloc != request.headers.get("host"):
            return RedirectResponse("/", status_code=303)
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Content-Security-Policy"] = ("default-src 'self'; frame-ancestors 'self'; img-src 'self' data:; "
                                                   "style-src 'self'; script-src 'self'")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def get_store():
    store = Store(cfg.db_path)
    try:
        yield store
    finally:
        store.close()


def master() -> review.MasterData:
    return review.MasterData(cfg.data_dir / "interfuerza")


class LoginRequired(Exception):
    pass


@app.exception_handler(LoginRequired)
async def to_login(request: Request, exc: LoginRequired):
    return RedirectResponse("/ingreso", status_code=303)


def current_user(request: Request, store: Store = Depends(get_store)):
    user = auth.user_for_token(store, request.cookies.get(COOKIE))
    if not user:
        raise LoginRequired()
    return user


def require(user, roles: set, what: str):
    if user["role"] not in roles:
        raise HTTPException(403, f"Su rol ({user['role']}) no permite {what}")


def back(invoice_id: int, error: str | None = None, ok: str | None = None):
    query = urlencode({k: v for k, v in (("error", error), ("ok", ok)) if v})
    return RedirectResponse(f"/facturas/{invoice_id}" + (f"?{query}" if query else ""), status_code=303)


# login

@app.get("/ingreso")
def login_page(request: Request, email: str = ""):
    return templates.TemplateResponse(request, "login.html", {"email": email, "step": "code" if email else "email"})


@app.post("/ingreso")
def login_email(request: Request, email: str = Form(...), store: Store = Depends(get_store)):
    auth.request_code(store, email)
    return templates.TemplateResponse(request, "login.html", {"email": email.strip().lower(), "step": "code"})


@app.post("/ingreso/codigo")
def login_code(request: Request, email: str = Form(...), code: str = Form(...), store: Store = Depends(get_store)):
    token = auth.verify_code(store, email, code)
    if not token:
        return templates.TemplateResponse(request, "login.html", {"email": email, "step": "code",
                                                                  "error": "Código inválido o vencido"})
    response = RedirectResponse("/facturas", status_code=303)
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=int(auth.SESSION_TTL.total_seconds()),
                        secure=request.url.scheme == "https")
    return response


@app.get("/salir")
def logout(request: Request, store: Store = Depends(get_store)):
    auth.logout(store, request.cookies.get(COOKIE))
    response = RedirectResponse("/ingreso", status_code=303)
    response.delete_cookie(COOKIE)
    return response


# invoices

@app.get("/")
def home():
    return RedirectResponse("/facturas", status_code=303)


@app.get("/facturas")
def invoice_list(request: Request, status: str = "needs_review", if_state: str = "", q: str = "", month: str = "",
                 page: int = 1, user=Depends(current_user), store: Store = Depends(get_store)):
    page = max(page, 1)
    rows, count, amount = store.search(status=status or None, if_state=if_state or None, supplier=q.strip() or None,
                                       month=month or None, limit=PAGE, offset=(page - 1) * PAGE)
    counts = dict(store.db.execute("SELECT status, COUNT(*) FROM invoices GROUP BY status").fetchall())
    months = [r[0] for r in store.db.execute(
        "SELECT DISTINCT substr(issued_at, 1, 7) m FROM invoices ORDER BY m DESC").fetchall()]
    filters = dict(status=status, if_state=if_state, q=q, month=month)
    return templates.TemplateResponse(request, "lista.html", dict(
        user=user, rows=rows, count=count, amount=amount, counts=counts, months=months, filters=filters, page=page,
        pages=max(1, -(-count // PAGE)), query=lambda **kw: urlencode({**filters, **kw})))


@app.get("/facturas/{invoice_id}")
def invoice_detail(request: Request, invoice_id: int, error: str = "", ok: str = "", user=Depends(current_user),
                   store: Store = Depends(get_store)):
    md = master()
    try:
        info = review.detail(store, md, invoice_id)
    except review.ReviewError as e:
        raise HTTPException(404, str(e))
    return templates.TemplateResponse(request, "factura.html", dict(
        user=user, master=md, error=error, ok=ok, can_edit=user["role"] in review.CAN_EDIT,
        can_approve=user["role"] in review.CAN_APPROVE,
        blockers=review.approval_blockers(info, md, check_saved=False), **info))


def _action(invoice_id: int, user, roles: set, what: str, fn, ok: str):
    require(user, roles, what)
    try:
        fn()
    except review.ReviewError as e:
        return back(invoice_id, error=str(e))
    return back(invoice_id, ok=ok)


@app.post("/facturas/{invoice_id}/lineas")
async def save_lines(request: Request, invoice_id: int, user=Depends(current_user), store: Store = Depends(get_store)):
    form = await request.form()
    try:
        parts = review.parse_parts(list(form.multi_items()))
    except review.ReviewError as e:
        return back(invoice_id, error=str(e))
    return _action(invoice_id, user, review.CAN_EDIT, "editar",
                   lambda: review.save_lines(store, master(), invoice_id, user["email"], parts), "Cuentas guardadas")


@app.post("/facturas/{invoice_id}/aprobar")
async def approve(request: Request, invoice_id: int, user=Depends(current_user), store: Store = Depends(get_store)):
    """Sent by the lines form's "Guardar y aprobar": saves the accounts shown, then approves."""
    form = await request.form()
    try:
        parts = review.parse_parts(list(form.multi_items()))
    except review.ReviewError as e:
        return back(invoice_id, error=str(e))
    return _action(invoice_id, user, review.CAN_APPROVE, "aprobar",
                   lambda: review.save_and_approve(store, master(), invoice_id, user["email"], parts,
                                                   cfg.documents_dir), "Aprobada")


@app.post("/facturas/{invoice_id}/rechazar")
def reject(invoice_id: int, reason: str = Form(""), user=Depends(current_user), store: Store = Depends(get_store)):
    return _action(invoice_id, user, review.CAN_APPROVE, "rechazar",
                   lambda: review.reject(store, invoice_id, user["email"], reason, cfg.documents_dir), "Rechazada")


@app.post("/facturas/{invoice_id}/reabrir")
def reopen(invoice_id: int, user=Depends(current_user), store: Store = Depends(get_store)):
    return _action(invoice_id, user, review.CAN_APPROVE, "reabrir",
                   lambda: review.reopen(store, invoice_id, user["email"], cfg.documents_dir), "Devuelta a revisión")


@app.post("/facturas/{invoice_id}/registrada")
def mark_posted(invoice_id: int, purchase_id: str = Form(""), user=Depends(current_user),
                store: Store = Depends(get_store)):
    return _action(invoice_id, user, review.CAN_EDIT, "marcar como registrada",
                   lambda: review.mark_posted(store, invoice_id, user["email"], purchase_id, cfg.documents_dir),
                   "Marcada como registrada en InterFuerza")


@app.post("/facturas/{invoice_id}/descartar")
def dismiss(invoice_id: int, user=Depends(current_user), store: Store = Depends(get_store)):
    return _action(invoice_id, user, review.CAN_EDIT, "descartar coincidencias",
                   lambda: review.dismiss_match(store, invoice_id, user["email"]), "Coincidencia descartada")


@app.post("/facturas/{invoice_id}/proveedor")
def set_provider(invoice_id: int, provider_id: str = Form(""), user=Depends(current_user),
                 store: Store = Depends(get_store)):
    return _action(invoice_id, user, review.CAN_EDIT, "asignar proveedores",
                   lambda: review.set_provider(store, master(), invoice_id, user["email"], provider_id.strip()),
                   "Proveedor asignado")


@app.get("/documentos/{doc_id}")
def document(doc_id: int, user=Depends(current_user), store: Store = Depends(get_store)):
    doc = store.document(doc_id)
    if not doc:
        raise HTTPException(404)
    path = Path(doc["path"]).resolve()
    if not path.is_relative_to(cfg.documents_dir.resolve()) or not path.is_file():
        raise HTTPException(404)
    media = "application/pdf" if doc["kind"] == "pdf" else "application/xml"
    return FileResponse(path, media_type=media, filename=doc["filename"], content_disposition_type="inline")
