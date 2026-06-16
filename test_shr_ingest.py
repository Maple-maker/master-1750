"""
test_shr_ingest.py — Smoke test for shr_ingest against the real Commo SHR PDF.

Run with the venv python:
    /path/to/venv/bin/python test_shr_ingest.py
"""

import json
from shr_ingest import ingest_shr

PDF = "/Users/jaidenrabatin/Desktop/AEGIS/30-PROJECTS/active/1750_bulk_editor/Commo SHR ++ (1).pdf"

print(f"Testing against: {PDF}\n")

result = ingest_shr(PDF)

# ---- Summary ----
print(f"record_count  : {result['record_count']}")
print(f"aggregated    : {len(result['aggregated'])} groups")
print(f"errors        : {result['errors']}")

# ---- First 5 per-serial records (all fields) ----
print("\n--- First 5 per-serial records ---")
for i, r in enumerate(result["records"][:5], 1):
    print(f"\nRecord {i}:")
    for k, v in r.items():
        print(f"  {k:<20}: {v}")

# ---- First 3 aggregated groups ----
print("\n--- First 3 aggregated groups ---")
for i, g in enumerate(result["aggregated"][:3], 1):
    print(f"\nGroup {i}:")
    for k, v in g.items():
        print(f"  {k:<20}: {v}")
