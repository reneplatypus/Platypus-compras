"""Parser for Panama DGI electronic invoices (Factura Electrónica, format 1.00).

PACs wrap the same rFE document differently (rFE, rContFe with the DGI protocol, rRetEnviConsFe) and
use three different namespaces (or none), so lookups here match on local tag names only.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import xml.etree.ElementTree as ET

# iDoc
DOC_TYPES = {
    "01": "factura",
    "02": "factura_importacion",
    "03": "factura_exportacion",
    "04": "nota_credito",
    "05": "nota_debito",
    "06": "nota_credito_generica",
    "07": "nota_debito_generica",
    "08": "factura_zona_franca",
    "09": "reembolso",
    "10": "factura_operacion_extranjera",
}
CREDIT_TYPES = {"04", "06"}

# dTasaITBMS
ITBMS_RATES = {"00": Decimal("0"), "01": Decimal("0.07"), "02": Decimal("0.10"), "03": Decimal("0.15")}

ZERO = Decimal("0")


class InvoiceParseError(ValueError):
    pass


@dataclass
class Line:
    seq: int
    code: str | None
    description: str
    qty: Decimal
    unit_price: Decimal
    net: Decimal                 # dPrItem: price x qty after line discount, before taxes
    itbms_rate: str | None
    itbms: Decimal
    isc: Decimal
    oti: Decimal                 # sum of gOTIItem (other taxes on the line)
    total: Decimal               # dValTotItem
    cpbs: str | None             # DGI product/service classification code


@dataclass
class Invoice:
    cufe: str
    doc_type: str                # iDoc code, see DOC_TYPES
    number: str
    issued_at: datetime
    environment: str             # iAmb: 1 production, 2 test
    issuer_ruc: str
    issuer_dv: str | None
    issuer_name: str
    receiver_ruc: str | None
    receiver_name: str | None
    receiver_type: str | None    # iTipoRec: 01 contribuyente, 02 consumidor final, ...
    lines: list[Line]
    net_total: Decimal
    itbms_total: Decimal
    isc_total: Decimal
    oti_total: Decimal
    other_charges: Decimal
    discount_total: Decimal
    total: Decimal
    due_dates: list[tuple[datetime, Decimal]] = field(default_factory=list)
    referenced_cufes: list[str] = field(default_factory=list)
    authorized: bool = False     # the file carries the DGI authorization protocol

    @property
    def is_credit(self) -> bool:
        return self.doc_type in CREDIT_TYPES


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(el, name):
    if el is None:
        return None
    for child in el.iter():
        if _local(child.tag) == name:
            return child
    return None


def _findall(el, name):
    return [child for child in el.iter() if _local(child.tag) == name]


def _text(el, name):
    found = _find(el, name)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _dec(el, name) -> Decimal:
    value = _text(el, name)
    return Decimal(value) if value else ZERO


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value)


def parse(data: bytes) -> Invoice:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise InvoiceParseError(f"XML mal formado: {e}") from e

    fe = root if _local(root.tag) == "rFE" else _find(root, "rFE")
    if fe is None:
        raise InvoiceParseError(f"No es una factura electrónica DGI (raíz {_local(root.tag)})")
    gen = _find(fe, "gDGen")
    tot = _find(fe, "gTot")
    if gen is None or tot is None:
        raise InvoiceParseError("Faltan gDGen o gTot")

    cufe = _text(fe, "dId")
    emis, rec = _find(gen, "gEmis"), _find(gen, "gDatRec")

    lines = []
    for item in _findall(fe, "gItem"):
        lines.append(Line(
            seq=int(_text(item, "dSecItem") or len(lines) + 1),
            code=_text(item, "dCodProd"),
            description=_text(item, "dDescProd") or "",
            qty=_dec(item, "dCantCodInt"),
            unit_price=_dec(item, "dPrUnit"),
            net=_dec(item, "dPrItem"),
            itbms_rate=_text(item, "dTasaITBMS"),
            itbms=_dec(item, "dValITBMS"),
            isc=_dec(item, "dValISC"),
            oti=sum((_dec(o, "dValOTI") for o in _findall(item, "gOTIItem")), ZERO),
            total=_dec(item, "dValTotItem"),
            cpbs=_text(item, "dCodCPBScmp") or _text(item, "dCodCPBSabr"),
        ))

    due_dates = [(_date(_text(p, "dFecItPlazo")), _dec(p, "dValItPlazo"))
                 for p in _findall(tot, "gPagPlazo") if _text(p, "dFecItPlazo")]
    referenced = [el.text.strip() for el in fe.iter()
                  if _local(el.tag).startswith("dCUFE") and _local(el.tag).endswith("Ref") and el.text]

    return Invoice(
        cufe=cufe,
        doc_type=_text(gen, "iDoc"),
        number=_text(gen, "dNroDF"),
        issued_at=_date(_text(gen, "dFechaEm")),
        environment=_text(gen, "iAmb"),
        issuer_ruc=_text(emis, "dRuc"),
        issuer_dv=_text(emis, "dDV"),
        issuer_name=_text(emis, "dNombEm"),
        receiver_ruc=_text(rec, "dRuc"),
        receiver_name=_text(rec, "dNombRec"),
        receiver_type=_text(rec, "iTipoRec"),
        lines=lines,
        net_total=_dec(tot, "dTotNeto"),
        itbms_total=_dec(tot, "dTotITBMS"),
        isc_total=_dec(tot, "dTotISC"),
        oti_total=sum((_dec(o, "dValOTITotal") for o in _findall(tot, "gOTITotal")), ZERO),
        other_charges=_dec(tot, "dTotOtrosGastos"),
        discount_total=_dec(tot, "dTotDesc"),
        total=_dec(tot, "dVTot"),
        due_dates=due_dates,
        referenced_cufes=referenced,
        authorized=_find(root, "gInfProt") is not None or _find(root, "dProtAut") is not None,
    )
