"""
app.py — Flask web app for the Master DD1750 Packing List tool.

Routes:
  GET  /              -> the single-page UI (templates/index.html)
  POST /upload        -> accept a batch of child 1750 PDFs, parse filenames,
                         sniff NSNs, aggregate, return rows JSON for the table
  POST /generate      -> take finalized rows + header JSON, render the master
                         DD1750 PDF, stream it back as a download
  POST /audit         -> take finalized rows + header JSON, run audit_master,
                         return the pass/fail report
  GET  /api/health    -> liveness probe for Railway

The heavy lifting lives in master_core (new logic) and render_core (v25's proven
renderer). This file is just plumbing: request parsing, temp-file handling, and
JSON/PDF responses.
"""

import csv
import io
import os
import re
import tempfile
import zipfile
from collections import Counter
from datetime import datetime
from uuid import uuid4

from flask import (
    Flask, render_template, request, jsonify, send_file, abort
)
from werkzeug.utils import secure_filename

import bom_ingest
import shr_ingest
import reconcile as reconcile_mod
import packing
import master_core
import render_core

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB batch ceiling

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PDF = os.path.join(BASE_DIR, "blank_1750.pdf")

# In-memory job store.  Assumes a single gunicorn worker — fine for this
# single-user tool.  Each key is a uuid4 hex; value is the full job dict.
JOBS: dict = {}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Upload + parse a batch of child 1750 PDFs
# ---------------------------------------------------------------------------

@app.route("/upload", methods=["POST"])
def upload():
    """
    Accept multipart 'files' (one or many PDFs). For each:
      - parse the filename into a ParsedMEI (shape-based classifier)
      - best-effort sniff the NSN from the PDF body
    Then aggregate into master rows and return them as JSON.

    Response: {"rows": [ {box_num, model, lin, nsn, serials[], qty, needs_review}, ... ],
               "file_count": N, "parsed": [ per-file parse for transparency ]}
    """
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files uploaded (expected form field 'files')."}), 400

    parsed_list = []
    per_file = []

    # Work inside one temp dir so sniff_nsn can read the bytes from disk.
    with tempfile.TemporaryDirectory() as tmpdir:
        for f in files:
            if not f or not f.filename:
                continue
            fname = f.filename
            # Skip anything that isn't a PDF (the test data has a stray directory
            # entry with no .pdf extension).
            if not fname.lower().endswith(".pdf"):
                continue

            mei = master_core.parse_filename(fname)

            # Best-effort NSN sniff from the saved bytes.
            try:
                safe = secure_filename(fname) or "upload.pdf"
                disk_path = os.path.join(tmpdir, safe)
                f.save(disk_path)
                nsn = master_core.sniff_nsn(disk_path)
            except Exception:
                nsn = ""
            # Stash the sniffed NSN on the object so aggregate_meis can use it.
            setattr(mei, "nsn_sniffed", nsn)

            parsed_list.append(mei)
            d = mei.to_dict()
            d["nsn"] = nsn
            per_file.append(d)

    if not parsed_list:
        return jsonify({"error": "No PDF files found in the upload."}), 400

    rows = master_core.aggregate_meis(parsed_list)
    return jsonify({
        "rows": [r.to_dict() for r in rows],
        "file_count": len(parsed_list),
        "parsed": per_file,
    })


# ---------------------------------------------------------------------------
# Upload SHR CSV (from shr-extractor) → return rows for the review table
# ---------------------------------------------------------------------------

@app.route("/upload-csv", methods=["POST"])
def upload_csv():
    """
    Accept a single CSV file exported by the shr-extractor tool.
    Expected columns: lin, mpo_description, nsn, nsn_description, oh_qty,
                      serial_number, unit, date
    Groups rows by (lin, nsn) so each unique item becomes one master row
    with all its serial numbers consolidated.

    Response: {"rows": [...], "record_count": N}
    """
    f = request.files.get("csv")
    if not f or not f.filename:
        return jsonify({"error": "No CSV file provided (expected form field 'csv')."}), 400
    if not f.filename.lower().endswith(".csv"):
        return jsonify({"error": "File must be a .csv export from the SHR extractor."}), 400

    try:
        text = f.read().decode("utf-8-sig")  # strip BOM if present
        reader = csv.DictReader(io.StringIO(text))
        raw_rows = list(reader)
    except Exception as e:
        return jsonify({"error": f"Could not parse CSV: {e}"}), 400

    if not raw_rows:
        return jsonify({"error": "CSV is empty."}), 400

    # Normalise column names (strip whitespace, lowercase)
    def col(row, *names):
        for n in names:
            for k, v in row.items():
                if k.strip().lower() == n:
                    return (v or "").strip()
        return ""

    # Group by (lin, nsn) → one MasterRow per unique item
    from collections import OrderedDict
    groups = OrderedDict()
    record_count = 0

    for raw in raw_rows:
        record_count += 1
        lin = col(raw, "lin").upper()
        nsn = col(raw, "nsn")
        model = col(raw, "nsn_description", "mpo_description")
        sn = col(raw, "serial_number")
        try:
            qty = int(col(raw, "oh_qty") or "1")
        except ValueError:
            qty = 1

        key = (lin, nsn)
        if key not in groups:
            groups[key] = {
                "model": model,
                "lin": lin,
                "nsn": nsn,
                "serials": [],
                "qty": qty,
                "needs_review": False,
            }
        if sn and sn not in groups[key]["serials"]:
            groups[key]["serials"].append(sn)

    # Assign box numbers and update qty to serial count where serials exist
    rows = []
    for i, (key, row) in enumerate(groups.items(), start=1):
        row["box_num"] = i
        if row["serials"]:
            row["qty"] = len(row["serials"])
        rows.append(row)

    return jsonify({"rows": rows, "record_count": record_count})


# ---------------------------------------------------------------------------
# Generate the master DD1750 PDF
# ---------------------------------------------------------------------------

@app.route("/generate", methods=["POST"])
def generate():
    """
    Body: {"rows": [...], "header": {...}}
    Renders the master DD1750 and streams it back as Master_DD1750.pdf.
    """
    data = request.get_json(silent=True) or {}
    rows = data.get("rows", [])
    header = data.get("header", {})

    if not rows:
        return jsonify({"error": "No rows provided to generate."}), 400

    # Re-sequence box numbers 1..N defensively (UI should already do this).
    for i, r in enumerate(rows, start=1):
        r["box_num"] = i

    items = master_core.rows_to_bom_items(rows)
    header_info = master_core.build_master_header(header, rows)

    # Render to a temp file, then stream it.
    out_fd, out_path = tempfile.mkstemp(suffix=".pdf")
    os.close(out_fd)
    try:
        render_core.generate_dd1750_from_items(
            items,
            TEMPLATE_PDF,
            out_path,
            header=header_info,
            draw_master_header_fn=render_core.draw_master_header,
        )
        with open(out_path, "rb") as fh:
            pdf_bytes = fh.read()
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name="Master_DD1750.pdf",
    )


# ---------------------------------------------------------------------------
# Audit the master structure
# ---------------------------------------------------------------------------

@app.route("/audit", methods=["POST"])
def audit():
    """
    Body: {"rows": [...], "header": {...}}
    Runs audit_master and returns {passed, issues[], box_count}.
    """
    data = request.get_json(silent=True) or {}
    rows = data.get("rows", [])
    header = data.get("header", {})
    result = master_core.audit_master(rows, header)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------

@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "master-1750-tool",
        "template_present": os.path.exists(TEMPLATE_PDF),
    })


# ---------------------------------------------------------------------------
# Helper: find the representative (first-item) box for a BOM in a box_map
# ---------------------------------------------------------------------------

def _representative_box(bom: dict, box_map: dict) -> int | None:
    """Return the box number of the BOM's first item, or None if no items."""
    bom_id = bom["bom_id"]
    for item in bom.get("items", []):
        key = packing.item_key(bom_id, item["line_no"])
        if key in box_map:
            return box_map[key]
    return None


# ---------------------------------------------------------------------------
# POST /ingest — upload BOMs (PDFs) + optional SHR PDF; create a job
# ---------------------------------------------------------------------------

@app.route("/ingest", methods=["POST"])
def ingest():
    """
    Multipart form fields:
      boms  — one or many PDF files (the child 1750 BOMs)
      shr   — optional single PDF (the hand-receipt / SHR)

    Creates a job, returns JSON with job_id and per-BOM metadata.
    """
    bom_files = request.files.getlist("boms")
    shr_file = request.files.get("shr")

    if not bom_files:
        return jsonify({"error": "No BOM files provided (field: 'boms')."}), 400

    boms = []       # list of ingest_bom result dicts
    shr_dict = None

    with tempfile.TemporaryDirectory() as tmpdir:
        # --- Ingest each BOM PDF ---
        for f in bom_files:
            if not f or not f.filename:
                continue
            fname = f.filename
            safe = secure_filename(fname) or "bom.pdf"
            disk_path = os.path.join(tmpdir, safe)
            f.save(disk_path)
            # Use the filename stem (no extension) as the nomenclature label.
            nomenclature = os.path.splitext(fname)[0]
            bom = bom_ingest.ingest_bom(disk_path, nomenclature=nomenclature)
            boms.append(bom)

        if not boms:
            return jsonify({"error": "No valid PDF BOM files found."}), 400

        # --- Ingest SHR if provided ---
        if shr_file and shr_file.filename:
            safe_shr = secure_filename(shr_file.filename) or "shr.pdf"
            shr_path = os.path.join(tmpdir, safe_shr)
            shr_file.save(shr_path)
            shr_dict = shr_ingest.ingest_shr(shr_path)

        # --- Reconcile (if SHR present) ---
        reconciliation = None
        if shr_dict:
            reconciliation = reconcile_mod.reconcile(boms, shr_dict)

        # --- Default box assignment (1 BOM per box) ---
        box_map = packing.default_box_map(boms)

    # --- Suggested header ---
    # UIC: pick the most common non-empty value from the BOMs.
    uic_counts = Counter(
        b.get("uic", "").strip() for b in boms if b.get("uic", "").strip()
    )
    suggested_uic = uic_counts.most_common(1)[0][0] if uic_counts else ""
    today_str = datetime.utcnow().strftime("%d %b %Y").upper()

    # --- Build response: per-BOM metadata ---
    rec_by_bom = (reconciliation or {}).get("by_bom", {})
    boms_out = []
    for bom in boms:
        bom_id = bom["bom_id"]
        rep_box = _representative_box(bom, box_map)
        rec_entry = rec_by_bom.get(bom_id, {})
        boms_out.append({
            "bom_id":         bom_id,
            "filename":       bom.get("filename", ""),
            "nomenclature":   bom.get("nomenclature", ""),
            "model":          bom.get("model", ""),
            "lin":            bom.get("lin", ""),
            "end_item_niin":  bom.get("end_item_niin", ""),
            "serial_number":  bom.get("serial_number", ""),
            "item_count":     bom.get("item_count", 0),
            "box_num":        rep_box,
            "zero_on_hand":   bom.get("zero_on_hand", False),
            "reconcile_status": rec_entry.get("status") if rec_entry else None,
            "items":          bom.get("items", []),  # full component list for UI drill-in
            "warnings":       bom.get("warnings", []),
            "errors":         bom.get("errors", []),
        })

    # Collect per-BOM warnings for easy UI display.
    warnings_by_bom = {
        b["bom_id"]: b.get("warnings", []) for b in boms if b.get("warnings")
    }

    # Create and store the job.
    job_id = uuid4().hex
    JOBS[job_id] = {
        "boms":           boms,
        "shr":            shr_dict,
        "reconciliation": reconciliation,
        "box_map":        box_map,
        "created_at":     datetime.utcnow().isoformat(),
    }

    return jsonify({
        "job_id":           job_id,
        "boms":             boms_out,
        "occupied_boxes":   packing.occupied_boxes(box_map),
        "suggested_header": {"uic": suggested_uic, "date": today_str},
        "reconcile_summary": (reconciliation or {}).get("summary"),
        "warnings_by_bom":  warnings_by_bom,
    })


# ---------------------------------------------------------------------------
# POST /assign — move BOMs or individual items to different boxes
# ---------------------------------------------------------------------------

@app.route("/assign", methods=["POST"])
def assign():
    """
    Body: {
        "job_id": "...",
        "moves": [
            {"bom_id": "...", "box_num": 3},        // move entire BOM
            {"item_key": "bom_id:line_no", "box_num": 5}  // move one item
        ]
    }
    Returns updated occupied_boxes and each BOM's representative box.
    """
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id")
    job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": f"Job '{job_id}' not found."}), 404

    boms = job["boms"]
    box_map = job["box_map"]

    for move in data.get("moves", []):
        # Exclude move: drop every key for this BOM so it leaves the packing
        # list entirely (used to remove a zero-on-hand box that isn't present).
        if move.get("exclude"):
            bom_id = move.get("bom_id")
            bom = next((b for b in boms if b["bom_id"] == bom_id), None)
            if bom is None:
                continue
            box_map = dict(box_map)
            for item in bom.get("items", []):
                box_map.pop(packing.item_key(bom_id, item["line_no"]), None)
            continue

        target_box = int(move["box_num"])

        if "item_key" in move:
            # Move a single item.
            box_map = packing.reassign(box_map, move["item_key"], target_box)

        elif "bom_id" in move:
            # Move ALL items in this BOM.
            bom_id = move["bom_id"]
            bom = next((b for b in boms if b["bom_id"] == bom_id), None)
            if bom is None:
                continue
            for item in bom.get("items", []):
                key = packing.item_key(bom_id, item["line_no"])
                box_map = packing.reassign(box_map, key, target_box)

    # Persist the updated map back into the job.
    job["box_map"] = box_map

    # Build box_by_bom: bom_id -> representative box.
    box_by_bom = {}
    for bom in boms:
        rep = _representative_box(bom, box_map)
        box_by_bom[bom["bom_id"]] = rep

    return jsonify({
        "occupied_boxes": packing.occupied_boxes(box_map),
        "box_by_bom":     box_by_bom,
    })


# ---------------------------------------------------------------------------
# POST /reconcile — return the stored reconciliation report
# ---------------------------------------------------------------------------

@app.route("/reconcile", methods=["POST"])
def reconcile_report():
    """
    Body: {"job_id": "..."}
    Returns the reconciliation dict (or an empty shell if no SHR was provided).
    """
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id")
    job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": f"Job '{job_id}' not found."}), 404

    rec = job.get("reconciliation")
    if rec is None:
        rec = {"by_bom": {}, "summary": {"total": 0, "clean": 0, "flagged": 0}}
    return jsonify(rec)


# ---------------------------------------------------------------------------
# POST /generate-individuals — render one DD1750 per box, ZIP and stream
# ---------------------------------------------------------------------------

@app.route("/generate-individuals", methods=["POST"])
def generate_individuals():
    """
    Body: {"job_id": "...", "header": {...}}
    Renders one DD1750 PDF per occupied box, zips them, streams as
    Individual_1750s.zip.
    """
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id")
    job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": f"Job '{job_id}' not found."}), 404

    boms = job["boms"]
    box_map = job["box_map"]
    header = data.get("header", {})
    condense = bool(data.get("condense", False))

    if not packing.occupied_boxes(box_map):
        return jsonify({"error": "No occupied boxes — assign items first."}), 400

    # Build the exact same condensed rows the master PDF uses so that
    # individual 1750 count and box numbers are always identical to the master.
    # Each condensed row → one individual 1750 PDF; items from all physical
    # boxes that share that row are combined and condensed into one list.
    raw_master_rows = packing.boxes_to_master_rows(boms, box_map)
    condensed_rows  = master_core.condense_master_rows(raw_master_rows)
    if not condensed_rows:
        return jsonify({"error": "No rows to render — assign items to boxes first."}), 400

    # Map condensed row (by model+lin key) → physical box numbers that feed it.
    def _mk(row):
        return (
            master_core.normalize_model(str(row.get("model", "") or "")),
            str(row.get("lin", "") or "").strip().upper(),
        )

    from collections import defaultdict
    seq_to_phys: dict = defaultdict(list)
    condensed_key_to_seq = {_mk(r): r["box_num"] for r in condensed_rows}
    for raw in raw_master_rows:
        seq = condensed_key_to_seq.get(_mk(raw))
        if seq is not None:
            seq_to_phys[seq].append(raw["box_num"])

    total_boxes = len(condensed_rows)
    zip_buffer = io.BytesIO()

    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for c_row in condensed_rows:
                seq_num   = c_row["box_num"]
                phys_boxes = seq_to_phys.get(seq_num, [])

                # Collect items from every physical box that belongs to this row.
                raw_items: list = []
                for pb in phys_boxes:
                    raw_items.extend(packing.items_for_box(boms, box_map, pb))

                if not raw_items:
                    continue

                # Condense when explicitly requested OR when multiple physical
                # boxes are merged into one condensed row.
                if condense or len(phys_boxes) > 1:
                    raw_items = packing.condense_items(raw_items)

                bom_items = []
                for it in raw_items:
                    nsn_str = it.get("nsn", "") or ""
                    source_serials = it.get("source_serials", [])
                    if source_serials:
                        sn_part = "SN: " + ", ".join(source_serials)
                        nsn_str = (nsn_str + "  " + sn_part).strip() if nsn_str else sn_part
                    bom_items.append(render_core.BomItem(
                        line_no=seq_num,
                        description=it.get("description", ""),
                        nsn=nsn_str,
                        qty=it.get("qty", 1),
                        unit_of_issue=it.get("unit_of_issue", "EA"),
                    ))

                # Determine distinct source BOMs for the END ITEM header field.
                seen_bom_ids: list = []
                for it in raw_items:
                    if it["bom_id"] not in seen_bom_ids:
                        seen_bom_ids.append(it["bom_id"])
                source_boms = [b for b in boms if b["bom_id"] in seen_bom_ids]

                if len(source_boms) == 1:
                    sb = source_boms[0]
                    end_item_str = render_core.format_end_item(
                        sb.get("nomenclature", ""),
                        sb.get("model", ""),
                        sb.get("serial_number", ""),
                    )
                else:
                    noms = [b.get("nomenclature") or b.get("model", "") for b in source_boms]
                    distinct_noms = list(dict.fromkeys(n for n in noms if n))
                    serials_part = ", ".join(
                        b.get("serial_number", "") for b in source_boms
                        if b.get("serial_number", "")
                    )
                    end_item_str = (
                        f"{distinct_noms[0] if distinct_noms else 'BOX'} "
                        f"({len(source_boms)}x)\nSN: {serials_part}" if serials_part
                        else "; ".join(distinct_noms)
                    )

                hdr = master_core.build_master_header(header, [])
                hdr.end_item = end_item_str
                hdr.num_boxes = str(total_boxes)

                out_fd, out_path = tempfile.mkstemp(suffix=".pdf", dir=tmpdir)
                os.close(out_fd)
                render_core.generate_dd1750_from_items(
                    bom_items,
                    TEMPLATE_PDF,
                    out_path,
                    header=hdr,
                    draw_master_header_fn=render_core.draw_master_header,
                )

                first_nom = (source_boms[0].get("nomenclature")
                             or source_boms[0].get("model", "box")) if source_boms else "box"
                safe_nom = re.sub(r'[^\w\-]', '_', first_nom)[:40]
                zip_name = f"Box_{seq_num:03d}_{safe_nom}.pdf"

                with open(out_path, "rb") as fh:
                    zf.writestr(zip_name, fh.read())

    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name="Individual_1750s.zip",
    )


# ---------------------------------------------------------------------------
# POST /generate-master — render the master DD1750 PDF and stream it
# ---------------------------------------------------------------------------

@app.route("/generate-master", methods=["POST"])
def generate_master():
    """
    Body: {"job_id": "...", "header": {...}}
    Renders the master DD1750 (one row per occupied box) and streams it as
    Master_DD1750.pdf.
    """
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id")
    job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": f"Job '{job_id}' not found."}), 404

    boms = job["boms"]
    box_map = job["box_map"]
    header = data.get("header", {})

    rows = packing.boxes_to_master_rows(boms, box_map)
    if not rows:
        return jsonify({"error": "No rows to render — assign items to boxes first."}), 400

    # Collapse same-model end items into one row and re-sequence box numbers 1..N.
    rows = master_core.condense_master_rows(rows)

    items = master_core.rows_to_bom_items(rows)
    hdr = master_core.build_master_header(header, rows)

    out_fd, out_path = tempfile.mkstemp(suffix=".pdf")
    os.close(out_fd)
    try:
        render_core.generate_dd1750_from_items(
            items,
            TEMPLATE_PDF,
            out_path,
            header=hdr,
            draw_master_header_fn=render_core.draw_master_header,
        )
        with open(out_path, "rb") as fh:
            pdf_bytes = fh.read()
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name="Master_DD1750.pdf",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
