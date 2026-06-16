"""
reconcile.py — SHR ↔ BOM reconciliation engine (advisory only)

Given a list of BOM dicts (from bom_ingest.ingest_bom) and a SHR dict
(from shr_ingest.ingest_shr), this module classifies how well each BOM's
end-item identity matches what is on the hand receipt.

IMPORTANT: this module is PURELY advisory. It never raises, never blocks,
and never modifies the BOMs or SHR it receives. Its output is a report you
can show to a user so they can decide what to do.

Public API
----------
    reconcile(boms: list[dict], shr: dict) -> dict

Returned shape
--------------
{
    "by_bom": {
        bom_id: {
            "status":       "match" | "partial" | "mismatch" | "not_in_shr",
            "matched_lin":  str,   # SHR LIN used for comparison, or ""
            "fields": {
                "lin":         {"bom": str, "shr": str, "status": "match"|"mismatch"|"missing"},
                "niin":        {"bom": str, "shr": str, "status": ...},
                "serial":      {"bom": str, "shr": str, "status": ...},
                "description": {"bom": str, "shr": str, "status": ...},
            },
            "messages": [str],   # human-readable advisory lines
        },
        ...
    },
    "summary": {"total": int, "clean": int, "flagged": int}
}
"""

import re
from typing import Optional


# ---------------------------------------------------------------------------
# NIIN helpers
# ---------------------------------------------------------------------------

def _digits_only(s: str) -> str:
    """Strip every non-digit character from s."""
    return re.sub(r'\D', '', s or '')


def _shr_niin(shr_nsn: str) -> str:
    """
    Derive the 9-digit NIIN from a full 13-digit SHR NSN.

    SHR NSN format: 4-digit FSC + 9-digit NIIN (with optional dashes).
    Examples:
        "5810015173587"  ->  "015173587"
        "702101E002938"  ->  last 9 digits of digits-only string

    We strip all non-digits, then take the last 9 characters. If the
    resulting string has fewer than 9 digits, return it as-is (bad data).
    """
    digits = _digits_only(shr_nsn)
    if len(digits) >= 9:
        return digits[-9:]
    return digits   # malformed; caller handles it gracefully


# ---------------------------------------------------------------------------
# Description comparison
# ---------------------------------------------------------------------------

_PUNCT_RE = re.compile(r'[^A-Z0-9\s]')


def _normalize_desc(s: str) -> str:
    """
    Upper-case, strip punctuation, and collapse internal whitespace.
    "TRANSFER UNIT,CRYPT" -> "TRANSFER UNIT CRYPT"
    """
    up = (s or '').upper()
    no_punct = _PUNCT_RE.sub(' ', up)
    return ' '.join(no_punct.split())


def _desc_match(bom_desc: str, shr_desc: str) -> bool:
    """
    Return True if the two descriptions are considered equivalent.

    Rules (any one is sufficient):
      1. Exact match after normalization.
      2. One normalized string contains the other (substring).
      3. Token overlap: at least 60 % of the *shorter* string's tokens
         also appear in the longer string's token set.
    """
    a = _normalize_desc(bom_desc)
    b = _normalize_desc(shr_desc)

    if not a or not b:
        return False

    # Rule 1: exact
    if a == b:
        return True

    # Rule 2: substring containment
    if a in b or b in a:
        return True

    # Rule 3: token overlap (≥ 60 % of shorter side's tokens)
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    shorter = tokens_a if len(tokens_a) <= len(tokens_b) else tokens_b
    longer  = tokens_b if len(tokens_a) <= len(tokens_b) else tokens_a
    overlap = shorter & longer
    if shorter and len(overlap) / len(shorter) >= 0.60:
        return True

    return False


# ---------------------------------------------------------------------------
# SHR index builder
# ---------------------------------------------------------------------------

def _build_shr_index(shr: dict) -> tuple[dict, set, dict]:
    """
    Build three fast-lookup structures from shr['aggregated']:

    lin_index   : {UPPER_LIN -> [group, ...]}   — one LIN can have multiple NSN groups
    all_serials : {UPPER_SERIAL}                 — flat set of every serial across all groups
    niin_index  : {9-digit-NIIN -> [group, ...]} — groups keyed by derived NIIN

    Returns (lin_index, all_serials, niin_index).
    """
    lin_index:   dict[str, list] = {}
    all_serials: set[str]        = set()
    niin_index:  dict[str, list] = {}

    for group in shr.get('aggregated', []):
        # --- LIN index ---
        lin = (group.get('lin') or '').upper()
        if lin:
            lin_index.setdefault(lin, []).append(group)

        # --- Serial index ---
        for sn in group.get('serials', []):
            if sn:
                all_serials.add(sn.upper())

        # --- NIIN index ---
        niin = _shr_niin(group.get('nsn', ''))
        if niin:
            niin_index.setdefault(niin, []).append(group)

    return lin_index, all_serials, niin_index


# ---------------------------------------------------------------------------
# Single-BOM matcher
# ---------------------------------------------------------------------------

def _find_shr_group(
    bom: dict,
    lin_index: dict,
    all_serials: set,
    niin_index: dict,
) -> Optional[dict]:
    """
    Pick the best SHR aggregated group for this BOM.

    Priority:
      1. Exact LIN match (uppercased).
      2. NIIN match (BOM.end_item_niin vs derived SHR NIINs).
      3. Serial match (BOM.serial_number found in any SHR group's serials).
      4. None → caller marks status "not_in_shr".

    When multiple groups share a LIN (different NSNs), pick the one whose
    NIIN matches the BOM; fall back to the first group if still ambiguous.
    """
    bom_lin    = (bom.get('lin')            or '').upper()
    bom_niin   = _digits_only(bom.get('end_item_niin') or '')
    bom_serial = (bom.get('serial_number')  or '').upper()

    # --- Priority 1: LIN ---
    if bom_lin and bom_lin in lin_index:
        candidates = lin_index[bom_lin]
        if len(candidates) == 1:
            return candidates[0]
        # Multiple groups under this LIN: prefer the one whose NIIN matches
        if bom_niin:
            for g in candidates:
                if _shr_niin(g.get('nsn', '')) == bom_niin:
                    return g
        return candidates[0]   # best-effort: first group wins

    # --- Priority 2: NIIN ---
    if bom_niin and bom_niin in niin_index:
        return niin_index[bom_niin][0]

    # --- Priority 3: Serial ---
    if bom_serial and bom_serial in all_serials:
        # Find the group that owns this serial
        for groups in lin_index.values():
            for g in groups:
                if bom_serial in [s.upper() for s in g.get('serials', [])]:
                    return g

    return None  # no match


# ---------------------------------------------------------------------------
# Field-level comparison
# ---------------------------------------------------------------------------

def _compare_fields(bom: dict, group: dict) -> tuple[dict, list[str]]:
    """
    Compare BOM identity fields against the matched SHR group.

    Returns:
        fields  — {field_name: {"bom": str, "shr": str, "status": ...}}
        messages — human-readable advisory list
    """
    messages: list[str] = []

    # -- lin --
    bom_lin  = (bom.get('lin') or '').upper()
    shr_lin  = (group.get('lin') or '').upper()
    if not bom_lin:
        lin_status = 'missing'
    elif bom_lin == shr_lin:
        lin_status = 'match'
    else:
        lin_status = 'mismatch'
        messages.append(f"LIN mismatch: BOM '{bom_lin}' vs SHR '{shr_lin}'")

    # -- niin --
    bom_niin_raw = _digits_only(bom.get('end_item_niin') or '')
    shr_niin_raw = _shr_niin(group.get('nsn') or '')
    if not bom_niin_raw:
        niin_status = 'missing'
    elif bom_niin_raw == shr_niin_raw:
        niin_status = 'match'
    else:
        niin_status = 'mismatch'
        messages.append(
            f"NIIN mismatch: BOM '{bom_niin_raw}' vs SHR derived NIIN '{shr_niin_raw}' "
            f"(from full NSN '{group.get('nsn', '')}')"
        )

    # -- serial --
    bom_serial = (bom.get('serial_number') or '').upper()
    shr_serials_upper = [s.upper() for s in group.get('serials', []) if s]
    # Display value: the matched serial, or the joined list for advisory context
    shr_serial_display = ', '.join(group.get('serials', []) or [])
    if not bom_serial:
        serial_status = 'missing'
    elif bom_serial in shr_serials_upper:
        serial_status = 'match'
    else:
        serial_status = 'mismatch'
        messages.append(
            f"Serial '{bom_serial}' not found in SHR for LIN '{shr_lin}' "
            f"(SHR has: {shr_serial_display})"
        )

    # -- description --
    bom_model = (bom.get('model') or '').strip()
    shr_desc  = (group.get('nsn_description') or '').strip()
    if not bom_model:
        desc_status = 'missing'
    elif _desc_match(bom_model, shr_desc):
        desc_status = 'match'
    else:
        desc_status = 'mismatch'
        messages.append(
            f"Description mismatch: BOM '{bom_model}' vs SHR '{shr_desc}'"
        )

    fields = {
        'lin':         {'bom': bom_lin,       'shr': shr_lin,          'status': lin_status},
        'niin':        {'bom': bom_niin_raw,   'shr': shr_niin_raw,     'status': niin_status},
        'serial':      {'bom': bom_serial,     'shr': shr_serial_display, 'status': serial_status},
        'description': {'bom': bom_model,      'shr': shr_desc,         'status': desc_status},
    }
    return fields, messages


# ---------------------------------------------------------------------------
# Roll-up logic
# ---------------------------------------------------------------------------

def _rollup_status(fields: dict) -> str:
    """
    Determine the BOM-level status from individual field statuses.

      - All comparable fields are 'match'                → 'match'
      - At least one field is 'mismatch'                 → 'mismatch'
      - No mismatches but some fields are 'missing'      → 'partial'
      - (This case only reached when a group was found.) → never 'not_in_shr'
    """
    statuses = [f['status'] for f in fields.values()]
    if any(s == 'mismatch' for s in statuses):
        return 'mismatch'
    if all(s == 'match' for s in statuses):
        return 'match'
    return 'partial'   # some missing, none mismatch


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def reconcile(boms: list[dict], shr: dict) -> dict:
    """
    Reconcile a list of BOM dicts against a SHR dict.

    Args:
        boms : list of dicts from bom_ingest.ingest_bom()
        shr  : dict from shr_ingest.ingest_shr()

    Returns:
        {
            "by_bom": { bom_id: { status, matched_lin, fields, messages } },
            "summary": { total, clean, flagged }
        }

    Never raises. All errors become advisory messages on the affected BOM.
    """
    # Build fast-lookup indexes once for all BOMs
    lin_index, all_serials, niin_index = _build_shr_index(shr)

    by_bom: dict = {}

    for bom in boms:
        bom_id = bom.get('bom_id', 'unknown')

        try:
            group = _find_shr_group(bom, lin_index, all_serials, niin_index)
        except Exception as exc:
            # Safety net: should never happen, but advisory engine must not crash
            by_bom[bom_id] = {
                'status':       'not_in_shr',
                'matched_lin':  '',
                'fields': {
                    'lin':         {'bom': '', 'shr': '', 'status': 'missing'},
                    'niin':        {'bom': '', 'shr': '', 'status': 'missing'},
                    'serial':      {'bom': '', 'shr': '', 'status': 'missing'},
                    'description': {'bom': '', 'shr': '', 'status': 'missing'},
                },
                'messages': [f"Internal error during SHR lookup: {exc}"],
            }
            continue

        if group is None:
            # No SHR group found for this BOM by any matching strategy
            bom_lin = (bom.get('lin') or '').upper()
            by_bom[bom_id] = {
                'status':       'not_in_shr',
                'matched_lin':  '',
                'fields': {
                    'lin':         {'bom': bom_lin, 'shr': '', 'status': 'missing'},
                    'niin':        {'bom': _digits_only(bom.get('end_item_niin') or ''), 'shr': '', 'status': 'missing'},
                    'serial':      {'bom': (bom.get('serial_number') or '').upper(), 'shr': '', 'status': 'missing'},
                    'description': {'bom': (bom.get('model') or '').strip(), 'shr': '', 'status': 'missing'},
                },
                'messages': [
                    f"No SHR entry found for BOM "
                    f"(LIN='{(bom.get('lin') or '').upper()}', "
                    f"NIIN='{bom.get('end_item_niin', '')}', "
                    f"Serial='{bom.get('serial_number', '')}')"
                ],
            }
            continue

        # Group found — compare fields
        fields, messages = _compare_fields(bom, group)
        status = _rollup_status(fields)

        by_bom[bom_id] = {
            'status':      status,
            'matched_lin': (group.get('lin') or '').upper(),
            'fields':      fields,
            'messages':    messages,
        }

    # Build summary
    total  = len(by_bom)
    clean  = sum(1 for v in by_bom.values() if v['status'] == 'match')
    flagged = total - clean

    return {
        'by_bom':  by_bom,
        'summary': {'total': total, 'clean': clean, 'flagged': flagged},
    }
