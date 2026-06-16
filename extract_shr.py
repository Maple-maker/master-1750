"""
extract_shr.py — Sub Hand Receipt PDF extractor

Handles two SHR PDF variants from GCSS-Army:
  1. XFA dynamic forms — data lives as structured XML in the PDF binary
  2. Flat text PDFs — data is in the visible text stream (no XFA layer)

Both variants produce identical output records.

Usage:
    python3 extract_shr.py path/to/SHR.pdf
"""

import csv
import io
import json
import re
import sys
import xml.etree.ElementTree as ET
from typing import Any

import pdfplumber
from pypdf import PdfReader


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_NS_RE = re.compile(r'\{[^}]+\}')


def _tag(element) -> str:
    """Local tag name, namespace stripped."""
    return _NS_RE.sub('', element.tag)


def _text(element) -> str:
    return (element.text or '').strip()


# ---------------------------------------------------------------------------
# Path 1 + 2: XFA extraction (standard GCSS-Army XFA dynamic forms)
# ---------------------------------------------------------------------------

def _extract_datasets_xml(pdf_path: str) -> str:
    """
    Pull xfa:datasets XML from an SHR PDF.

    PATH 1 — standard: /Root/AcroForm/XFA array of alternating [name, stream] pairs.
    PATH 2 — brute-force: scan every stream object in the PDF for xfa:datasets.
              Only attempted when the PDF structure is ambiguous (e.g., /AcroForm
              exists but /XFA is missing due to a non-standard embedding). Skipped
              when /AcroForm clearly has no /XFA key — that's definitive proof there
              is no XFA layer, and the brute-force scan would just be noisy.
    Raises ValueError("no_xfa") if neither path finds anything.
    """
    reader = PdfReader(pdf_path)
    xfa_possibly_present = False  # set True only if we have ambiguous evidence

    # PATH 1: standard /AcroForm/XFA array
    try:
        root = reader.trailer['/Root'].get_object()
        acroform_ref = root.get('/AcroForm')
        if acroform_ref is not None:
            acroform = acroform_ref.get_object()
            xfa_ref = acroform.get('/XFA')
            if xfa_ref is not None:
                # /XFA key present — try standard extraction
                xfa_possibly_present = True
                items = list(xfa_ref.get_object())
                for i in range(0, len(items) - 1, 2):
                    try:
                        val = items[i + 1].get_object()
                        if not hasattr(val, 'get_data'):
                            continue
                        raw = val.get_data()
                        if b'xfa:datasets' in raw:
                            return raw.decode('utf-8', errors='replace')
                    except Exception:
                        continue
            # else: /AcroForm present but no /XFA — definitively not XFA, skip scan
        else:
            # No /AcroForm at all — unusual; try brute force just in case
            xfa_possibly_present = True
    except Exception:
        # Unexpected PDF structure — uncertain, try brute force
        xfa_possibly_present = True

    # PATH 2: brute-force only when XFA presence is ambiguous
    if xfa_possibly_present:
        result = _scan_all_streams_for_xfa(reader)
        if result:
            return result

    raise ValueError("no_xfa")  # sentinel — caller tries text path next


def _scan_all_streams_for_xfa(reader: PdfReader) -> str | None:
    """Walk every indirect object and return the first stream with xfa:datasets."""
    from pypdf.generic import IndirectObject

    obj_nums: set[int] = set()
    try:
        for table in reader.xref:
            if isinstance(table, dict):
                for k in table:
                    try:
                        obj_nums.add(int(k))
                    except (ValueError, TypeError):
                        pass
    except Exception:
        pass

    if not obj_nums:
        obj_nums = set(range(1, 500))

    for objnum in obj_nums:
        try:
            obj = IndirectObject(objnum, 0, reader).get_object()
            if not hasattr(obj, 'get_data'):
                continue
            raw = obj.get_data()
            if b'xfa:datasets' in raw:
                return raw.decode('utf-8', errors='replace')
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# XFA XML → records
# ---------------------------------------------------------------------------

def _parse_xfa_root(root) -> list[dict[str, Any]]:
    """Extract property records from a parsed xfa:datasets XML root element."""

    def find_tag(node, tag):
        for child in node:
            if _tag(child) == tag:
                return child
        return None

    def findall_tag(node, tag):
        return [child for child in node if _tag(child) == tag]

    xfa_data = find_tag(root, 'data')
    data = find_tag(xfa_data, 'data')

    unit = _text(find_tag(data, 'IM_UNIT_DESC')) if find_tag(data, 'IM_UNIT_DESC') is not None else ''
    date = _text(find_tag(data, 'DATE')) if find_tag(data, 'DATE') is not None else ''

    records = []
    im_receipt = find_tag(data, 'IM_RECEIPT')
    if im_receipt is None:
        return records

    for mpo_block in findall_tag(im_receipt, 'DATA'):
        mpo_descr_el = find_tag(mpo_block, 'MPO_DESCR')
        mpo_descr = _text(mpo_descr_el) if mpo_descr_el is not None else ''
        lin = re.sub(r'[^A-Z0-9]', '', mpo_descr.upper())[:6]

        rcpt_detail = find_tag(mpo_block, 'RCPT_DETAIL')
        if rcpt_detail is None:
            continue

        for item_block in findall_tag(rcpt_detail, 'DATA'):
            nsn_el     = find_tag(item_block, 'NSN')
            maktx_el   = find_tag(item_block, 'MAKTX')
            labst_el   = find_tag(item_block, 'LABST')
            id_num_el  = find_tag(item_block, 'ID_NUM')

            nsn             = _text(nsn_el)     if nsn_el     is not None else ''
            nsn_description = _text(maktx_el)   if maktx_el   is not None else ''
            oh_qty          = _text(labst_el)   if labst_el   is not None else '0'
            primary_sn      = _text(id_num_el)  if id_num_el  is not None else ''

            serials: set[str] = set()
            if primary_sn:
                serials.add(primary_sn)

            sernr_block = find_tag(item_block, 'SERNR')
            if sernr_block is not None:
                for sernr_data in findall_tag(sernr_block, 'DATA'):
                    for key in ('SERNR1', 'SERNR2', 'SERNR3'):
                        el = find_tag(sernr_data, key)
                        if el is not None and _text(el):
                            serials.add(_text(el))

            if not serials:
                serials.add('')

            for sn in sorted(serials):
                records.append({
                    'unit': unit,
                    'date': date,
                    'lin': lin,
                    'mpo_description': mpo_descr,
                    'nsn': nsn,
                    'nsn_description': nsn_description,
                    'oh_qty': oh_qty,
                    'serial_number': sn,
                })

    return records


# ---------------------------------------------------------------------------
# Path 3: text-based extraction (flat/printed SHR PDFs — no XFA layer)
# ---------------------------------------------------------------------------

# Compiled once; used in _parse_text_pdf
_DATE_RE    = re.compile(r'^Date:\s*(.+)', re.IGNORECASE)
_TO_RE      = re.compile(r'^To:\s*(.+)',   re.IGNORECASE)
_MPO_HDR    = re.compile(r'^MPO\s+MPO Description',         re.IGNORECASE)
_NSN_HDR    = re.compile(r'^NSN\s+NSN Description',         re.IGNORECASE)
_SYSNO_HDR  = re.compile(r'^SysNo\s+SerNo',                 re.IGNORECASE)
_PAGE_HDR   = re.compile(r'^(Sub Hand Receipt|Time:|Page \d|From:|FE:|UIC:)', re.IGNORECASE)
_FOOTER_RE  = re.compile(r'^(From \(Print Name\)|To \(Print Name\))',         re.IGNORECASE)

# MPO data line: 9 digits, then 6-char LIN, then description
_MPO_LINE   = re.compile(r'^\d{9}\s+([A-Z0-9]{6})\s+(.+)$')

# NSN data line ends: ... EA {ciic} {dla} EA {oh_qty}
# The lazy (.+?) safely handles descriptions that themselves contain "EA".
_NSN_LINE   = re.compile(r'^(\S+)\s+(.+?)\s+EA\s+\S+\s+\S+\s+EA\s+(\d+)\s*$')


def _parse_text_pdf(pdf_path: str) -> list[dict[str, Any]]:
    """
    Extract SHR records from a flat text PDF via pdfplumber.

    The text layout produced by GCSS-Army is consistent across pages:
      - Page headers (Sub Hand Receipt / Date / UIC / ...) — skipped
      - "MPO MPO Description" header + data line  →  sets current LIN/MPO desc
      - "NSN NSN Description ..." header + data line  →  captures NSN + OH Qty
      - "SysNo ..." header + following lines  →  captures serial numbers
    State persists across page boundaries so MPO blocks that span pages parse correctly.
    """
    with pdfplumber.open(pdf_path) as pdf:
        # Concatenate all pages; page breaks are invisible to the state machine
        all_lines: list[str] = []
        for page in pdf.pages:
            text = page.extract_text() or ''
            all_lines.extend(text.split('\n'))

    records: list[dict[str, Any]] = []
    unit = ''
    date = ''

    # Current item state
    cur_lin      = ''
    cur_mpo_desc = ''
    cur_nsn      = ''
    cur_nsn_desc = ''
    cur_oh_qty   = '0'
    cur_serials: list[str] = []

    state = 'scan'  # 'scan' | 'serials'

    def flush():
        """Emit one record per serial (or one blank-serial record) for the current NSN."""
        nonlocal cur_nsn, cur_serials
        if not cur_nsn:
            return
        sns = cur_serials if cur_serials else ['']
        for sn in sns:
            records.append({
                'unit':            unit,
                'date':            date,
                'lin':             cur_lin,
                'mpo_description': cur_mpo_desc,
                'nsn':             cur_nsn,
                'nsn_description': cur_nsn_desc,
                'oh_qty':          cur_oh_qty,
                'serial_number':   sn,
            })
        cur_nsn = ''
        cur_serials = []

    for raw in all_lines:
        line = raw.strip()
        if not line:
            continue

        # Signature block at end of document — stop
        if _FOOTER_RE.match(line):
            break

        # Extract metadata from page headers (only on first occurrence)
        m = _DATE_RE.match(line)
        if m:
            if not date:
                date = m.group(1).strip()
            continue
        m = _TO_RE.match(line)
        if m:
            if not unit:
                unit = m.group(1).strip()
            continue
        if _PAGE_HDR.match(line):
            continue

        # Section headers
        if _MPO_HDR.match(line):
            state = 'scan'
            continue

        if _NSN_HDR.match(line):
            flush()
            state = 'scan'
            continue

        if _SYSNO_HDR.match(line):
            state = 'serials'
            continue

        # MPO data line: 9-digit MPO number + 6-char LIN + description
        m = _MPO_LINE.match(line)
        if m:
            flush()
            cur_lin      = m.group(1)
            cur_mpo_desc = m.group(2).strip()
            state = 'scan'
            continue

        # NSN data line
        m = _NSN_LINE.match(line)
        if m:
            flush()
            cur_nsn      = m.group(1)
            cur_nsn_desc = m.group(2).strip()
            cur_oh_qty   = m.group(3)
            state = 'scan'
            continue

        # Serial number lines (space-separated; may span multiple rows)
        if state == 'serials':
            cur_serials.extend(t for t in line.split() if t)

    flush()
    return records


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_shr_pdf(pdf_path: str) -> list[dict[str, Any]]:
    """
    Extract equipment records from a Sub Hand Receipt PDF.

    Supports both GCSS-Army XFA dynamic forms and flat text SHR PDFs.
    Returns a list of flat dicts, one per serialized item:
        { unit, date, lin, mpo_description, nsn, nsn_description, oh_qty, serial_number }
    """
    # Try XFA extraction first (standard GCSS-Army SHRs)
    try:
        xml_str = _extract_datasets_xml(pdf_path)
        root = ET.fromstring(xml_str)
        return _parse_xfa_root(root)
    except ValueError:
        pass  # fall through to text path

    # Fall back to text extraction (flat/printed SHRs)
    records = _parse_text_pdf(pdf_path)
    if records:
        return records

    raise ValueError(
        "Could not extract any records from this PDF. "
        "It is neither a standard XFA SHR nor a parseable flat text SHR. "
        "Try re-exporting from GCSS-Army using File > Save As in Adobe Reader."
    )


def aggregate_records(records: list[dict]) -> list[dict]:
    """
    Collapse per-serial records into one row per (lin, nsn) group.

    Each group has: lin, nsn_description, nsn, oh_qty (total), serials (list),
    unit, date.  The raw one-per-serial records remain available via parse_shr_pdf.
    """
    groups: dict = {}
    order: list = []

    for r in records:
        key = (r['lin'], r['nsn'])
        if key not in groups:
            groups[key] = {
                'lin':             r['lin'],
                'nsn':             r['nsn'],
                'nsn_description': r['nsn_description'],
                'oh_qty':          0,
                'serials':         [],
                'unit':            r['unit'],
                'date':            r['date'],
            }
            order.append(key)
        g = groups[key]
        g['oh_qty'] += 1
        sn = r['serial_number']
        if sn and sn not in g['serials']:
            g['serials'].append(sn)

    return [groups[k] for k in order]


def to_csv(records: list[dict]) -> str:
    """Convert per-serial records list to CSV string."""
    if not records:
        return ''
    buf = io.StringIO()
    fieldnames = ['lin', 'mpo_description', 'nsn', 'nsn_description', 'oh_qty', 'serial_number', 'unit', 'date']
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(records)
    return buf.getvalue()


def to_csv_aggregated(records: list[dict]) -> str:
    """Convert aggregated records to CSV with serial numbers comma-joined."""
    agg = aggregate_records(records)
    if not agg:
        return ''
    buf = io.StringIO()
    fieldnames = ['lin', 'nsn', 'nsn_description', 'oh_qty', 'serials', 'unit', 'date']
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in agg:
        writer.writerow({**row, 'serials': ', '.join(row['serials'])})
    return buf.getvalue()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python3 extract_shr.py path/to/SHR.pdf")
        sys.exit(1)

    path = sys.argv[1]
    print(f"Extracting: {path}")
    records = parse_shr_pdf(path)
    print(f"Found {len(records)} records\n")

    for r in records[:5]:
        print(f"  LIN: {r['lin']}  NSN: {r['nsn']}  SN: {r['serial_number']}  DESC: {r['nsn_description']}")

    out_path = path.replace('.pdf', '_extracted.csv')
    with open(out_path, 'w') as f:
        f.write(to_csv(records))
    print(f"\nCSV saved: {out_path}")
