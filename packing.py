"""
packing.py — Flexible box-assignment model and master-row builder.

Responsibilities:
  1. item_key()            — stable string key for one BOM item.
  2. default_box_map()     — 1-BOM-per-box starting point.
  3. reassign()            — pure (non-mutating) key → box reassignment.
  4. occupied_boxes()      — which box numbers are actually in use.
  5. items_for_box()       — every item (across all BOMs) assigned to one box.
  6. boxes_to_master_rows()— one row per occupied box for the master 1750.

Design:
  - Box numbers are PHYSICAL LABELS.  They are preserved exactly as the user
    assigns them.  This module never re-sequences them.
  - All state lives in the caller-supplied box_map dict; this module is
    stateless and treats every dict as immutable (reassign() copies).
  - stdlib only — no third-party imports.

Bom dict shape (produced by bom_ingest.ingest_bom, consumed here):
    {
      bom_id, filename, nomenclature, model, serial_number, lin,
      end_item_niin, uic, item_count,
      items: [{line_no, description, nsn, qty, unit_of_issue}],
      warnings, errors
    }
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def item_key(bom_id: str, line_no: int) -> str:
    """
    Stable string key that uniquely identifies one component item inside a BOM.

    Format: "<bom_id>:<line_no>"
    Used as the dict key in box_map so callers can move individual items
    between boxes without touching the rest of the BOM.

    Example:
        item_key("abc123", 5) -> "abc123:5"
    """
    return f"{bom_id}:{line_no}"


def default_box_map(boms: list[dict]) -> dict:
    """
    Build the starting (1-BOM-per-box) assignment map.

    Every item in boms[i] gets assigned to box number (i + 1).
    So BOM at index 0 → box 1, BOM at index 1 → box 2, etc.

    Returns:
        dict mapping item_key(bom_id, line_no) -> box_num (int) for every
        item in every BOM.  Returns an empty dict when boms is empty.

    Example:
        boms = [{"bom_id": "A", "items": [{"line_no": 1}, ...]}, ...]
        default_box_map(boms)  ->  {"A:1": 1, "A:2": 1, "B:1": 2, ...}
    """
    box_map: dict[str, int] = {}
    for box_num, bom in enumerate(boms, start=1):
        bom_id = bom["bom_id"]
        for item in bom.get("items", []):
            key = item_key(bom_id, item["line_no"])
            box_map[key] = box_num
    return box_map


def reassign(box_map: dict, key: str, box_num: int) -> dict:
    """
    Return a NEW box_map with one item moved to a different box.

    Pure function — the caller's box_map is never mutated.
    If 'key' does not exist in box_map it is simply added in the copy.

    Args:
        box_map : current assignment map (not modified)
        key     : item_key string to reassign
        box_num : destination box number (physical label)

    Returns:
        New dict identical to box_map except key -> box_num.

    Example:
        new_map = reassign(old_map, "A:3", 9)
        # old_map is unchanged; new_map has "A:3" pointing to box 9
    """
    new_map = dict(box_map)   # shallow copy — values are ints so this is safe
    new_map[key] = box_num
    return new_map


def occupied_boxes(box_map: dict) -> list[int]:
    """
    Return a sorted list of box numbers that have at least one item assigned.

    Gaps are preserved (e.g. [1, 2, 9] if box 3-8 are empty or not used).

    Example:
        occupied_boxes({"A:1": 1, "A:2": 9, "B:1": 2}) -> [1, 2, 9]
    """
    return sorted(set(box_map.values()))


def items_for_box(boms: list[dict], box_map: dict, box_num: int) -> list[dict]:
    """
    Return every component item assigned to 'box_num', enriched with its
    source BOM's end-item metadata.

    Searches across ALL BOMs, so a shared box correctly collects items from
    multiple BOMs and a split item from one BOM appears in multiple boxes.

    Each returned item dict has:
        line_no        — original line number from the source BOM
        description    — component description
        nsn            — component NSN
        qty            — quantity
        unit_of_issue  — unit of issue (e.g. "EA")
        bom_id         — source BOM id
        nomenclature   — source BOM's human label (e.g. "B49")
        lin            — source BOM's LIN
        serial_number  — source BOM's end-item serial number

    The renderer is responsible for re-sequencing line numbers per box;
    this function preserves the ORIGINAL BOM line numbers.

    Args:
        boms    : list of Bom dicts (same list used to build box_map)
        box_map : current assignment map
        box_num : which box to query

    Returns:
        List of enriched item dicts, in the order they appear within each BOM
        (BOMs iterated in the order they appear in 'boms').
    """
    result: list[dict] = []

    for bom in boms:
        bom_id = bom["bom_id"]
        # Pull the end-item metadata we'll attach to each component.
        source_meta = {
            "bom_id":       bom_id,
            "nomenclature": bom.get("nomenclature", ""),
            "lin":          bom.get("lin", ""),
            "serial_number": bom.get("serial_number", ""),
        }

        for item in bom.get("items", []):
            key = item_key(bom_id, item["line_no"])
            if box_map.get(key) == box_num:
                # Build enriched copy — never mutate the original item.
                enriched = {
                    "line_no":       item["line_no"],
                    "description":   item.get("description", ""),
                    "nsn":           item.get("nsn", ""),
                    "qty":           item.get("qty", 1),
                    "unit_of_issue": item.get("unit_of_issue", "EA"),
                }
                enriched.update(source_meta)
                result.append(enriched)

    return result


def boxes_to_master_rows(boms: list[dict], box_map: dict) -> list[dict]:
    """
    Build one master-row dict per occupied box, sorted by box_num.

    A "master row" summarises the END ITEMS (whole BOMs) whose components
    are at least partially packed in that box.  It is the shape consumed by
    master_core.rows_to_bom_items().

    Each row dict:
        box_num : int   — physical box label (gaps preserved)
        model   : str   — "; ".join of distinct model-or-nomenclature strings
                          for every BOM that has ≥1 item in this box
        lin     : str   — LIN of the FIRST such BOM (lexically first in boms)
        nsn     : str   — end_item_niin of the first such BOM
        serials : list  — serial_number of each such BOM, de-duped,
                          in BOM order; empty strings omitted
        qty     : int   — number of distinct end items (BOMs) in this box

    The 1:1 simple case falls out naturally: with default_box_map each box
    holds exactly one BOM, so model/lin/nsn/serials/qty all map to that BOM.

    Args:
        boms    : list of Bom dicts
        box_map : current assignment map

    Returns:
        List of row dicts sorted by box_num ascending.
    """
    # Collect which BOMs contribute to each occupied box.
    # box_to_boms: box_num -> list of bom dicts, in boms-order, de-duped.
    # We use a set of bom_ids to track duplicates but preserve order.
    box_to_boms: dict[int, list[dict]] = {}
    box_to_bom_ids_seen: dict[int, set[str]] = {}

    for bom in boms:
        bom_id = bom["bom_id"]
        for item in bom.get("items", []):
            key = item_key(bom_id, item["line_no"])
            box_num = box_map.get(key)
            if box_num is None:
                continue  # item not assigned to any box (shouldn't happen in practice)
            if box_num not in box_to_boms:
                box_to_boms[box_num] = []
                box_to_bom_ids_seen[box_num] = set()
            if bom_id not in box_to_bom_ids_seen[box_num]:
                box_to_boms[box_num].append(bom)
                box_to_bom_ids_seen[box_num].add(bom_id)

    rows: list[dict] = []
    for box_num in sorted(box_to_boms.keys()):
        contributing_boms = box_to_boms[box_num]

        # Build the model string: prefer bom["model"], fall back to nomenclature.
        # De-dup while preserving order.
        seen_models: set[str] = set()
        model_parts: list[str] = []
        for bom in contributing_boms:
            label = (bom.get("model") or bom.get("nomenclature") or "").strip()
            if label and label not in seen_models:
                model_parts.append(label)
                seen_models.add(label)
        model_str = "; ".join(model_parts)

        # LIN and NSN come from the FIRST contributing BOM.
        first = contributing_boms[0]
        lin_str = (first.get("lin") or "").strip()
        nsn_str = (first.get("end_item_niin") or "").strip()

        # Serials: one per BOM, omit blanks, preserve order, de-dup.
        seen_serials: set[str] = set()
        serials: list[str] = []
        for bom in contributing_boms:
            sn = (bom.get("serial_number") or "").strip()
            if sn and sn not in seen_serials:
                serials.append(sn)
                seen_serials.add(sn)

        qty = len(contributing_boms)  # number of distinct end items

        rows.append({
            "box_num": box_num,
            "model":   model_str,
            "lin":     lin_str,
            "nsn":     nsn_str,
            "serials": serials,
            "qty":     qty,
        })

    return rows


def condense_items(items: list[dict]) -> list[dict]:
    """
    Merge component lines that are the SAME item into one line, summing qty
    and collecting the end-item serial numbers from each source BOM.

    "Same" = identical NSN + normalized description (case/space-insensitive).
    Used when the user opts to condense duplicates — e.g. three identical
    KIV-7M lines (qty 1 each) collapse into one line with qty 3 and
    source_serials: ["SN1", "SN2", "SN3"] so the 1750 can show which
    end items those components belong to.  Order of first appearance is
    preserved; all other fields come from the first hit.
    """
    merged: dict = {}
    order: list = []
    for it in items:
        key = (
            (it.get("nsn") or "").strip().upper(),
            " ".join((it.get("description") or "").upper().split()),
        )
        if key not in merged:
            merged[key] = dict(it)        # copy — never mutate the caller's dict
            merged[key]["source_serials"] = []
            order.append(key)
        else:
            merged[key]["qty"] = merged[key].get("qty", 0) + it.get("qty", 0)
        # Collect the end-item serial number from this component's source BOM.
        sn = (it.get("serial_number") or "").strip()
        if sn and sn not in merged[key]["source_serials"]:
            merged[key]["source_serials"].append(sn)
    return [merged[k] for k in order]


__all__ = [
    "item_key",
    "default_box_map",
    "reassign",
    "occupied_boxes",
    "items_for_box",
    "boxes_to_master_rows",
    "condense_items",
]
