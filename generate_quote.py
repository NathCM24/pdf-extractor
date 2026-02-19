#!/usr/bin/env python3
"""
generate_quote.py — Waste Experts Quote Generator
Fully cleaned + conflict-free version
"""

import os
import sys
import json
import base64
import argparse
import re
from pathlib import Path

# ─────────────────────────────────────────────────────────────
# Dependency guard
# ─────────────────────────────────────────────────────────────

missing = []
try:
    import anthropic
except ImportError:
    missing.append("anthropic")

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.utils import ImageReader
except ImportError:
    missing.append("reportlab")

if missing:
    sys.exit(f"Missing packages. Run: pip install {' '.join(missing)}")

# ─────────────────────────────────────────────────────────────
# Layout / Branding
# ─────────────────────────────────────────────────────────────

NAVY = colors.HexColor("#1e2e3d")
GREEN = colors.HexColor("#8ec431")
WHITE = colors.white
LIGHT_ROW = colors.HexColor("#f0f4f8")
MID_GREY = colors.HexColor("#e2e8f0")
DARK_GREY = colors.HexColor("#2d3748")
TEXT_GREY = colors.HexColor("#444444")
LABEL_GREY = colors.HexColor("#718096")
GREEN_LIGHT = colors.HexColor("#e8f5d0")
BORDER_CLR = colors.HexColor("#c8d6e5")
BG_BOX = colors.HexColor("#f0f4f8")

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
CONTENT_W = PAGE_W - 2 * MARGIN
RADIUS = 2 * mm

WE_ADDRESS = [
    "School Lane, Kirkheaton",
    "Huddersfield, West Yorkshire",
    "HD5 0JS",
]

SCRIPT_DIR = Path(__file__).parent

FONT_R = "Helvetica"
FONT_B = "Helvetica-Bold"
FONT_XB = "Helvetica-Bold"

# ─────────────────────────────────────────────────────────────
# Supplier validation
# ─────────────────────────────────────────────────────────────

INVALID_PATTERNS = (
    "waste experts",
    "electrical waste",
    "electrical waste recycling group",
    "ewrg",
)

def _normalize_for_match(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())

def _is_invalid_supplier(name: str) -> bool:
    normalized = (name or "").strip().lower()
    compact = _normalize_for_match(normalized)

    if any(p in normalized for p in INVALID_PATTERNS):
        return True
    if "wasteexperts" in compact:
        return True
    if "electrical" in compact and "waste" in compact and "group" in compact:
        return True
    return False

# ─────────────────────────────────────────────────────────────
# Claude Prompt
# ─────────────────────────────────────────────────────────────

EXTRACT_PROMPT = """
Extract fields from this PO PDF and return ONLY valid JSON.

CRITICAL:
The PO provider MUST come from footer / terms / important info.

Never use Waste Experts / Electrical Waste / EWRG as issuer.

Return:

{
  "po_provider_name": "...",
  "po_provider_address": "...",
  "po_provider_email": "...",
  "client_name": "...",
  "client_address": "...",
  "client_email": "...",
  "reference_number": "...",
  "quote_expiry_date": "...",
  "job_name": "...",
  "site_postcode": "...",
  "line_items": [
    {
      "description": "...",
      "quantity": 1,
      "unit_price": 0.00,
      "line_total": 0.00
    }
  ],
  "notes": "...",
  "terms_important_info": "VERBATIM footer text"
}
"""

# ─────────────────────────────────────────────────────────────
# Extraction
# ─────────────────────────────────────────────────────────────

def normalize_extracted_data(data: dict) -> dict:
    name = (data.get("po_provider_name") or "").strip()
    address = (data.get("po_provider_address") or "").strip()
    email = (data.get("po_provider_email") or "").strip()
    terms = str(data.get("terms_important_info") or "")

    if _is_invalid_supplier(name):
        name = ""

    if not name:
        pattern = re.compile(
            r"\b([A-Z][A-Za-z&'.,-]*(?:\s+[A-Z][A-Za-z&'.,-]*){0,5}\s+(?:Ltd|Limited|PLC|LLP))\b"
        )
        matches = pattern.findall(terms)
        for m in matches:
            if not _is_invalid_supplier(m):
                name = m
                break

    data["supplier_name"] = name or None
    data["supplier_address"] = address or None
    data["supplier_email"] = email or None
    return data

def extract(pdf_path: Path) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        sys.exit("ANTHROPIC_API_KEY not set.")

    client = anthropic.Anthropic(api_key=api_key)
    b64 = base64.standard_b64encode(pdf_path.read_bytes()).decode()

    resp = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2500,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": EXTRACT_PROMPT},
                ],
            }
        ],
    )

    raw = resp.content[0].text.strip()

    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}") + 1
        raw = raw[start:end]

    data = json.loads(raw)
    return normalize_extracted_data(data)

# ─────────────────────────────────────────────────────────────
# PDF Generation (stable full layout)
# ─────────────────────────────────────────────────────────────

def money(val):
    try:
        return f"£{float(val):,.2f}"
    except:
        return "£0.00"

def generate_pdf(data: dict, logo_path: Path, out_path: Path):
    c = rl_canvas.Canvas(str(out_path), pagesize=A4)
    y = PAGE_H - MARGIN

    c.setFont(FONT_XB, 16)
    c.setFillColor(NAVY)
    c.drawString(MARGIN, y, "QUOTE")
    y -= 25

    supplier_name = data.get("supplier_name") or "PO provider not found"

    c.setFont(FONT_B, 11)
    c.drawString(MARGIN, y, "Bill To:")
    y -= 15
    c.setFont(FONT_R, 10)
    c.drawString(MARGIN, y, supplier_name)
    y -= 30

    total_sum = 0.0
    for item in data.get("line_items") or []:
        desc = str(item.get("description") or "")
        try:
            qty = float(item.get("quantity") or 1)
        except:
            qty = 1.0
        try:
            unit = float(item.get("unit_price") or 0)
        except:
            unit = 0.0

        total = qty * unit
        total_sum += total

        c.setFont(FONT_R, 9)
        c.drawString(MARGIN, y, desc[:90])
        c.drawRightString(PAGE_W - MARGIN, y, money(total))
        y -= 14

    y -= 20
    c.setFont(FONT_XB, 12)
    c.drawRightString(PAGE_W - MARGIN, y, f"TOTAL: {money(total_sum)}")

    c.save()
    print(f"[ok] Quote saved: {out_path}")

# ─────────────────────────────────────────────────────────────
# Entry
# ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input_pdf")
    ap.add_argument("--out")
    args = ap.parse_args()

    pdf_in = Path(args.input_pdf)
    if not pdf_in.exists():
        sys.exit("File not found.")

    data = extract(pdf_in)

    out_path = Path(args.out) if args.out else SCRIPT_DIR / "quote.pdf"
    generate_pdf(data, SCRIPT_DIR / "logo.png", out_path)

if __name__ == "__main__":
    main()