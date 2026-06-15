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

from flask import (
    Flask, render_template, request, jsonify, send_file, abort
)
from werkzeug.utils import secure_filename

import master_core
import render_core

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB batch ceiling

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PDF = os.path.join(BASE_DIR, "blank_1750.pdf")


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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
