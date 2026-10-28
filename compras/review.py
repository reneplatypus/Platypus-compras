"""Review workflow: invoice detail, per-line accounts with splits, approve / reject / mark posted.

A line can be split across several accounts (e.g. part expense, part cost of sales); the parts of a line
must add up to its cost, which already includes folded ISC/OTI (see costing.py). Approval locks the
invoice for the posting step; nothing here writes to InterFuerza.
"""
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .costing import purchase_amounts
from .fe_parser import DOC_TYPES, parse
from .store import Store, _now

ROLES = ("viewer", "preparer", "approver", "admin")
CAN_EDIT = {"preparer", "approver", "admin"}
CAN_APPROVE = {"approver", "admin"}
# Account types offered for purchase lines, in display order.
LINE_ACCOUNT_TYPES = ("Expenses", "Costs Of Sales", "Fixed Assets", "Other Current Assets", "Other Current",
                      "Other Assets")
CENT = Decimal("0.01")


class ReviewError(ValueError):
    pass


class MasterData:
    """InterFuerza accounts, providers and past purchases, from the cache posted_check writes."""

    def __init__(self, cache_dir: Path):
        def load(name, default):
            path = cache_dir / name
            return json.loads(path.read_text()) if path.is_file() else default

        accounts = load("accounts.json", [])
        self.accounts = {a["Internal_Code"]: a for a in accounts}
        self.line_accounts = sorted(
            (a for a in accounts if a.get("Inactive") != "YES" and a.get("Type") in LINE_ACCOUNT_TYPES),
            key=lambda a: (LINE_ACCOUNT_TYPES.index(a["Type"]), a.get("Codigo") or ""))
        self.providers = load("providers.json", {})
        self._by_provider = defaultdict(Counter)
        for rec in load("purchases.json", []):
            head = rec.get("Purchase") or {}
            for ln in rec.get("Lines") or []:
                if ln.get("Account"):
                    self._by_provider[head.get("Proveedor")][ln["Account"]] += 1

    def label(self, code: str) -> str:
        a = self.accounts.get(code)
        return f"{a['Codigo']} {a['Name']}" if a else code

    def suggestions(self, provider_id: str | None, limit: int = 3) -> list[str]:
        """Accounts this provider's past purchases used most, active ones only."""
        counter = self._by_provider.get(provider_id) or Counter()
        return [code for code, _ in counter.most_common() if code in self.accounts
                and self.accounts[code].get("Inactive") != "YES"][:limit]


@dataclass
class LineView:
    seq: int
    description: str
    qty: Decimal
    cost: Decimal
    folded_taxes: Decimal
    parts: list[tuple[str, Decimal]] = field(default_factory=list)   # (account, amount)


def provider_for(store: Store, issuer_ruc: str):
    return store.supplier_map().get(issuer_ruc)


def detail(store: Store, master: MasterData, invoice_id: int) -> dict:
    row = store.invoice(invoice_id)
    if row is None:
        raise ReviewError("Factura no encontrada")
    docs = store.documents(invoice_id)
    xml = next(d for d in docs if d["kind"] == "xml")
    inv = parse(Path(xml["path"]).read_bytes())
    amounts = purchase_amounts(inv)
    mapping = provider_for(store, row["issuer_ruc"])
    suggested = master.suggestions(mapping["provider_id"] if mapping else None)

    saved = defaultdict(list)
    for ln in store.lines(invoice_id):
        saved[ln["seq"]].append((ln["account"], Decimal(ln["amount"])))
    lines = []
    for src, cost in zip(inv.lines, amounts.lines):
        parts = saved.get(src.seq) or [(suggested[0] if suggested else "", cost.cost)]
        lines.append(LineView(src.seq, src.description, src.qty, cost.cost, cost.folded_taxes, parts))
    return dict(row=row, inv=inv, doc_type=DOC_TYPES.get(inv.doc_type, inv.doc_type), amounts=amounts,
                lines=lines, saved=bool(saved), issues=json.loads(row["issues"] or "[]"), docs=docs,
                pdfs=[d for d in docs if d["kind"] == "pdf"], mapping=mapping, suggested=suggested,
                history=store.history(invoice_id))


def parse_parts(form_items: list[tuple[str, str]]) -> list[tuple[int, str, Decimal]]:
    """Form fields account_<seq> / amount_<seq>, repeated once per split, in order."""
    accounts, amounts = defaultdict(list), defaultdict(list)
    for key, value in form_items:
        if key.startswith("account_"):
            accounts[int(key[8:])].append(value.strip())
        elif key.startswith("amount_"):
            amounts[int(key[7:])].append(value.strip())
    out = []
    for seq in sorted(accounts):
        if len(accounts[seq]) != len(amounts[seq]):
            raise ReviewError(f"Línea {seq}: cuentas y montos no coinciden")
        for account, amount in zip(accounts[seq], amounts[seq]):
            try:
                value = Decimal(amount.replace(",", "")).quantize(CENT)
            except InvalidOperation:
                raise ReviewError(f"Línea {seq}: monto inválido «{amount}»") from None
            out.append((seq, account, value))
    return out


def check_parts(lines: list[LineView], parts: list[tuple[int, str, Decimal]], master: MasterData,
                complete: bool) -> list[str]:
    """Problems with a set of line parts. complete=True (approval) also requires every account filled."""
    problems = []
    by_seq = defaultdict(list)
    for seq, account, amount in parts:
        by_seq[seq].append((account, amount))
    for line in lines:
        got = by_seq.get(line.seq, [])
        total = sum((a for _, a in got), Decimal("0"))
        if total != line.cost:
            problems.append(f"Línea {line.seq}: las partes suman {total}, la línea cuesta {line.cost}")
        for account, amount in got:
            if account and account not in master.accounts:
                problems.append(f"Línea {line.seq}: la cuenta {account} no existe en InterFuerza")
            elif complete and not account:
                problems.append(f"Línea {line.seq}: falta la cuenta")
            if amount == 0 and len(got) > 1:
                problems.append(f"Línea {line.seq}: hay una parte en 0")
    extra = set(by_seq) - {ln.seq for ln in lines}
    if extra:
        problems.append(f"Líneas desconocidas: {sorted(extra)}")
    return problems


def _require(row, statuses, what):
    if row["status"] not in statuses:
        raise ReviewError(f"No se puede {what} una factura en estado {row['status']}")


def save_lines(store: Store, master: MasterData, invoice_id: int, user: str, parts) -> None:
    info = detail(store, master, invoice_id)
    _require(info["row"], ("needs_review",), "editar")
    problems = check_parts(info["lines"], parts, master, complete=False)
    if problems:
        raise ReviewError("; ".join(problems))
    numbered, counter = [], Counter()
    for seq, account, amount in parts:
        counter[seq] += 1
        numbered.append((seq, counter[seq], account, str(amount)))
    store.replace_lines(invoice_id, numbered)          # audits the before / after allocation
    store.update_invoice(invoice_id, reviewed_by=user, reviewed_at=_now())
    store.commit()


def approval_blockers(info: dict, master: MasterData, check_saved: bool = True) -> list[str]:
    """Why this invoice can't be approved yet (empty when it can). check_saved=False when the screen is
    about to save the lines it shows, so only the other conditions matter."""
    row = info["row"]
    blockers = []
    if row["status"] != "needs_review":
        return [f"La factura está en estado {row['status']}"]
    if row["if_state"] in ("posted", "possible"):
        blockers.append(f"InterFuerza podría tener ya esta compra ({row['if_purchase_id']}): "
                        "confirme «Sí, es la compra» o «No es esta compra»")
    if not info["mapping"]:
        blockers.append("Asigne el proveedor de InterFuerza")
    blockers += [i["message"] for i in info["issues"] if i["severity"] == "error"]
    if check_saved:
        if not info["saved"]:
            blockers.append("Guarde las cuentas de las líneas")
        else:
            parts = [(ln.seq, account, amount) for ln in info["lines"] for account, amount in ln.parts]
            blockers += check_parts(info["lines"], parts, master, complete=True)
    return blockers


def approve(store: Store, master: MasterData, invoice_id: int, user: str, documents_dir: Path) -> None:
    info = detail(store, master, invoice_id)
    _require(info["row"], ("needs_review",), "aprobar")
    blockers = approval_blockers(info, master)
    if blockers:
        raise ReviewError("; ".join(blockers))
    store.set_status(invoice_id, "approved", documents_dir, approved_by=user, approved_at=_now())
    store.audit(user, invoice_id, "aprobar")
    store.commit()


def save_and_approve(store: Store, master: MasterData, invoice_id: int, user: str, parts,
                     documents_dir: Path) -> None:
    """The screen's single "Guardar y aprobar": nothing is saved unless approval would also succeed."""
    info = detail(store, master, invoice_id)
    _require(info["row"], ("needs_review",), "aprobar")
    blockers = approval_blockers(info, master, check_saved=False) + check_parts(info["lines"], parts, master,
                                                                                complete=True)
    if blockers:
        raise ReviewError("; ".join(blockers))
    save_lines(store, master, invoice_id, user, parts)
    approve(store, master, invoice_id, user, documents_dir)


def reject(store: Store, invoice_id: int, user: str, reason: str, documents_dir: Path) -> None:
    row = store.invoice(invoice_id)
    _require(row, ("needs_review", "approved"), "rechazar")
    if not reason.strip():
        raise ReviewError("Indique el motivo del rechazo")
    store.set_status(invoice_id, "rejected", documents_dir, status_reason=reason.strip(),
                     approved_by=None, approved_at=None)
    store.audit(user, invoice_id, "rechazar", reason.strip())
    store.commit()


def reopen(store: Store, invoice_id: int, user: str, documents_dir: Path) -> None:
    row = store.invoice(invoice_id)
    _require(row, ("approved", "rejected"), "reabrir")
    store.set_status(invoice_id, "needs_review", documents_dir, status_reason=None, approved_by=None,
                     approved_at=None)
    store.audit(user, invoice_id, "reabrir", f"desde {row['status']}")
    store.commit()


def mark_posted(store: Store, invoice_id: int, user: str, purchase_id: str, documents_dir: Path) -> None:
    """The reviewer confirms InterFuerza already has this purchase (e.g. a 'possible' match)."""
    row = store.invoice(invoice_id)
    _require(row, ("needs_review", "rejected"), "marcar como registrada")
    purchase_id = purchase_id.strip()
    if not purchase_id:
        raise ReviewError("Indique el número de compra de InterFuerza")
    store.set_status(invoice_id, "imported", documents_dir, posted_via="manual", if_state="posted",
                     if_purchase_id=purchase_id, status_reason=f"Registrada en InterFuerza: compra {purchase_id}")
    store.audit(user, invoice_id, "marcar_registrada", purchase_id)
    store.commit()


def dismiss_match(store: Store, invoice_id: int, user: str) -> None:
    """The reviewer says the suggested InterFuerza purchase is not this invoice; later checks skip it."""
    row = store.invoice(invoice_id)
    _require(row, ("needs_review",), "descartar la coincidencia de")
    if row["if_state"] not in ("posted", "possible") or not row["if_purchase_id"]:
        raise ReviewError("No hay coincidencia que descartar")
    store.update_invoice(invoice_id, if_state="not_posted", if_dismissed=row["if_purchase_id"],
                         if_match_note=f"Coincidencia con compra {row['if_purchase_id']} descartada por {user}")
    store.audit(user, invoice_id, "descartar_coincidencia", row["if_purchase_id"])
    store.commit()


def set_provider(store: Store, master: MasterData, invoice_id: int, user: str, provider_id: str) -> None:
    row = store.invoice(invoice_id)
    if provider_id not in master.providers:
        raise ReviewError(f"Proveedor {provider_id} no existe en InterFuerza")
    store.set_supplier(row["issuer_ruc"], row["issuer_name"], provider_id, master.providers[provider_id],
                       "manual", 1.0)                  # audits the before / after mapping
    store.commit()
