"""
test_bom_ingest.py — Scratch test for bom_ingest.ingest_bom()
Run from the spine repo root via the venv python:
    /path/to/venv/bin/python test_bom_ingest.py
"""
import sys, os, json

# Make sure we can import our modules from the spine directory
sys.path.insert(0, os.path.dirname(__file__))

from bom_ingest import ingest_bom

TEST_BOMS = [
    (
        "/Users/jaidenrabatin/Desktop/AEGIS/30-PROJECTS/active/1750_bulk_editor/FC BOMS/"
        "EPP TRUCK 10T2K1J23F1024820.pdf",
        "EPP TRUCK 10T",
    ),
    (
        "/Users/jaidenrabatin/Desktop/AEGIS/30-PROJECTS/active/1750_bulk_editor/FC BOMS/"
        "0137 A05023 INTERROGATOR SET.pdf",
        "INTERROGATOR SET",
    ),
    (
        "/Users/jaidenrabatin/Desktop/AEGIS/30-PROJECTS/active/1750_bulk_editor/arms room BOMs/"
        "103133 M39263  LMG 5.56MM M249.pdf",
        "LMG M249",
    ),
]

for pdf_path, nom in TEST_BOMS:
    print(f"\n{'='*70}")
    print(f"FILE   : {os.path.basename(pdf_path)}")
    result = ingest_bom(pdf_path, nomenclature=nom)
    print(f"LIN    : {result['lin'] or '(none)'}")
    print(f"SERIAL : {result['serial_number'] or '(none)'}")
    print(f"MODEL  : {result['model'] or '(none)'}")
    print(f"NIIN   : {result['end_item_niin'] or '(none)'}")
    print(f"UIC    : {result['uic'] or '(none)'}")
    print(f"ITEMS  : {result['item_count']}")

    if result["items"]:
        print("  First 2 items:")
        for item in result["items"][:2]:
            print(f"    [{item['line_no']}] {item['description']}  NSN={item['nsn']}  qty={item['qty']}  ui={item['unit_of_issue']}")
    else:
        print("  (no items extracted)")

    if result["warnings"]:
        for w in result["warnings"]:
            print(f"  WARN: {w}")
    if result["errors"]:
        for e in result["errors"]:
            print(f"  ERR : {e}")

print(f"\n{'='*70}")
print("Done.")
