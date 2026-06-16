"""
shr_ingest.py — Normalized ingest wrapper for Sub Hand Receipt PDFs

This is the single entry point the main app calls to get SHR data.
It delegates all parsing to extract_shr.py and packages the results
into a consistent dict so the rest of the app never has to know about
XFA vs flat-text differences.

Usage:
    from shr_ingest import ingest_shr
    result = ingest_shr("/path/to/SHR.pdf")
"""

from extract_shr import aggregate_records, parse_shr_pdf


def ingest_shr(pdf_path: str) -> dict:
    """
    Parse a Sub Hand Receipt PDF and return normalized data.

    Accepts any GCSS-Army SHR — both XFA dynamic forms and flat text PDFs.
    Never raises; on failure, returns empty lists and an error message.

    Returns a dict with:
        records       — one dict per serialized item (raw, from parse_shr_pdf)
        aggregated    — one dict per (LIN, NSN) group (from aggregate_records)
        record_count  — total number of per-serial records
        errors        — list of error strings (empty on success)
    """
    # Start with a safe empty result; we fill it in below
    result = {
        "records": [],
        "aggregated": [],
        "record_count": 0,
        "errors": [],
    }

    # --- Step 1: Extract per-serial records from the PDF ---
    try:
        records = parse_shr_pdf(pdf_path)
    except Exception as exc:
        # Return the empty result with the error rather than crashing the app
        result["errors"].append(f"parse_shr_pdf failed: {exc}")
        return result

    # --- Step 2: Collapse per-serial records into per-(LIN, NSN) groups ---
    try:
        aggregated = aggregate_records(records)
    except Exception as exc:
        # Records parsed fine but aggregation failed — still return raw records
        result["errors"].append(f"aggregate_records failed: {exc}")
        result["records"] = records
        result["record_count"] = len(records)
        return result

    # --- Step 3: Pack everything into the normalized result dict ---
    result["records"] = records
    result["aggregated"] = aggregated
    result["record_count"] = len(records)

    return result
