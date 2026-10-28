"""Checks run on every parsed invoice before it reaches the review queue.

Errors block approval; warnings are shown to the reviewer. Duplicate CUFE detection needs the database
and lives with the intake code.
"""
from dataclasses import dataclass
from decimal import Decimal

from .fe_parser import Invoice

PLATYPUS_RUC = "155687144-2-2019"

ERROR, WARNING = "error", "warning"


@dataclass
class Issue:
    code: str
    severity: str
    message: str


def _tolerance(inv: Invoice) -> Decimal:
    # Some PACs send 6-decimal line amounts; allow a cent per line of rounding drift.
    return Decimal("0.01") * max(1, len(inv.lines))


def validate(inv: Invoice, receiver_ruc: str = PLATYPUS_RUC) -> list[Issue]:
    issues = []
    if not inv.cufe:
        issues.append(Issue("no_cufe", ERROR, "El XML no trae CUFE (dId)."))
    if inv.environment != "1":
        issues.append(Issue("test_environment", ERROR, f"Documento de ambiente de pruebas (iAmb={inv.environment})."))
    if inv.receiver_ruc != receiver_ruc:
        if not inv.receiver_ruc and "PLATYPUS" in (inv.receiver_name or "").upper():
            issues.append(Issue("consumer_receipt", WARNING,
                                f"Emitida a «{inv.receiver_name}» como consumidor final, sin RUC de Platypus."))
        else:
            issues.append(Issue("wrong_receiver", ERROR,
                                f"Emitida a {inv.receiver_ruc or 'sin RUC'} ({inv.receiver_name or '-'}), no a Platypus."))
    if not inv.lines:
        issues.append(Issue("no_lines", ERROR, "La factura no tiene líneas."))
    if not inv.authorized:
        issues.append(Issue("no_protocol", WARNING,
                            "El archivo no trae el protocolo de autorización DGI; verificar el CUFE en la DGI."))

    tol = _tolerance(inv)
    net = sum((ln.net for ln in inv.lines), Decimal("0"))
    if abs(net - inv.net_total) > tol:
        issues.append(Issue("net_mismatch", WARNING, f"Suma de líneas {net} ≠ dTotNeto {inv.net_total}."))
    itbms = sum((ln.itbms for ln in inv.lines), Decimal("0"))
    if abs(itbms - inv.itbms_total) > tol:
        issues.append(Issue("itbms_mismatch", WARNING, f"ITBMS de líneas {itbms} ≠ dTotITBMS {inv.itbms_total}."))
    expected = (inv.net_total + inv.itbms_total + inv.isc_total + inv.oti_total
                + inv.other_charges - inv.discount_total)
    if abs(expected - inv.total) > tol:
        issues.append(Issue("total_mismatch", WARNING,
                            f"Neto + impuestos + cargos − descuentos = {expected} ≠ dVTot {inv.total}."))
    return issues
