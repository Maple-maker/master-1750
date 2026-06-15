# master-1750-tool

A small Flask web tool that turns a **batch of child DD Form 1750 PDFs** into a
single rendered **Master DD Form 1750 packing list**.

You drop the child 1750s, the tool reads each **filename** to identify the Major
End Item (Model / LIN / Serial), aggregates identical equipment into one box row
(qty = count, all serials listed), you fill the header once, and it generates the
master packing list PDF. A built-in auditor validates the result.

## What it does

1. **Upload** — drag/drop or browse a batch of child 1750 PDFs.
2. **Parse** — each filename is classified by token *shape* (order-independent)
   into Model / LIN / Serial / Bumper. The NSN is best-effort sniffed from the
   child PDF body when present.
3. **Aggregate** — identical `(LIN, Model)` collapse into one box: `qty = count`,
   serials listed comma-separated.
4. **Review** — edit any cell in the table; rows missing a LIN/Model are
   highlighted for review. Add/remove rows; box numbers re-sequence 1..N.
5. **Generate** — renders the master DD1750 (two-line rows, paginated at 18
   rows/page, `NOTHING FOLLOWS` marker) onto the official blank template.
6. **Audit** — checks box numbering, required fields, Packer ≠ Signer, serial
   presence, and qty/serial consistency; reports ERRORs and WARNINGs.

## Run locally

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open **http://localhost:8000**.

(Set `PORT=8055` before `python app.py` to use a different port.)

## Deploy (Railway)

Standard NIXPACKS build. `Procfile` starts gunicorn bound to `$PORT`; the
health check path is **`/api/health`**. `runtime.txt` pins Python 3.11.

## Filename parser logic

The parser is **shape-based and order-independent** — the *shape* of each token,
not its position, decides what it is:

1. **Normalize** — strip `.pdf`; strip `DD1750` / `1750` form stamps even when
   glued by `_` or `.` (e.g. `M249_DD1750`, `DD1750.T92889`); convert `_`/`.` to
   spaces; collapse whitespace.
2. **Explicit serial marker** (highest priority) — `SN_xxxx`, `SN: xxxx`,
   `SN_ xxxx` (space after underscore), or even `2SN_xxxx` glued to a model
   fragment → that value is the serial.
3. **Bumper** — `^B\d{1,3}[A-Z]?$` (B33, B5, B34S).
4. **LIN** — `^[A-Z]\d{5}$` (T88915, E05003, A22496); first match wins, and any
   later LIN-shaped duplicate is consumed so it never leaks into the model.
5. **Serial (by shape)** — pure-numeric ≥3 digits, hyphenated registration
   (`J-K1234567-AB`), or mixed alphanumeric ≥7 chars (`1ABCD2345678`,
   `A0012345`, `W0009999`).
6. **Model** — everything left, joined in original order. Model-number fragments
   like `M983A4`, `M1113`, `M249`, `AN/PAS-13D` correctly fall through here
   (they don't match the strict LIN/serial shapes).

`needs_review = True` whenever the LIN or Model is missing, so the UI flags it for
a human fix. The editable table is the safety net — the parser is never silently
wrong.

## Files

- `app.py` — Flask routes (`/`, `/upload`, `/generate`, `/audit`, `/api/health`).
- `master_core.py` — filename parser, NSN sniffer, aggregator, header builder, auditor.
- `render_core.py` — PDF render/merge/pagination, adapted from the proven v25 renderer.
- `blank_1750.pdf` — the flattened official DD1750 render template.
- `templates/index.html` — single-page UI (inlined CSS + JS).
