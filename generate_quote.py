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
import urllib.request
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
  "po_provider_name":    "PO provider/issuer company from terms/footer/important info (the company sending you the PO), or null",
  "po_provider_address": "PO provider postal address (newline-separated) from terms/footer/important info, or null",
  "po_provider_email":   "PO provider email address, or null",
  "supplier_name":       "Same as PO provider if present; otherwise null",
  "supplier_address":    "Same as PO provider address if present; otherwise null",
  "supplier_email":      "Same as PO provider email if present; otherwise null",
  "client_name":      "Company name of the buyer/client (if present), or null",
  "client_address":   "Full postal address of the client (newline-separated), or null",
  "client_email":     "Client email address, or null",
  "reference_number": "PO or reference number",
  "quote_expiry_date":"Valid-until / expiry date in DD/MM/YYYY format, or null",
  "job_name":         "Brief title or description for this job/quote",
  "site_postcode":    "Postcode of the site or delivery location (e.g. SG19 1QY), or null",
  "line_items": [
    {
      "description": "Clear, plain-English product/service summary from the PO (include waste/material type and service type where possible)",
      "quantity":    1,
      "unit_price":  0.00,
      "line_total":  0.00
    }
  ],
  "notes": "Any caveats, special instructions, or comments. Empty string if none.",
  "terms_important_info": "Verbatim bottom terms/footer/important-info block text, or empty string"
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

Prioritise PO provider identity from footer/terms/important info/signature blocks.
Never use Waste Experts, Electrical Waste, Electrical Waste Recycling Group, or the service/customer/site address entity as the supplier unless explicitly stated as the PO issuer in terms/footer.
If terms/footer says phrases like "Go Green Ltd employee" or "accept ... terms & conditions" tied to a company name, that company is the PO provider.
Use numeric types (not strings) for quantity, unit_price, and line_total.
Return ONLY the JSON object — no markdown fences, no explanation."""

INVALID_BILL_TO_PATTERNS = (
    "waste experts",
    "electrical waste",
    "electrical waste recycling group",
    "ewrg",
)

ALLOWED_SUPPLIER_NAMES_TEXT = """Go Green Ltd
Business Waste Ltd
BusinessWaste.co.uk Ltd
Divert.co.uk Ltd
Reconomy (UK) Ltd
Reconomy Group
Biffa Waste Services Ltd
Veolia ES (UK) Ltd
SUEZ Recycling and Recovery UK Ltd
FCC Environment (UK) Ltd
Grundon Waste Management Ltd
Viridor Waste Management Ltd
Tradebe UK Ltd
Tradebe Healthcare Ltd
Restore Datashred Ltd
Paper Round Ltd
Enva UK Ltd
Enva England Ltd
Sims Lifecycle Services Ltd
Sims Recycling Solutions Ltd
Mitie Waste Services Ltd
Mitie Environmental Services Ltd
Olleco Ltd
Valpak Ltd
Comply Direct Ltd
Anthesis Ltd
Recyclex Ltd
Clearsprings Waste Management Ltd
Waste Managed Ltd
Waste Care Ltd
Total Recycling Services Ltd
UK Waste Solutions Ltd
Waste Support Ltd
Waste Managed Services Ltd
Waste King Ltd
Waste Cost Reduction Services Ltd
Waste Mission Ltd
All Waste Matters Ltd
Waste Knot Energy Ltd
Wastebox Ltd
Initial Medical Services Ltd
Sharpsmart Ltd
SRCL Ltd
Rentokil Initial UK Ltd
Daniels Healthcare Ltd
SteriCycle UK Ltd
Recycling Lives Ltd
Hills Waste Solutions Ltd
Ward Recycling Ltd
Cawleys Waste Management Ltd
Bywaters Ltd
Powerday PLC
Geminor UK Ltd
Ecosurety Ltd
Augean Ltd
Envirogreen Ltd
Mitie Group plc
Sodexo Ltd
ISS Facility Services Ltd
CBRE Managed Services Ltd
Compass Group UK & Ireland Ltd
Engie Services Ltd
OCS Group UK Ltd
Bouygues E&S Solutions Ltd
Kier Facilities Services Ltd
Amey plc
Serco Ltd
LondonWaste Ltd
PHS Group Ltd
PHS Waste Management Ltd
PHS Teacrate Ltd
Countrystyle Recycling Ltd
Ellgia Ltd
Mick George Ltd
Beauparc Ltd
NWH Group Ltd
Fenix Recycling Ltd
Noble Recycling Ltd
Collecteco Ltd
Cory Environmental Ltd
Cory Riverside Energy Ltd
Riverside Waste Machinery Ltd
Clearaway Recycling Ltd
Willshee's Waste & Recycling Ltd
A1 Recycling Ltd
Recycling UK Ltd
Envirocycle London Ltd
Thalia Waste Management Ltd
Acumen Waste Services Ltd
Advanced Waste Solutions Ltd
Blueleaf Ltd
BPR Group Europe Ltd
Celtic Recycling Ltd
Cleansing Service Group Ltd
Clearfast Waste Consultancy Ltd
CLD Environmental Ltd
Coastal Recycling Ltd
Dunmow Waste Management Ltd
Eco Sustainable Solutions Ltd
ECO2 Management Services Ltd
Encon Insulation Ltd
Envirocraft Waste Solutions Ltd
EnviroHub Ltd
EPC-UK Ltd
Essex Waste Management Ltd
Europcar Waste Services Ltd
Forward Waste Management Ltd
Gaskells Waste Services Ltd
Gemini Recycling Ltd
Glasdon UK Ltd
Green Recycling Ltd
Greenstar Recycling Ltd
Greyparrot Ltd
Hadfields Waste Management Ltd
Hazrem Environmental Ltd
Hewden Hire Ltd
Hurn Recycling Ltd
Impact Recycling Ltd
J&B Recycling Ltd
J Dickinson & Sons Ltd
Jayplas Ltd
Kay Group Ltd
KCM Waste Management Ltd
Key Recycling Ltd
Lathams Ltd
Leafield Environmental Ltd
Leigh Environmental Ltd
LKM Recycling Ltd
LSS Waste Management Ltd
M&M Waste Solutions Ltd
Malary Ltd
May Glass Recycling Ltd
McCarthy Marland Ltd
Mid UK Recycling Ltd
Moran Waste Management Ltd
Moulded Foams Ltd
MTS Cleansing Services Ltd
New Earth Solutions Ltd
Norfolk Waste Management Ltd
North West Recycling Ltd
Oakleaf Recycling Ltd
Pallet Loop Ltd
Pennon Group plc
Perrywell Ltd
Plastics Recycling Ltd
PPR Recycling Ltd
Premier Waste Recycling Ltd
Quest Waste Management Ltd
R Collard Ltd
RDF Services Ltd
Recycled Products Ltd
Recycling Lives Services Ltd
Renewi UK Services Ltd
Resource Futures Ltd
RGM Environmental Ltd
RJ Recycling Ltd
S Roberts & Son Ltd
Savanna Rags International Ltd
Select Environmental Services Ltd
Seneca Resource Recovery Ltd
Shredall SDS Group Ltd
Simply Waste Solutions Ltd
Smith Recycling Ltd
Southern Waste Management Ltd
Spectrum Waste Ltd
Speedy Waste Ltd
SRS Waste Management Ltd
Stuart Partners Ltd
Sustainable Waste Management Ltd
SWEEEP Kuusakoski Ltd
The Green Group Ltd
The Recycling Partnership Ltd
Thorntons Recycling Ltd
Tom White Waste Ltd
Totus Environmental Ltd
UK Computer Recycling Ltd
UK Recycling Ltd
Universal Recycling Ltd
Valley Trading Ltd
Vanden Recycling Ltd
Viridor Energy Ltd
W&S Recycling Ltd
Wade Environmental Ltd
Waste2Tricity Ltd
Wastecycle Ltd
WastePlan Ltd
WCR Waste Consultancy Ltd
WEEE Solutions Ltd
West London Waste Authority
Western Bio-Energy Ltd
WGM Waste Management Ltd
White Recycling Ltd
William Tracey Group Ltd
WRM Recycling Ltd
Yorwaste Ltd
Zero Waste Group Ltd
Zest Recycle Ltd"""

ALLOWED_SUPPLIER_NAMES = [
    name.strip() for name in ALLOWED_SUPPLIER_NAMES_TEXT.splitlines() if name.strip()
]


def _normalize_for_match(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _is_invalid_supplier(name: str) -> bool:
    normalized = (name or "").strip().lower()
    compact = _normalize_for_match(normalized)

    if any(pat in normalized for pat in INVALID_BILL_TO_PATTERNS):
        return True

    # Catch OCR variants like "recyclcing" / "recyling" where explicit phrase match fails.
    if "wasteexperts" in compact:
        return True
    if "electrical" in compact and "waste" in compact and "group" in compact:
        return True

    return False


def _find_allowlisted_supplier(text: str) -> str:
    if not text:
        return ""

    lower_text = (text or "").lower()
    compact_text = _normalize_for_match(text)
    best_name, best_pos = "", None

    for name in ALLOWED_SUPPLIER_NAMES:
        pos = lower_text.find(name.lower())
        if pos == -1:
            compact_name = _normalize_for_match(name)
            compact_pos = compact_text.find(compact_name)
            if compact_pos == -1:
                continue
            pos = compact_pos

        if best_pos is None or pos < best_pos:
            best_name, best_pos = name, pos

    return best_name


def _extract_company_candidates(text: str) -> list:
    """Find likely UK company names from free text (e.g. footer terms)."""
    if not text:
        return []
    pattern = re.compile(r"\b([A-Z][A-Za-z&'.,-]*(?:\s+[A-Z][A-Za-z&'.,-]*){0,5}\s+(?:Ltd|Limited|PLC|LLP))\b")
    seen, names = set(), []
    for match in pattern.findall(text):
        name = " ".join(match.replace("\n", " ").split()).strip(" ,.-")
        key = name.lower()
        if key not in seen and not _is_invalid_supplier(name):
            seen.add(key)
            names.append(name)
    return names


def _extract_registered_office(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"Registered Office:\s*(.+?)(?:Registered in|Company Number|$)", text, flags=re.I | re.S)
    if not m:
        return ""
    return " ".join(m.group(1).replace("\n", " ").split()).strip(" ,")


def normalize_extracted_data(data: dict) -> dict:
    """Prefer PO provider details and avoid billing us/end-customer entities."""
    supplier_name = (data.get("po_provider_name") or data.get("supplier_name") or "").strip()
    supplier_address = (data.get("po_provider_address") or data.get("supplier_address") or "").strip()
    supplier_email = (data.get("po_provider_email") or data.get("supplier_email") or "").strip()

    terms_text = str(data.get("terms_important_info") or "")
    combined_text = "\n".join(
        part for part in [
            terms_text,
            str(data.get("notes") or ""),
            str(data.get("po_provider_name") or ""),
            str(data.get("supplier_name") or ""),
        ] if part
    )

    allowlisted_supplier = _find_allowlisted_supplier(combined_text)
    if allowlisted_supplier:
        supplier_name = allowlisted_supplier

    if not supplier_name or _is_invalid_supplier(supplier_name):
        candidates = _extract_company_candidates(combined_text)
        supplier_name = candidates[0] if candidates else ""

    if not supplier_address:
        supplier_address = _extract_registered_office(combined_text)

    if _is_invalid_supplier(supplier_name):
        supplier_name = ""
        supplier_address = ""
        supplier_email = ""

    data["supplier_name"] = supplier_name or None
    data["supplier_address"] = supplier_address or None
    data["supplier_email"] = supplier_email or None
    return data

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
        end   = raw.rfind("}") + 1
        raw   = raw[start:end]

    return normalize_extracted_data(json.loads(raw))

# ─── drawing helpers ─────────────────────────────────────────────────────────

def rounded_rect(c, x, y, w, h, r=RADIUS, fill=None, stroke=None, lw=0.5):
    """Draw a rounded-corner rectangle.  y = BOTTOM of rect (ReportLab coords)."""
    k = r * 0.5523  # Bezier control-point offset for quarter-circle approximation
    p = c.beginPath()
    p.moveTo(x + r, y)
    p.lineTo(x + w - r, y)
    p.curveTo(x + w - k, y,      x + w, y + k,      x + w, y + r)
    p.lineTo(x + w, y + h - r)
    p.curveTo(x + w, y + h - k,  x + w - k, y + h,  x + w - r, y + h)
    p.lineTo(x + r, y + h)
    p.curveTo(x + k, y + h,      x, y + h - k,       x, y + h - r)
    p.lineTo(x, y + r)
    p.curveTo(x, y + k,           x + k, y,           x + r, y)
    p.close()
    if fill is not None:
        c.setFillColor(fill)
    if stroke is not None:
        c.setStrokeColor(stroke)
        c.setLineWidth(lw)
    c.drawPath(p, fill=int(fill is not None), stroke=int(stroke is not None))


def money(val) -> str:
    try:
        return f"£{float(val):,.2f}"
    except (TypeError, ValueError):
        return "£0.00"
        end = raw.rfind("}") + 1
        raw = raw[start:end]

    data = json.loads(raw)
    return normalize_extracted_data(data)

def wrap_text(c, text, font, size, max_width) -> list:
    """Split text into lines that each fit within max_width, including long words."""
    def split_long_token(token):
        chunks = []
        remaining = token
        while remaining and c.stringWidth(remaining, font, size) > max_width:
            cut = len(remaining)
            while cut > 1 and c.stringWidth(remaining[:cut], font, size) > max_width:
                cut -= 1
            chunks.append(remaining[:cut])
            remaining = remaining[cut:]
        if remaining:
            chunks.append(remaining)
        return chunks or [""]

    words = (text or "").split()
    lines, line = [], ""
    for word in words:
        pieces = split_long_token(word)
        for piece in pieces:
            candidate = (line + " " + piece).strip()
            if c.stringWidth(candidate, font, size) <= max_width:
                line = candidate
            else:
                if line:
                    lines.append(line)
                line = piece
    if line:
        lines.append(line)
    return lines or [""]


def label(c, x, y, text):
    """Draw a small all-caps section label."""
    c.setFont(FONT_B, 7)
    c.setFillColor(NAVY)
    c.drawString(x, y, text.upper())
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

    def down(delta):
        nonlocal y
        y -= delta

    def draw_footer():
        c.setStrokeColor(MID_GREY)
        c.setLineWidth(0.5)
        c.line(MARGIN, MARGIN - 2 * mm, PAGE_W - MARGIN, MARGIN - 2 * mm)
        c.setFont(FONT_R, 7)
        c.setFillColor(LABEL_GREY)
        c.drawCentredString(
            PAGE_W / 2, MARGIN / 2,
            "Waste Experts Ltd  •  School Lane, Kirkheaton, Huddersfield HD5 0JS"
            "  •  emma-jane@wasteexperts.co.uk  •  +441388721000",
        )

    def ensure_space(required_height_mm, redraw=None):
        nonlocal y
        if (y - MARGIN) >= required_height_mm:
            return
        draw_footer()
        c.showPage()
        y = PAGE_H - MARGIN
        c.setFont(FONT_R, 9)
        c.setFillColor(TEXT_GREY)
        c.setStrokeColor(MID_GREY)
        if redraw:
            redraw()

    # ── Logo ────────────────────────────────────────────────────────────────
    logo_h = 16 * mm
    if logo_path.exists():
        try:
            img     = ImageReader(str(logo_path))
            iw, ih  = img.getSize()
            logo_w  = logo_h * (iw / ih)
            c.drawImage(str(logo_path), MARGIN, y - logo_h,
                        width=logo_w, height=logo_h,
                        preserveAspectRatio=True, mask="auto")
        except Exception:
            c.setFont(FONT_XB, 13)
            c.setFillColor(NAVY)
            c.drawString(MARGIN, y - logo_h + 2 * mm, "WASTE EXPERTS")
    else:
        c.setFont(FONT_XB, 13)
        c.setFillColor(NAVY)
        c.drawString(MARGIN, y - logo_h + 2 * mm, "WASTE EXPERTS")

    down(logo_h + 8 * mm)

    # ── Title (mirrors output file name) ─────────────────────────────────────
    title_text = out_path.stem.replace("_", " ").replace("-", " ").upper()
    c.setFillColor(NAVY)
    font_size = 19
    while font_size > 10 and c.stringWidth(title_text, FONT_XB, font_size) > CONTENT_W:
        font_size -= 1
    c.setFont(FONT_XB, font_size)
    c.drawCentredString(PAGE_W / 2, y, title_text)
    down(6 * mm)

    # ── Green divider ────────────────────────────────────────────────────────
    c.setStrokeColor(GREEN)
    c.setLineWidth(2)
    c.line(MARGIN, y, PAGE_W - MARGIN, y)
    down(7 * mm)

    # ── Two-column addresses ─────────────────────────────────────────────────
    col1_x = MARGIN
    col2_x = PAGE_W / 2 + 6 * mm
    addr_top = y

    # Left – Bill To
    label(c, col1_x, y, "Bill To")
    down(4.5 * mm)
    supplier_name = data.get("supplier_name") or "PO provider not found"
    supplier_address = data.get("supplier_address") or ""
    supplier_email = data.get("supplier_email")

    c.setFont(FONT_B, 10)
    c.setFillColor(NAVY)
    c.drawString(col1_x, y, supplier_name)
    down(5 * mm)
    c.setFont(FONT_R, 9)
    c.setFillColor(TEXT_GREY)
    addr_lines = [l.strip() for l in
                  (supplier_address or "").replace(", ", "\n").split("\n")
                  if l.strip()][:5]
    for al in addr_lines:
        c.drawString(col1_x, y, al)
        down(4.5 * mm)
    if supplier_email:
        c.drawString(col1_x, y, supplier_email)
        down(4.5 * mm)
    bottom_left = y

    # Right – From
    y = addr_top
    label(c, col2_x, y, "From")
    y -= 4.5 * mm
    c.setFont(FONT_B, 10)
    c.setFillColor(NAVY)
    c.drawString(col2_x, y, "Waste Experts")
    y -= 5 * mm
    c.setFont(FONT_R, 9)
    c.setFillColor(TEXT_GREY)
    for we_line in WE_ADDRESS:
        c.drawString(col2_x, y, we_line)
        y -= 4.5 * mm
    bottom_right = y

    y = min(bottom_left, bottom_right)
    down(6 * mm)

    # ── Prepared By (2-line layout to avoid overflow) ────────────────────────
    prep_h = 17 * mm
    rounded_rect(c, MARGIN, y - prep_h, CONTENT_W, prep_h,
                 fill=colors.HexColor("#f5f7f9"))
    label(c, MARGIN + 3 * mm, y - 4 * mm, "Prepared By")
    c.setFont(FONT_B, 9)
    c.setFillColor(NAVY)
    c.drawString(MARGIN + 3 * mm, y - 9 * mm, PREPARED_BY["name"])
    c.setFont(FONT_R, 8.5)
    c.setFillColor(TEXT_GREY)
    c.drawString(MARGIN + 3 * mm, y - 14 * mm, PREPARED_BY["title"])
    c.drawRightString(PAGE_W - MARGIN - 3 * mm, y - 14 * mm,
                      f"{PREPARED_BY['email']}  |  {PREPARED_BY['phone']}")
    down(prep_h + 6 * mm)

    # ── Reference / Expiry pills ─────────────────────────────────────────────
    box_w = 65 * mm
    box_h = 13 * mm

    rounded_rect(c, MARGIN, y - box_h, box_w, box_h,
                 fill=BG_BOX, stroke=MID_GREY)
    label(c, MARGIN + 3 * mm, y - 4.5 * mm, "Reference")
    c.setFont(FONT_B, 11)
    c.setFont(FONT_XB, 16)
    c.setFillColor(NAVY)
    c.drawString(MARGIN, y, "QUOTE")
    y -= 25

    supplier_name = data.get("supplier_name") or "PO provider not found"

    c.setFont(FONT_B, 11)
    c.setFillColor(NAVY)
    c.drawString(ex_x + 3 * mm, y - 9.5 * mm,
                 data.get("quote_expiry_date") or "—")

    down(box_h + 8 * mm)

    # ── Products & Services table ────────────────────────────────────────────
    col_w   = [CONTENT_W * 0.50, CONTENT_W * 0.11,
               CONTENT_W * 0.19, CONTENT_W * 0.20]
    headers = ["PRODUCTS & SERVICES", "QUANTITY", "PRICE PER UNIT", "LINE TOTAL"]
    hdr_h   = 9 * mm
    row_h   = 8 * mm

    def draw_table_header():
        rounded_rect(c, MARGIN, y - hdr_h, CONTENT_W, hdr_h, r=2 * mm, fill=NAVY)
        c.setFont(FONT_B, 8)
        c.setFillColor(WHITE)
        hx = MARGIN + 3 * mm
        for i, hdr in enumerate(headers):
            if i == 0:
                c.drawString(hx, y - hdr_h + 2.5 * mm, hdr)
            else:
                c.drawRightString(hx + col_w[i] - 3 * mm, y - hdr_h + 2.5 * mm, hdr)
            hx += col_w[i]
        down(hdr_h)

    ensure_space(hdr_h)
    draw_table_header()

    # Data rows
    grand_total = 0.0
    line_items  = data.get("line_items") or []
    for idx, item in enumerate(line_items):
        ensure_space(row_h, redraw=draw_table_header)
        desc  = str(item.get("description") or "")
        qty   = item.get("quantity", 1)
        unit  = float(item.get("unit_price") or 0)
        total = float(item.get("line_total") or (float(qty) * unit))
        grand_total += total

        row_fill = LIGHT_ROW if idx % 2 == 0 else WHITE
        c.setFillColor(row_fill)
        c.rect(MARGIN, y - row_h, CONTENT_W, row_h, fill=1, stroke=0)
        c.setStrokeColor(MID_GREY)
        c.setLineWidth(0.3)
        c.line(MARGIN, y - row_h, MARGIN + CONTENT_W, y - row_h)

        text_y  = y - row_h + 2.5 * mm
        max_desc = col_w[0] - 6 * mm

        # Truncate description if too wide
        desc_str = desc
        c.setFont(FONT_R, 9)
        while desc_str and c.stringWidth(desc_str, FONT_R, 9) > max_desc:
            desc_str = desc_str[:-1]
        if desc_str != desc:
            desc_str = desc_str[:-1] + "…"
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
        c.drawRightString(rx + col_w[1] - 3 * mm, text_y, str(qty))
        rx += col_w[1]
        c.drawRightString(rx + col_w[2] - 3 * mm, text_y, money(unit))
        rx += col_w[2]
        c.setFont(FONT_B, 9)
        c.drawRightString(rx + col_w[3] - 3 * mm, text_y, money(total))

        down(row_h)

    down(6 * mm)

    # ── Summary / Total ───────────────────────────────────────────────────────
    sum_w = 82 * mm
    sum_x = PAGE_W - MARGIN - sum_w
    sub_h = 10 * mm
    tot_h = 14 * mm

    ensure_space(sub_h + 2 * mm + tot_h + 10 * mm)

    # Subtotal (light green background)
    rounded_rect(c, sum_x, y - sub_h, sum_w, sub_h, fill=GREEN_LIGHT)
    c.setFont(FONT_R, 9)
    c.setFillColor(NAVY)
    c.drawString(sum_x + 4 * mm, y - sub_h + 3 * mm, "One-time subtotal")
    c.setFont(FONT_B, 9)
    c.drawRightString(sum_x + sum_w - 4 * mm, y - sub_h + 3 * mm, money(grand_total))
    down(sub_h + 2 * mm)

    # Total (green, larger)
    rounded_rect(c, sum_x, y - tot_h, sum_w, tot_h, r=3 * mm, fill=GREEN)
    c.setFont(FONT_B, 10)
    c.setFillColor(WHITE)
    c.drawString(sum_x + 5 * mm, y - tot_h / 2 - 1.5 * mm, "TOTAL")
    c.setFont(FONT_XB, 15)
    c.setFillColor(NAVY)
    c.drawRightString(sum_x + sum_w - 5 * mm, y - tot_h / 2 - 2.5 * mm,
                      money(grand_total))
    down(tot_h + 10 * mm)

    # ── Caveats / Comments ───────────────────────────────────────────────────
    notes = str(data.get("notes") or "").strip()
    note_lines = wrap_text(c, notes, FONT_R, 9, CONTENT_W - 8 * mm) if notes else []
    note_idx = 0

    while True:
        ensure_space(22 * mm)
        remaining = y - MARGIN - 5 * mm
        comm_h = max(22 * mm, min(remaining, 45 * mm))

        rounded_rect(c, MARGIN, y - comm_h, CONTENT_W, comm_h,
                     stroke=BORDER_CLR, lw=1.5)
        label(c, MARGIN + 4 * mm, y - 5 * mm, "Caveats / Comments")

        if not note_lines:
            c.setFont(FONT_R, 9)
            c.setFillColor(LABEL_GREY)
            c.drawString(MARGIN + 4 * mm, y - 11 * mm, "No additional notes.")
            down(comm_h)
            break

        note_y = y - 11 * mm
        box_bottom = y - comm_h + 4 * mm
        c.setFont(FONT_R, 9)
        c.setFillColor(TEXT_GREY)
        while note_idx < len(note_lines) and note_y >= box_bottom:
            c.drawString(MARGIN + 4 * mm, note_y, note_lines[note_idx])
            note_idx += 1
            note_y -= 4.5 * mm

        down(comm_h)
        if note_idx >= len(note_lines):
            break
        down(6 * mm)

    # ── Footer ───────────────────────────────────────────────────────────────
    draw_footer()
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

    if args.job_name:
        data["job_name"] = args.job_name

    print(f"  Client    : {data.get('client_name', '–')}")
    print(f"  Reference : {data.get('reference_number', '–')}")
    print(f"  Expiry    : {data.get('quote_expiry_date', '–')}")
    print(f"  Items     : {len(data.get('line_items') or [])}")

    # Logo: prefer the WhatsApp image, fall back to generic names
    logo_path = next(
        (p for pattern in (
            "WhatsApp_Image_*.jpeg", "WhatsApp_Image_*.jpg",
            "logo.png", "logo.jpg",
        ) for p in SCRIPT_DIR.glob(pattern)),
        SCRIPT_DIR / "logo.png",
    )

    # Output path
    if args.out:
        out_path = Path(args.out)
    else:
        client = (data.get("client_name") or "customer").strip().lower()
        postcode = (data.get("site_postcode") or "unknown-postcode").strip().lower()

        def slugify(val):
            cleaned = "".join(ch if (ch.isalnum() or ch in " -_") else " " for ch in val)
            slug = "-".join(part for part in cleaned.replace("_", " ").split() if part)
            return slug[:60] or "quote"

        out_path = SCRIPT_DIR / f"{slugify(client)}-{slugify(postcode)}.pdf"

    print("Rendering PDF...")
    generate_pdf(data, logo_path, out_path)

    out_path = Path(args.out) if args.out else SCRIPT_DIR / "quote.pdf"
    generate_pdf(data, SCRIPT_DIR / "logo.png", out_path)

if __name__ == "__main__":
    main()