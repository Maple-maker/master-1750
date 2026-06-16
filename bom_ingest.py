"""
bom_ingest.py — Normalized BOM ingest wrapper for the master-1750-tool.

Exposes a single public function:

    ingest_bom(pdf_path, nomenclature="") -> dict

The returned dict is the canonical "Bom" schema that downstream code depends on.
It is never allowed to raise — on total failure it returns the dict with empty
items and an entry in "errors".

Extraction strategy (waterfall):
  1. Call dd1750_core.extract_items_from_pdf()  — v25's multi-format extractor
     (handles GCSS-Army Standard, EPP, DA-2062, form-fields, optional OCR).
  2. If that yields zero items, fall back to bom_parser.parse_bom_pdf()
     (AcroForm state-machine reader; filters to OH-Qty > 0 components).
  3. Map whichever source won into the canonical dict.

This module calls ONLY the extraction half of dd1750_core — never its
rendering functions (generate_*, format_*). Rendering is owned by render_core.
"""

import os
import uuid

# ── Primary extractor (v25 multi-format) ──────────────────────────────────────
from dd1750_core import extract_items_from_pdf, ExtractionResult

# ── Fallback extractor (AcroForm form-field reader) ───────────────────────────
import bom_parser as _bom_parser

# ── Last-resort metadata recovery from the FILENAME ───────────────────────────
# v25's LIN regex is unreliable (it can match "line-out" body text before the
# real LIN field), and many scanned BOMs have no extractable serial. But the
# filenames reliably encode both — e.g. "0137 A05023 INTERROGATOR SET.pdf"
# carries LIN A05023, and "103133 M39263 LMG 5.56MM M249.pdf" carries serial
# 103133 + LIN M39263. master_core.parse_filename is the shape-based classifier
# already validated against 145 real filenames, so we reuse it as a fallback.
from master_core import parse_filename as _parse_filename


def _build_items(rows):
    """
    Build the canonical item list from raw extractor rows.

    `rows` is a list of tuples: (description, nsn, unit_of_issue, oh_qty, auth_qty).

    Quantity rules (per the packing workflow):
      * Quantity comes from the BOM's ON-HAND (OH QTY) column so the 1750
        reflects the property ACTUALLY present.
      * If OH QTY is exactly 0, the item is not physically on hand — OMIT the
        whole line.
      * If OH QTY is unknown (-1, i.e. the source had no OH column), fall back
        to the authorized quantity so we don't silently drop real items.
    Line numbers are re-sequenced 1..N AFTER omissions so they stay contiguous.
    """
    items = []
    seq = 1
    for desc, nsn, uoi, oh_qty, auth_qty in rows:
        if oh_qty == 0:
            continue  # zero on hand -> not present -> drop the line entirely
        qty = oh_qty if oh_qty > 0 else max(auth_qty, 1)  # -1 unknown -> auth
        items.append({
            "line_no":       seq,
            "description":   desc,
            "nsn":           nsn,
            "qty":           qty,
            "unit_of_issue": uoi or "EA",
        })
        seq += 1
    return items


def _pdf_has_extractable_text(pdf_path: str) -> bool:
    """
    Return True if the PDF has enough real text to try v25 first.
    GCSS-Army AcroForm BOMs have essentially zero extractable text — all
    content lives in form-field annotations.  30 chars is a safe threshold.
    """
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages[:3]:
                if len((page.extract_text() or "").strip()) > 30:
                    return True
        return False
    except Exception:
        return True  # assume text on any error; v25 will handle gracefully


def ingest_bom(pdf_path: str, nomenclature: str = "") -> dict:
    """
    Extract BOM items from a PDF and return a canonical Bom dict.

    Args:
        pdf_path    : Absolute or relative path to the BOM PDF.
        nomenclature: Human label for this BOM (e.g. "B49", "Truck 10T").
                      Falls back to the filename stem when empty.

    Returns:
        dict with keys:
            bom_id, filename, nomenclature, model, serial_number, lin,
            end_item_niin, uic, item_count, items, warnings, errors

        items is a list of:
            { line_no, description, nsn, qty, unit_of_issue }

        warnings / errors are lists of strings.  Never raises.
    """

    # --- Bootstrap the output skeleton so every code path can return it -------
    filename = os.path.basename(pdf_path)
    stem     = os.path.splitext(filename)[0]

    out = {
        "bom_id":        uuid.uuid4().hex,
        "filename":      filename,
        "nomenclature":  nomenclature or stem,
        "model":         "",
        "serial_number": "",
        "lin":           "",
        "end_item_niin": "",
        "uic":           "",
        "item_count":    0,
        "items":         [],
        "zero_on_hand":  False,
        "warnings":      [],
        "errors":        [],
    }

    # ── Route: form-field PDFs → bom_parser first; text PDFs → v25 first ────
    # GCSS-Army AcroForm BOMs have no extractable text — v25 returns 0 items
    # on them.  Routing them directly to bom_parser (page-ordered /Annots walk)
    # avoids the wasted v25 pass and guarantees all pages are captured.
    use_bom_parser_first = not _pdf_has_extractable_text(pdf_path)

    if use_bom_parser_first:
        # ── PHASE 1 (form-field PDF): bom_parser page-ordered walk ───────────
        try:
            bom_record = _bom_parser.parse_bom_pdf(pdf_path)

            out["lin"]           = bom_record.lin    or ""
            out["serial_number"] = bom_record.serial or ""
            out["end_item_niin"] = bom_record.niin   or ""
            out["model"]         = bom_record.desc   or ""

            out["items"] = _build_items([
                (comp.description, comp.nsn, "EA", comp.oh_qty, comp.oh_qty)
                for comp in bom_record.components
            ])

            if not out["items"]:
                out["errors"].append(
                    "bom_parser (page-walk) returned no items. "
                    "PDF may be a scanned image or unsupported format."
                )

        except Exception as exc:
            out["errors"].append(f"bom_parser extraction failed: {exc}")

    else:
        # ── PHASE 1 (text PDF): v25 multi-format extractor ───────────────────
        v25_result: ExtractionResult | None = None

        try:
            v25_result = extract_items_from_pdf(pdf_path, start_page=0)
        except Exception as exc:
            out["errors"].append(f"dd1750_core extraction failed: {exc}")

        if v25_result:
            out["warnings"].extend(v25_result.warnings)
            out["errors"].extend(v25_result.errors)

        primary_items = v25_result.items if v25_result else []

        if primary_items:
            out["items"] = _build_items([
                (item.description, item.nsn, item.unit_of_issue, item.oh_qty, item.qty)
                for item in primary_items
            ])

            meta = v25_result.metadata

            raw_lin = meta.lin or ""
            out["lin"] = raw_lin if len(raw_lin) >= 6 else ""

            raw_model = meta.end_item_description or ""
            _noise_prefixes = ("SER/EQUIP", "PUB/BOM", "FROM:", "TO:", "PUB NUM")
            if any(raw_model.upper().startswith(p) for p in _noise_prefixes):
                raw_model = ""
            out["model"] = raw_model

            out["serial_number"] = meta.serial_equip_no or ""
            out["end_item_niin"] = meta.end_item_niin   or ""
            out["uic"]           = meta.uic             or ""

        else:
            # ── PHASE 2: bom_parser fallback (text PDF with no items from v25) ─
            out["warnings"].append(
                "Primary extractor returned no items; falling back to bom_parser."
            )

            try:
                bom_record = _bom_parser.parse_bom_pdf(pdf_path)

                out["lin"]           = bom_record.lin    or ""
                out["serial_number"] = bom_record.serial or ""
                out["end_item_niin"] = bom_record.niin   or ""
                out["model"]         = bom_record.desc   or ""

                out["items"] = _build_items([
                    (comp.description, comp.nsn, "EA", comp.oh_qty, comp.oh_qty)
                    for comp in bom_record.components
                ])

                if not out["items"]:
                    out["errors"].append(
                        "bom_parser fallback also returned no items. "
                        "PDF may be a scanned image or unsupported format."
                    )

            except Exception as exc:
                out["errors"].append(f"bom_parser fallback failed: {exc}")

    # ── PHASE 3: filename metadata recovery ───────────────────────────────────
    # If the PDF extraction couldn't recover LIN / serial / model, fall back to
    # the filename, which encodes them reliably for this data set. We only FILL
    # blanks — we never overwrite a value the PDF actually produced.
    if not out["lin"] or not out["serial_number"] or not out["model"]:
        parsed = _parse_filename(filename)
        if not out["lin"] and parsed.lin:
            out["lin"] = parsed.lin
            out["warnings"].append(f"LIN '{parsed.lin}' recovered from filename.")
        if not out["serial_number"] and parsed.sn:
            out["serial_number"] = parsed.sn
            out["warnings"].append(f"Serial '{parsed.sn}' recovered from filename.")
        if not out["model"] and parsed.model:
            out["model"] = parsed.model

    # ── PHASE 4: zero-on-hand end-item placeholder ────────────────────────────
    # A BOM whose components are ALL zero-on-hand extracts to an empty item list
    # (every component line is dropped by _build_items).  But the END ITEM itself
    # — an AIR CONDITIONER, a QUADRANT — is still a physical box on the packing
    # list.  Without a line it gets no box number and vanishes from the master.
    # Insert ONE placeholder line representing the end item so it gets a box, a
    # master row, and an individual 1750.  Tag it zero_on_hand so the UI flags it
    # for review and the user can exclude it before generating if it isn't present.
    if not out["items"]:
        label = out["model"] or out["nomenclature"] or stem
        out["items"] = [{
            "line_no":       1,
            "description":   label,
            "nsn":           out["end_item_niin"] or "",
            "qty":           1,
            "unit_of_issue": "EA",
            "zero_on_hand":  True,
        }]
        out["zero_on_hand"] = True
        out["warnings"].append(
            "No on-hand components found; inserted a placeholder end-item line. "
            "Verify this box is physically present, or exclude it before generating."
        )

    # --- Final bookkeeping ----------------------------------------------------
    out["item_count"] = len(out["items"])

    return out
