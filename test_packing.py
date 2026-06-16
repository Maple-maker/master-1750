"""
test_packing.py — Unit tests for packing.py

Tests cover three scenarios using 3 synthetic BOMs (2-3 items each):

  1. DEFAULT (1:1)   — default_box_map puts each BOM in its own box.
  2. SHARE           — all of BOM2's items moved into box 1.
                       Box 1 now holds items from BOM0 + BOM2.
  3. SPLIT           — from default, one item of BOM0 moved into box 9.
                       Box 1 still holds the rest of BOM0; box 9 holds one item.

Integration smoke test: feed boxes_to_master_rows output into
master_core.rows_to_bom_items to confirm shape compatibility.

Run with: ./venv/bin/python test_packing.py
"""

import sys
import traceback

from packing import (
    item_key,
    default_box_map,
    reassign,
    occupied_boxes,
    items_for_box,
    boxes_to_master_rows,
)

# ---------------------------------------------------------------------------
# Synthetic BOM fixtures
# ---------------------------------------------------------------------------

BOM0 = {
    "bom_id":        "BOM0",
    "filename":      "bom0.pdf",
    "nomenclature":  "RIFLE M4",
    "model":         "RIFLE 5.56MM M4",
    "serial_number": "SN-111111",
    "lin":           "A12345",
    "end_item_niin": "010101010",
    "uic":           "W4ABCD",
    "item_count":    3,
    "items": [
        {"line_no": 1, "description": "UPPER RECEIVER",   "nsn": "1005-01-111-1111", "qty": 1, "unit_of_issue": "EA"},
        {"line_no": 2, "description": "LOWER RECEIVER",   "nsn": "1005-01-111-2222", "qty": 1, "unit_of_issue": "EA"},
        {"line_no": 3, "description": "BARREL ASSEMBLY",  "nsn": "1005-01-111-3333", "qty": 1, "unit_of_issue": "EA"},
    ],
    "warnings": [],
    "errors":   [],
}

BOM1 = {
    "bom_id":        "BOM1",
    "filename":      "bom1.pdf",
    "nomenclature":  "PISTOL M17",
    "model":         "PISTOL 9MM M17",
    "serial_number": "SN-222222",
    "lin":           "B67890",
    "end_item_niin": "020202020",
    "uic":           "W4ABCD",
    "item_count":    2,
    "items": [
        {"line_no": 1, "description": "SLIDE ASSEMBLY",   "nsn": "1005-01-222-1111", "qty": 1, "unit_of_issue": "EA"},
        {"line_no": 2, "description": "FRAME ASSEMBLY",   "nsn": "1005-01-222-2222", "qty": 1, "unit_of_issue": "EA"},
    ],
    "warnings": [],
    "errors":   [],
}

BOM2 = {
    "bom_id":        "BOM2",
    "filename":      "bom2.pdf",
    "nomenclature":  "MACHINE GUN M249",
    "model":         "LMG 5.56MM M249",
    "serial_number": "SN-333333",
    "lin":           "C11111",
    "end_item_niin": "030303030",
    "uic":           "W4ABCD",
    "item_count":    2,
    "items": [
        {"line_no": 1, "description": "BOLT CARRIER GROUP", "nsn": "1005-01-333-1111", "qty": 1, "unit_of_issue": "EA"},
        {"line_no": 2, "description": "FEED TRAY COVER",    "nsn": "1005-01-333-2222", "qty": 1, "unit_of_issue": "EA"},
    ],
    "warnings": [],
    "errors":   [],
}

BOMS = [BOM0, BOM1, BOM2]

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_failures: list[str] = []
_passes: int = 0

def check(description: str, condition: bool):
    global _passes
    if condition:
        _passes += 1
        print(f"  PASS  {description}")
    else:
        _failures.append(description)
        print(f"  FAIL  {description}")


# ---------------------------------------------------------------------------
# TEST 1: item_key
# ---------------------------------------------------------------------------
print("\n=== TEST: item_key ===")
check("item_key formats as bom_id:line_no",
      item_key("BOM0", 3) == "BOM0:3")
check("item_key with string bom_id and line_no=1",
      item_key("abc", 1) == "abc:1")


# ---------------------------------------------------------------------------
# TEST 2: default_box_map — 1:1 case
# ---------------------------------------------------------------------------
print("\n=== TEST: default_box_map (1:1) ===")
base_map = default_box_map(BOMS)

# BOM0 (index 0) → box 1
check("BOM0 item 1 → box 1", base_map[item_key("BOM0", 1)] == 1)
check("BOM0 item 2 → box 1", base_map[item_key("BOM0", 2)] == 1)
check("BOM0 item 3 → box 1", base_map[item_key("BOM0", 3)] == 1)

# BOM1 (index 1) → box 2
check("BOM1 item 1 → box 2", base_map[item_key("BOM1", 1)] == 2)
check("BOM1 item 2 → box 2", base_map[item_key("BOM1", 2)] == 2)

# BOM2 (index 2) → box 3
check("BOM2 item 1 → box 3", base_map[item_key("BOM2", 1)] == 3)
check("BOM2 item 2 → box 3", base_map[item_key("BOM2", 2)] == 3)

# All 7 items accounted for
check("All 7 items in base_map", len(base_map) == 7)


# ---------------------------------------------------------------------------
# TEST 3: occupied_boxes on default map
# ---------------------------------------------------------------------------
print("\n=== TEST: occupied_boxes (default) ===")
boxes = occupied_boxes(base_map)
check("Default has 3 occupied boxes", boxes == [1, 2, 3])


# ---------------------------------------------------------------------------
# TEST 4: reassign is pure (no mutation)
# ---------------------------------------------------------------------------
print("\n=== TEST: reassign is pure ===")
map_before = dict(base_map)
new_map = reassign(base_map, item_key("BOM0", 1), 99)
check("Original map NOT mutated", base_map == map_before)
check("New map has the reassigned key",   new_map[item_key("BOM0", 1)] == 99)
check("New map old key intact for item 2", new_map[item_key("BOM0", 2)] == 1)


# ---------------------------------------------------------------------------
# TEST 5: SHARE scenario — all of BOM2 → box 1
# ---------------------------------------------------------------------------
print("\n=== TEST: SHARE (BOM2 items moved to box 1) ===")

share_map = base_map
for item in BOM2["items"]:
    share_map = reassign(share_map, item_key("BOM2", item["line_no"]), 1)

# occupied_boxes should be [1, 2] — box 3 is now empty
share_boxes = occupied_boxes(share_map)
check("SHARE: occupied_boxes == [1, 2]", share_boxes == [1, 2])

# items_for_box(1) must contain items from BOM0 AND BOM2
box1_items = items_for_box(BOMS, share_map, 1)
box1_bom_ids = {it["bom_id"] for it in box1_items}
check("SHARE: box 1 contains BOM0 items", "BOM0" in box1_bom_ids)
check("SHARE: box 1 contains BOM2 items", "BOM2" in box1_bom_ids)
check("SHARE: box 1 item count == 5 (3 + 2)", len(box1_items) == 5)

# Master rows for SHARE
share_rows = boxes_to_master_rows(BOMS, share_map)
check("SHARE: 2 master rows", len(share_rows) == 2)

# The rows must be sorted by box_num
row_box_nums = [r["box_num"] for r in share_rows]
check("SHARE: rows sorted by box_num", row_box_nums == sorted(row_box_nums))

box1_row = next(r for r in share_rows if r["box_num"] == 1)
check("SHARE: box 1 qty == 2 (two end items)", box1_row["qty"] == 2)

# Both models appear in the shared box's model string
check("SHARE: box 1 model contains BOM0's model",
      "RIFLE 5.56MM M4" in box1_row["model"])
check("SHARE: box 1 model contains BOM2's model",
      "LMG 5.56MM M249" in box1_row["model"])

# Both serials present
check("SHARE: box 1 serials contains BOM0 serial",
      "SN-111111" in box1_row["serials"])
check("SHARE: box 1 serials contains BOM2 serial",
      "SN-333333" in box1_row["serials"])

# box 2 should still be BOM1 only
box2_row = next(r for r in share_rows if r["box_num"] == 2)
check("SHARE: box 2 qty == 1", box2_row["qty"] == 1)
check("SHARE: box 2 model is BOM1's model",
      box2_row["model"] == "PISTOL 9MM M17")


# ---------------------------------------------------------------------------
# TEST 6: SPLIT scenario — one BOM0 item → box 9
# ---------------------------------------------------------------------------
print("\n=== TEST: SPLIT (one BOM0 item moved to box 9) ===")

# Start fresh from default
split_map = reassign(base_map, item_key("BOM0", 3), 9)

split_boxes = occupied_boxes(split_map)
check("SPLIT: 9 is in occupied_boxes", 9 in split_boxes)
check("SPLIT: occupied_boxes includes 1, 2, 3, 9",
      set(split_boxes) == {1, 2, 3, 9})

# Box 1 should still have BOM0's remaining 2 items
split_box1 = items_for_box(BOMS, split_map, 1)
split_box1_bom_ids = {it["bom_id"] for it in split_box1}
check("SPLIT: box 1 still has BOM0 items", "BOM0" in split_box1_bom_ids)
check("SPLIT: box 1 has 2 BOM0 items (not 3)", len(split_box1) == 2)

# Box 9 should have the one moved item from BOM0
split_box9 = items_for_box(BOMS, split_map, 9)
check("SPLIT: box 9 has 1 item", len(split_box9) == 1)
check("SPLIT: box 9 item is from BOM0", split_box9[0]["bom_id"] == "BOM0")
check("SPLIT: box 9 item is the third component",
      split_box9[0]["line_no"] == 3)
check("SPLIT: box 9 item description is BARREL ASSEMBLY",
      split_box9[0]["description"] == "BARREL ASSEMBLY")

# Master rows for SPLIT — should have rows for boxes 1, 2, 3, 9
split_rows = boxes_to_master_rows(BOMS, split_map)
split_row_nums = [r["box_num"] for r in split_rows]
check("SPLIT: 4 master rows", len(split_rows) == 4)
check("SPLIT: rows sorted by box_num", split_row_nums == sorted(split_row_nums))
check("SPLIT: box 9 row present", 9 in split_row_nums)

# Both box 1 and box 9 reference BOM0 as the end item
box1_split = next(r for r in split_rows if r["box_num"] == 1)
box9_split  = next(r for r in split_rows if r["box_num"] == 9)
check("SPLIT: box 1 model is BOM0's", "RIFLE 5.56MM M4" in box1_split["model"])
check("SPLIT: box 9 model is BOM0's", "RIFLE 5.56MM M4" in box9_split["model"])
check("SPLIT: box 9 qty == 1", box9_split["qty"] == 1)


# ---------------------------------------------------------------------------
# TEST 7: master-row shape validation (contract check)
# ---------------------------------------------------------------------------
print("\n=== TEST: master row shape contract ===")
default_rows = boxes_to_master_rows(BOMS, base_map)
check("Default: 3 master rows", len(default_rows) == 3)

for row in default_rows:
    check(f"box {row['box_num']} has 'box_num' int",
          isinstance(row["box_num"], int))
    check(f"box {row['box_num']} has 'model' str",
          isinstance(row["model"], str))
    check(f"box {row['box_num']} has 'lin' str",
          isinstance(row["lin"], str))
    check(f"box {row['box_num']} has 'nsn' str",
          isinstance(row["nsn"], str))
    check(f"box {row['box_num']} has 'serials' list",
          isinstance(row["serials"], list))
    check(f"box {row['box_num']} has 'qty' int",
          isinstance(row["qty"], int))

# Spot-check default 1:1 values
r1 = next(r for r in default_rows if r["box_num"] == 1)
check("1:1 box 1 model == BOM0 model",   r1["model"] == "RIFLE 5.56MM M4")
check("1:1 box 1 lin   == BOM0 lin",     r1["lin"]   == "A12345")
check("1:1 box 1 nsn   == BOM0 niin",    r1["nsn"]   == "010101010")
check("1:1 box 1 serials contains BOM0", "SN-111111" in r1["serials"])
check("1:1 box 1 qty   == 1",            r1["qty"]   == 1)


# ---------------------------------------------------------------------------
# TEST 8: Integration — feed rows into master_core.rows_to_bom_items
# ---------------------------------------------------------------------------
print("\n=== TEST: integration with master_core.rows_to_bom_items ===")
try:
    from master_core import rows_to_bom_items

    # Use the default (1:1) rows — simplest well-formed input
    bom_items = rows_to_bom_items(default_rows)

    check("rows_to_bom_items returns a list",
          isinstance(bom_items, list))
    check("rows_to_bom_items returns at least 3 items (one per box)",
          len(bom_items) >= 3)

    # BomItem objects must have line_no, description, nsn, qty, unit_of_issue
    first_item = bom_items[0]
    check("BomItem has line_no attr",        hasattr(first_item, "line_no"))
    check("BomItem has description attr",    hasattr(first_item, "description"))
    check("BomItem has nsn attr",            hasattr(first_item, "nsn"))
    check("BomItem has qty attr",            hasattr(first_item, "qty"))
    check("BomItem has unit_of_issue attr",  hasattr(first_item, "unit_of_issue"))

    # Also run the SHARE rows through to prove multi-BOM rows are accepted
    share_bom_items = rows_to_bom_items(share_rows)
    check("SHARE rows accepted by rows_to_bom_items",
          isinstance(share_bom_items, list) and len(share_bom_items) >= 2)

    print(f"\n  rows_to_bom_items produced {len(bom_items)} BomItem(s) from "
          f"{len(default_rows)} master rows.")
    for item in bom_items:
        print(f"    line {item.line_no}: {item.description!r}  "
              f"nsn={item.nsn!r}  qty={item.qty}")

except Exception:
    _failures.append("rows_to_bom_items integration test")
    print("  FAIL  rows_to_bom_items raised an exception:")
    traceback.print_exc()


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
total = _passes + len(_failures)
print(f"\n{'='*50}")
print(f"Results: {_passes}/{total} passed, {len(_failures)} failed")
if _failures:
    print("\nFailed checks:")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All tests passed.")
    sys.exit(0)
