"""Line costs for an InterFuerza purchase.

InterFuerza's `purchase` takes taxes only as one header amount (Impuestos). Policy (René, 2026-10-28):
Impuestos carries ITBMS only; ISC and other taxes (OTI) are folded into each line's cost. Anything the
lines don't explain (header-level charges, discounts, rounding) is spread over the lines in proportion
to their cost and reported as `residual`, so SubTotal + Impuestos always equals the invoice total.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from .fe_parser import Invoice

CENT = Decimal("0.01")


@dataclass
class CostLine:
    seq: int
    description: str
    qty: Decimal
    cost: Decimal        # amount to book on the line's account, 2 decimals
    folded_taxes: Decimal  # ISC + OTI included in cost


@dataclass
class PurchaseAmounts:
    lines: list[CostLine]
    subtotal: Decimal
    itbms: Decimal
    total: Decimal
    residual: Decimal    # spread across lines; non-zero means the reviewer should look


def _cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def purchase_amounts(inv: Invoice) -> PurchaseAmounts:
    itbms = _cents(inv.itbms_total)
    total = _cents(inv.total)
    subtotal = total - itbms

    raw = [ln.net + ln.isc + ln.oti for ln in inv.lines]
    residual = subtotal - sum(raw, Decimal("0"))
    base = sum(raw, Decimal("0"))
    if base:
        shares = [r + residual * r / base for r in raw]
    else:
        shares = [subtotal / len(raw)] * len(raw) if raw else []
    costs = [_cents(s) for s in shares]

    # Rounding remainder goes to the largest line so the lines add up to the cent.
    if costs:
        biggest = max(range(len(costs)), key=lambda i: abs(costs[i]))
        costs[biggest] += subtotal - sum(costs, Decimal("0"))

    lines = [CostLine(seq=ln.seq, description=ln.description, qty=ln.qty, cost=c,
                      folded_taxes=_cents(ln.isc + ln.oti))
             for ln, c in zip(inv.lines, costs)]
    return PurchaseAmounts(lines=lines, subtotal=subtotal, itbms=itbms, total=total,
                           residual=_cents(residual))
