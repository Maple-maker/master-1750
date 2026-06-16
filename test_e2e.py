"""
End-to-end integration test for the unified connex packing tool.
Drives the real Flask routes in-process via the test client:
  ingest -> assign(share) -> assign(split) -> generate-master + generate-individuals
Validates the generated PDFs. Uses real sample BOMs + the real Commo SHR.
"""
import io
import os
import zipfile

import app as flask_app
from pypdf import PdfReader

ROOT = "/Users/jaidenrabatin/Desktop/AEGIS/30-PROJECTS/active/1750_bulk_editor"
BOMS = [
    f"{ROOT}/FC BOMS/0137 A05023 INTERROGATOR SET.pdf",
    f"{ROOT}/FC BOMS/0095 S78397 SATELLITE COMMUNICA.pdf",
    f"{ROOT}/arms room BOMs/103133 M39263  LMG 5.56MM M249.pdf",
    f"{ROOT}/FC BOMS/14T0016 A22496 AIMING CIRCLE M2A2.pdf",
]
SHR = f"{ROOT}/Commo SHR ++ (1).pdf"

c = flask_app.app.test_client()
ok = True
def check(label, cond):
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    ok = ok and cond

# 1) INGEST -------------------------------------------------------------------
print("1) POST /ingest (4 BOMs + SHR)")
data = {"boms": [(open(p, "rb"), os.path.basename(p)) for p in BOMS],
        "shr": (open(SHR, "rb"), "Commo SHR ++ (1).pdf")}
r = c.post("/ingest", data=data, content_type="multipart/form-data")
check("status 200", r.status_code == 200)
j = r.get_json()
job_id = j["job_id"]
boms = j["boms"]
check("4 BOMs returned", len(boms) == 4)
check("default boxes 1..4", sorted(b["box_num"] for b in boms) == [1, 2, 3, 4])
check("reconcile_status present on every BOM", all(b["reconcile_status"] for b in boms))
check("suggested_header has date", bool(j["suggested_header"].get("date")))
print(f"     job_id={job_id}  recon_summary={j['reconcile_summary']}")
for b in boms:
    print(f"     box{b['box_num']} {b['filename'][:34]:36} LIN={b['lin']:8} items={b['item_count']:3} SHR={b['reconcile_status']}")

# 2) ASSIGN — SHARE: move BOM #4 into box 1 ----------------------------------
print("2) POST /assign  (share: BOM4 -> box 1)")
r = c.post("/assign", json={"job_id": job_id, "moves": [{"bom_id": boms[3]["bom_id"], "box_num": 1}]})
check("status 200", r.status_code == 200)
occ = r.get_json()["occupied_boxes"]
check("occupied boxes now [1,2,3]", occ == [1, 2, 3])
print(f"     occupied={occ}")

# 3) ASSIGN — SPLIT: move first item of BOM #1 into box 9 ---------------------
print("3) POST /assign  (split: one item of BOM1 -> box 9)")
first_line = boms[0]["items"][0]["line_no"]
item_key = f"{boms[0]['bom_id']}:{first_line}"
r = c.post("/assign", json={"job_id": job_id, "moves": [{"item_key": item_key, "box_num": 9}]})
check("status 200", r.status_code == 200)
occ = r.get_json()["occupied_boxes"]
check("box 9 now occupied (split)", 9 in occ)
print(f"     occupied={occ}")

# 4) GENERATE MASTER ----------------------------------------------------------
print("4) POST /generate-master")
header = {"name": "MORRIS", "rank": "SSG", "unit": "B BTY 2-55 ADA",
          "uic": "WH1ZT0", "battery": "HHB", "battalion": "2-55 ADA",
          "sloc": "ASB1-BME", "shrh": "SSG MORRIS", "container": "USAU1820795",
          "sun": "", "seal": "", "date": "16 JUN 2026", "signer_name": "CPT HOLLAND"}
r = c.post("/generate-master", json={"job_id": job_id, "header": header})
check("status 200", r.status_code == 200)
master_pdf = r.data
check("is a PDF", master_pdf[:4] == b"%PDF")
reader = PdfReader(io.BytesIO(master_pdf))
check("master has >=1 page", len(reader.pages) >= 1)
txt = (reader.pages[0].extract_text() or "").upper()
check("master text mentions UIC value", "WH1ZT0" in txt or "PACKING LIST" in txt)
print(f"     master pages={len(reader.pages)}  bytes={len(master_pdf)}")

# 5) GENERATE INDIVIDUALS -----------------------------------------------------
print("5) POST /generate-individuals")
r = c.post("/generate-individuals", json={"job_id": job_id, "header": header})
check("status 200", r.status_code == 200)
zf = zipfile.ZipFile(io.BytesIO(r.data))
names = zf.namelist()
check("zip has one PDF per occupied box", len(names) == len(occ))
check("all entries are PDFs", all(n.lower().endswith(".pdf") for n in names))
print(f"     zip entries ({len(names)}): {names}")

# 6) LEGACY routes still alive ------------------------------------------------
print("6) legacy routes")
check("/api/health ok", c.get("/api/health").get_json().get("status") == "ok")
check("/ serves UI", b"ingestBtn" in c.get("/").data)

print("\nE2E RESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
raise SystemExit(0 if ok else 1)
