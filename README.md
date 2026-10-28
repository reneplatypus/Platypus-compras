# platypus-compras

Supplier-invoice intake and review for Platypus, Inc., posting approved purchases to InterFuerza;
customer credit (AR) dashboard to follow. See `/opt/interfuerza/docs/HOST_INVENTORY.md` for the host.

## Status

Phase 1, read-only: nothing writes to Gmail or InterFuerza.

- `compras/fe_parser.py` — Panama DGI e-invoice XML (format 1.00), any PAC wrapper or namespace.
- `compras/costing.py` — line costs for an InterFuerza `purchase`: ITBMS goes to `Impuestos`;
  ISC and other taxes are folded into line costs; lines always add up to the invoice total.
- `compras/validate.py` — receiver RUC is Platypus, production environment, DGI protocol, totals.
- `compras/intake.py` — Gmail over IMAP (read-only, BODY.PEEK); files XML/PDF under
  `data/documents/{unposted,rejected,errors}/YYYY/MM/<ruc>_<number>/`, indexed by CUFE in `data/compras.db`.
- `compras/revalidate.py` — re-run validation after a rule change and move folders.
- `compras/interfuerza.py` — GET-only InterFuerza client, spaced to 1 request/s.
- `compras/posted_check.py` — marks each invoice `if_state` = posted / possible / not_posted by matching
  InterFuerza purchases on supplier, exact total and date (purchases carry no CUFE or RUC).

## Commands

    python3 -m compras.intake [--since YYYY/MM/DD] [--dry-run]   # new Gmail invoices
    python3 -m compras.posted_check [--cached]                    # refresh the InterFuerza marker
    python3 -m compras.revalidate

Settings come from `.env` (GMAIL_USER, GMAIL_APP_PASSWORD; optional COMPRAS_DATA_DIR,
COMPRAS_GMAIL_SINCE). The InterFuerza token is read from `/opt/interfuerza/interfuerzatoken`.

## Tests

    python3 -m unittest -v

Uses only the standard library. `RealSamplesTest` also runs every XML under `/opt/interfuerza/samples`
(override with `COMPRAS_SAMPLES`) and is skipped when that folder is absent. Real supplier
documents stay out of git.
