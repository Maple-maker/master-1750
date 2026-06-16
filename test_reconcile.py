"""
test_reconcile.py — Tests for reconcile.reconcile()

Two test suites:

  (a) Synthetic — 4 BOMs against a hand-crafted SHR covering:
        1. Perfect match (all fields align)
        2. Serial mismatch (LIN/NIIN match but wrong serial)
        3. Description mismatch (LIN/serial match but model doesn't match)
        4. not_in_shr (LIN not present in SHR at all)

  (b) Real integration — BOM built from real SHR values for LIN C05002,
      reconciled against the actual Commo SHR PDF. Expects status 'match'.

Run with:
    ./venv/bin/python test_reconcile.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from reconcile import reconcile
from shr_ingest import ingest_shr

# ---------------------------------------------------------------------------
# (a) Synthetic test suite
# ---------------------------------------------------------------------------

# A minimal SHR dict with two aggregated groups
SYNTHETIC_SHR = {
    "aggregated": [
        {
            # Group 1: TRANSFER UNIT,CRYPT — will be used for perfect-match and serial-mismatch BOMs
            "lin":             "C05002",
            "nsn":             "5810015173587",   # NIIN = 015173587
            "nsn_description": "TRANSFER UNIT,CRYPT",
            "oh_qty":          5,
            "serials":         ["142357", "151383", "155011"],
            "unit":            "AS8M COMMO",
            "date":            "2026-04-25",
        },
        {
            # Group 2: RADIO SET — used for description-mismatch BOM
            "lin":             "R45463",
            "nsn":             "5820015353667",   # NIIN = 015353667
            "nsn_description": "RECEIVER-TRANSMITTER,RADIO:RT-1523F(C)/U",
            "oh_qty":          2,
            "serials":         ["203044", "203267"],
            "unit":            "AS8M COMMO",
            "date":            "2026-04-25",
        },
    ],
    "records":      [],
    "record_count": 0,
    "errors":       [],
}

# BOM 1: Perfect match — every field lines up
BOM_PERFECT = {
    "bom_id":        "bom-001",
    "filename":      "test_perfect.pdf",
    "nomenclature":  "Test Perfect",
    "lin":           "C05002",
    "end_item_niin": "015173587",   # matches last 9 of 5810015173587
    "serial_number": "142357",      # in SHR serials list
    "model":         "TRANSFER UNIT CRYPT",  # token-overlap match
    "uic":           "",
    "item_count":    1,
    "items":         [],
    "warnings":      [],
    "errors":        [],
}

# BOM 2: Serial mismatch — LIN and NIIN correct, serial not on SHR
BOM_SERIAL_MISMATCH = {
    "bom_id":        "bom-002",
    "filename":      "test_serial.pdf",
    "nomenclature":  "Test Serial Mismatch",
    "lin":           "C05002",
    "end_item_niin": "015173587",
    "serial_number": "999999",   # NOT in SHR for this LIN
    "model":         "TRANSFER UNIT,CRYPT",
    "uic":           "",
    "item_count":    1,
    "items":         [],
    "warnings":      [],
    "errors":        [],
}

# BOM 3: Description mismatch — LIN, NIIN, serial all correct but model is wrong
BOM_DESC_MISMATCH = {
    "bom_id":        "bom-003",
    "filename":      "test_desc.pdf",
    "nomenclature":  "Test Desc Mismatch",
    "lin":           "R45463",
    "end_item_niin": "015353667",
    "serial_number": "203044",
    "model":         "COMPLETELY WRONG DESCRIPTION XYZ",   # no token overlap
    "uic":           "",
    "item_count":    1,
    "items":         [],
    "warnings":      [],
    "errors":        [],
}

# BOM 4: not_in_shr — LIN does not exist in the synthetic SHR
BOM_NOT_IN_SHR = {
    "bom_id":        "bom-004",
    "filename":      "test_missing.pdf",
    "nomenclature":  "Test Not In SHR",
    "lin":           "ZZZZZ9",        # fictional LIN
    "end_item_niin": "000000000",     # fictional NIIN
    "serial_number": "000000",        # not in any group
    "model":         "FICTIONAL ITEM",
    "uic":           "",
    "item_count":    1,
    "items":         [],
    "warnings":      [],
    "errors":        [],
}

SYNTHETIC_BOMS = [BOM_PERFECT, BOM_SERIAL_MISMATCH, BOM_DESC_MISMATCH, BOM_NOT_IN_SHR]


def run_synthetic():
    print("=" * 60)
    print("(a) SYNTHETIC TESTS")
    print("=" * 60)

    result = reconcile(SYNTHETIC_BOMS, SYNTHETIC_SHR)
    by_bom   = result["by_bom"]
    summary  = result["summary"]

    failures = []

    # -- BOM 1: expect 'match' --
    r1 = by_bom["bom-001"]
    print(f"\nBOM-001 (perfect):  status={r1['status']!r}  matched_lin={r1['matched_lin']!r}")
    for field, detail in r1["fields"].items():
        print(f"  {field:<12}: {detail}")
    for msg in r1["messages"]:
        print(f"  MSG: {msg}")
    assert r1["status"] == "match", f"Expected 'match', got {r1['status']!r}"

    # -- BOM 2: expect 'mismatch' (serial wrong) --
    r2 = by_bom["bom-002"]
    print(f"\nBOM-002 (serial ×): status={r2['status']!r}  matched_lin={r2['matched_lin']!r}")
    for field, detail in r2["fields"].items():
        print(f"  {field:<12}: {detail}")
    for msg in r2["messages"]:
        print(f"  MSG: {msg}")
    assert r2["status"] == "mismatch", f"Expected 'mismatch', got {r2['status']!r}"
    assert any("Serial" in m or "serial" in m.lower() for m in r2["messages"]), \
        "Expected a serial-related advisory message"

    # -- BOM 3: expect 'mismatch' (description wrong) --
    r3 = by_bom["bom-003"]
    print(f"\nBOM-003 (desc ×):  status={r3['status']!r}  matched_lin={r3['matched_lin']!r}")
    for field, detail in r3["fields"].items():
        print(f"  {field:<12}: {detail}")
    for msg in r3["messages"]:
        print(f"  MSG: {msg}")
    assert r3["status"] == "mismatch", f"Expected 'mismatch', got {r3['status']!r}"
    assert any("Description" in m or "description" in m.lower() for m in r3["messages"]), \
        "Expected a description advisory message"

    # -- BOM 4: expect 'not_in_shr' --
    r4 = by_bom["bom-004"]
    print(f"\nBOM-004 (missing): status={r4['status']!r}  matched_lin={r4['matched_lin']!r}")
    for msg in r4["messages"]:
        print(f"  MSG: {msg}")
    assert r4["status"] == "not_in_shr", f"Expected 'not_in_shr', got {r4['status']!r}"
    assert r4["messages"], "Expected at least one advisory message for not_in_shr"

    # -- Summary --
    print(f"\nSummary: total={summary['total']}  clean={summary['clean']}  flagged={summary['flagged']}")
    assert summary["total"]   == 4,  f"Expected total=4, got {summary['total']}"
    assert summary["clean"]   == 1,  f"Expected clean=1, got {summary['clean']}"
    assert summary["flagged"] == 3,  f"Expected flagged=3, got {summary['flagged']}"

    print("\n[PASS] All synthetic assertions passed.")


# ---------------------------------------------------------------------------
# (b) Real integration test
# ---------------------------------------------------------------------------

REAL_SHR_PATH = (
    "/Users/jaidenrabatin/Desktop/AEGIS/30-PROJECTS/active/"
    "1750_bulk_editor/Commo SHR ++ (1).pdf"
)

# Mirrors real SHR data for C05002 / NSN 5810015173587 / serial 142357
BOM_REAL = {
    "bom_id":        "bom-real-c05002",
    "filename":      "real_c05002.pdf",
    "nomenclature":  "TRANSFER UNIT CRYPT",
    "lin":           "C05002",
    "end_item_niin": "015173587",   # last 9 digits of 5810015173587
    "serial_number": "142357",      # confirmed in SHR
    "model":         "TRANSFER UNIT,CRYPT",
    "uic":           "",
    "item_count":    0,
    "items":         [],
    "warnings":      [],
    "errors":        [],
}


def run_real():
    print("\n" + "=" * 60)
    print("(b) REAL INTEGRATION TEST")
    print(f"    SHR: {REAL_SHR_PATH}")
    print("=" * 60)

    shr = ingest_shr(REAL_SHR_PATH)
    print(f"SHR loaded: {len(shr['aggregated'])} aggregated groups, errors={shr['errors']}")

    result = reconcile([BOM_REAL], shr)
    r = result["by_bom"]["bom-real-c05002"]

    print(f"\nBOM-real-c05002:")
    print(f"  status      : {r['status']!r}")
    print(f"  matched_lin : {r['matched_lin']!r}")
    for field, detail in r["fields"].items():
        print(f"  {field:<12}: {detail}")
    for msg in r["messages"]:
        print(f"  MSG: {msg}")

    assert r["status"] == "match", (
        f"Expected status 'match' for real C05002 BOM, got {r['status']!r}\n"
        f"Messages: {r['messages']}"
    )

    print("\n[PASS] Real integration assertion passed.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_synthetic()
    run_real()

    print("\n" + "=" * 60)
    print("All tests PASSED.")
    print("=" * 60)
