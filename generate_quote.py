#!/usr/bin/env python3
"""
generate_quote.py — Waste Experts Quote Generator

Reads a purchase order PDF, extracts key fields with Claude AI,
and renders a branded Waste Experts quote PDF.

Key fix:
- Supplier (PO provider/issuer) is forced to come from the Terms/Important Info/footer block.
- We explicitly forbid selecting Waste Experts / Electrical Waste / EWRG as supplier, even with OCR variants.
- We ask Claude to (a) return the verbatim footer block, and (b) explicitly choose the issuer from that block.

Usage:
    python generate_quote.py input.pdf
    python generate_quote.py input.pdf --job-name "Fluorescent Tubes Collection"
    python generate_quote.py input.pdf --out my_quote.pdf

Requirements:
    pip install anthropic reportlab pillow
    ANTHROPIC_API_KEY environment variable must be set.
"""

import os
import sys
import json
import base64
import argparse
import urllib.request
import re
from pathlib import Path

# ─── dependency guard ────────────────────────────────────────────────────────

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
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
except ImportError:
    missing.append("reportlab")
if missing:
    sys.exit(f"Missing packages. Run:  pip install {' '.join(missing)}")

# ─── brand colours ───────────────────────────────────────────────────────────

NAVY        = colors.HexColor("#1e2e3d")
GREEN       = colors.HexColor("#8ec431")
WHITE       = colors.white
LIGHT_ROW   = colors.HexColor("#f0f4f8")
MID_GREY    = colors.HexColor("#e2e8f0")
DARK_GREY   = colors.HexColor("#2d3748")
TEXT_GREY   = colors.HexColor("#444444")
LABEL_GREY  = colors.HexColor("#718096")
GREEN_LIGHT = colors.HexColor("#e8f5d0")
BORDER_CLR  = colors.HexColor("#c8d6e5")
BG_BOX      = colors.HexColor("#f0f4f8")

# ─── layout ──────────────────────────────────────────────────────────────────

PAGE_W, PAGE_H = A4
MARGIN    = 18 * mm
CONTENT_W = PAGE_W - 2 * MARGIN
RADIUS    = 2 * mm

# ─── fixed content ───────────────────────────────────────────────────────────

WE_ADDRESS = ["School Lane, Kirkheaton", "Huddersfield, West Yorkshire", "HD5 0JS"]

PREPARED_BY = {
    "name":  "Emma Dedeke",
    "title": "Internal Account Manager",
    "email": "emma-jane@wasteexperts.co.uk",
    "phone": "+441388721000",
}

SCRIPT_DIR = Path(__file__).parent

# ─── fonts ───────────────────────────────────────────────────────────────────

FONT_DIR = SCRIPT_DIR / "fonts"
FONT_SPECS = [
    ("Montserrat",           "Montserrat-Regular.ttf"),
    ("Montserrat-SemiBold",  "Montserrat-SemiBold.ttf"),
    ("Montserrat-Bold",      "Montserrat-Bold.ttf"),
    ("Montserrat-ExtraBold", "Montserrat-ExtraBold.ttf"),
]
_GH_BASE = "https://media.githubusercontent.com/media/google/fonts/main/ofl/montserrat/static/"

# Module-level font name variables – overridden to Helvetica on download failure
FONT_R  = "Montserrat"
FONT_SB = "Montserrat-SemiBold"
FONT_B  = "Montserrat-Bold"
FONT_XB = "Montserrat-ExtraBold"


def ensure_fonts():
    """Locate and register Montserrat TTFs with ReportLab.

    Resolution order for each font file:
      1. fonts/ subdirectory next to this script
      2. Windows user fonts directory
      3. Windows system fonts directory
      4. Download from GitHub (fallback, may fail)
    Falls back to Helvetica if none of the above succeed.
    """
    global FONT_R, FONT_SB, FONT_B, FONT_XB
    FONT_DIR.mkdir(exist_ok=True)

    # Extra search dirs (Windows font locations)
    _win_user = Path.home() / "AppData/Local/Microsoft/Windows/Fonts"
    _win_sys  = Path("C:/Windows/Fonts")
    _search   = [FONT_DIR, _win_user, _win_sys]

    all_ok = True
    for face, fname in FONT_SPECS:
        # 1. Find the file in one of the known locations
        found = next((d / fname for d in _search if (d / fname).exists()), None)

        # 2. Copy to fonts/ so ReportLab always loads from a stable path
        dest = FONT_DIR / fname
        if found and found != dest:
            import shutil as _shutil
            _shutil.copy2(found, dest)
            found = dest

        # 3. Download as last resort
        if not found:
            url = _GH_BASE + fname
            print(f"  Downloading {fname}...")
            try:
                urllib.request.urlretrieve(url, dest)
                found = dest
            except Exception as exc:
                print(f"  [!] Could not obtain {fname}: {exc}")
                all_ok = False
                continue

        try:
            pdfmetrics.registerFont(TTFont(face, str(found)))
        except Exception as exc:
            print(f"  [!] Could not register {face}: {exc}")
            all_ok = False

    if not all_ok:
        print("  -> Falling back to Helvetica for missing fonts.")
        FONT_R  = "Helvetica"
        FONT_SB = "Helvetica"
        FONT_B  = "Helvetica-Bold"
        FONT_XB = "Helvetica-Bold"


# ─── Claude extraction ───────────────────────────────────────────────────────

EXTRACT_PROMPT = """Extract the fields below from this PO PDF and return ONLY valid JSON.

CRITICAL RULE:
- The PO provider/issuer (supplier to bill) MUST be identified from the *bottom Terms / Important Info / Footer* block.
- Do NOT choose Waste Experts / Electrical Waste / Electrical Waste Recycling Group as the issuer unless the footer explicitly states they issued the PO.

Return JSON in this shape:

{
  "po_provider_name":    "Issuer company from footer/terms/important info/signature block, or null",
  "po_provider_address": "Issuer postal address from footer/terms/important info (newline-separated), or null",
  "po_provider_email":   "Issuer email from footer/terms/important info, or null",

  "supplier_name":       "Same as po_provider_name if present; otherwise null",
  "supplier_address":    "Same as po_provider_address if present; otherwise null",
  "supplier_email":      "Same as po_provider_email if present; otherwise null",

  "client_name":      "Company name of the buyer/client (if present), or null",
  "client_address":   "Full postal address of the client (newline-separated), or null",
  "client_email":     "Client email address, or null",
  "reference_number": "PO or reference number",
  "quote_expiry_date":"Valid-until / expiry date in DD/MM/YYYY format, or null",
  "job_name":         "Brief title or description for this job/quote",
  "site_postcode":    "Postcode of the site or delivery location (e.g. SG19 1QY), or null",
  "line_items": [
    {
      "description": "Clear, plain-English product/service summary (include waste/material type and service type where possible)",
      "quantity":    1,
      "unit_price":  0.00,
      "line_total":  0.00
    }
  ],
  "notes": "Any caveats, special instructions, or comments. Empty string if none.",
  "terms_important_info": "VERBATIM bottom terms/footer/important-info block text (copy exactly), or empty string"
}

Extra guidance:
- Prioritise footer cues like: 'Registered Office', 'Company No', 'VAT', 'Terms and Conditions', 'On behalf of <Company>', '<Company> employees', signature blocks, or a footer company name/address.
- Use numeric types (not strings) for quantity, unit_price, line_total.
Return ONLY the JSON object. No markdown. No explanation.
"""

# Anything like these should NEVER be the supplier/issuer.
INVALID_BILL_TO_PATTERNS = (
    "waste experts",
    "electrical waste",
    "electrical waste recycling group",
    "ewrg",
)


def _is_invalid_supplier(name: str) -> bool:
    """
    Reject obvious self/sister-brand matches and common OCR variants.
    We use both substring matching and compacted alpha-only matching.
    """
    normalized = (name or "").strip().lower()
    compact = re.sub(r"[^a-z]", "", normalized)

    if any(pat in normalized for pat in INVALID_BILL_TO_PATTERNS):
        return True

    # OCR/spacing variants
    if "wasteexperts" in compact:
        return True

    # Electrical Waste Recycling Group variants (spacing/typos)
    if "electrical" in compact and "waste" in compact and "recycling" in compact and "group" in compact:
        return True

    return False


def _extract_company_candidates(text: str) -> list:
    """Find likely UK company names from free text (e.g. footer terms)."""
    if not text:
        return []
    pattern = re.compile(
        r"\b([A-Z][A-Za-z&'.,-]*(?:\s+[A-Z][A-Za-z&'.,-]*){0,6}\s+(?:Ltd|Limited|PLC|LLP))\b"
    )
    seen, names = set(), []
    for match in pattern.findall(text):
        name = " ".join(match.replace("\n", " ").split()).strip(" ,.-")
        key = name.lower()
        if key not in seen and not _is_invalid_supplier(name):
            seen.add(key)
            names.append(name)
    return names


def _extract_registered_office(text: str) -> str:
    """Try to pull a Registered Office address block from free text."""
    if not text:
        return ""
    m = re.search(
        r"Registered Office[:\s]*\s*(.+?)(?:Registered in|Company Number|Company No\.?|Reg(?:istered)? No\.?|VAT|$)",
        text,
        flags=re.I | re.S,
    )
    if not m:
        return ""
    return " ".join(m.group(1).replace("\n", " ").split()).strip(" ,")


def _extract_email(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", text, flags=re.I)
    return (m.group(0) if m else "").strip()


def normalize_extracted_data(data: dict) -> dict:
    """
    Force issuer/supplier from Terms/Important Info/footer when possible.

    Strategy:
    1) Prefer po_provider_* from Claude.
    2) If missing OR invalid (Waste Experts/EWRG/etc), derive from terms_important_info using heuristics.
    3) If still missing, fall back to any other candidates (notes etc) but NEVER invalid.
    """
    # Start with Claude's preferred issuer fields
    supplier_name = (data.get("po_provider_name") or "").strip()
    supplier_address = (data.get("po_provider_address") or "").strip()
    supplier_email = (data.get("po_provider_email") or "").strip()

    terms_text = str(data.get("terms_important_info") or "").strip()
    notes_text = str(data.get("notes") or "").strip()

    # If Claude didn't populate, or populated with an invalid "us" name, fix from footer text.
    if (not supplier_name) or _is_invalid_supplier(supplier_name):
        candidates = _extract_company_candidates(terms_text)
        supplier_name = candidates[0] if candidates else ""

    # Address should come from footer first
    if not supplier_address:
        supplier_address = _extract_registered_office(terms_text)

    # Email should come from footer first
    if not supplier_email:
        supplier_email = _extract_email(terms_text)

    # If still empty, use broader combined text as a last resort (but still disallow invalid)
    combined_text = "\n".join(part for part in [terms_text, notes_text] if part).strip()
    if (not supplier_name) and combined_text:
        candidates = _extract_company_candidates(combined_text)
        supplier_name = candidates[0] if candidates else ""
    if (not supplier_address) and combined_text:
        supplier_address = _extract_registered_office(combined_text)
    if (not supplier_email) and combined_text:
        supplier_email = _extract_email(combined_text)

    # Final guardrail: never allow invalid names through.
    if _is_invalid_supplier(supplier_name):
        supplier_name = ""
        supplier_address = ""
        supplier_email = ""

    # Set the canonical fields used by the PDF
    data["supplier_name"] = supplier_name or None
    data["supplier_address"] = supplier_address or None
    data["supplier_email"] = supplier_email or None

    # Also keep po_provider_* aligned to the same chosen issuer, for debugging/printing
    data["po_provider_name"] = data.get("po_provider_name") or (supplier_name or None)
    data["po_provider_address"] = data.get("po_provider_address") or (supplier_address or None)
    data["po_provider_email"] = data.get("po_provider_email") or (supplier_email or None)

    return data


def extract(pdf_path: Path) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        sys.exit("Error: ANTHROPIC_API_KEY environment variable is not set.")

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

    # Strip accidental fences / leading text.
    if "```" in raw or not raw.lstrip().startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start != -1 and end != -1:
            raw = raw[start:end]

    data = json.loads(raw)
    return normalize_extracted_data(data)


# ─── drawing helpers ─────────────────────────────────────────────────────────

def rounded_rect(c, x, y, w, h, r=RADIUS, fill=None, stroke=None, lw=0.5):
    """Draw a rounded-corner rectangle. y = BOTTOM of rect (ReportLab coords)."""
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


# ─── PDF generation ──────────────────────────────────────────────────────────

def generate_pdf(data: dict, logo_path: Path, out_path: Path):
    c = rl_canvas.Canvas(str(out_path), pagesize=A4)
    y = PAGE_H - MARGIN  # cursor starts at top

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
            PAGE_W / 2,
            MARGIN / 2,
            "Waste Experts Ltd  •  School Lane, Kirkheaton, Huddersfield HD5 0JS"
            "  •  emma-jane@wasteexperts.co.uk  •  +441388721000",
        )

    def ensure_space(required_height, redraw=None):
        """Ensure there's at least required_height points available above bottom margin."""
        nonlocal y
        if (y - MARGIN) >= required_height:
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
            img = ImageReader(str(logo_path))
            iw, ih = img.getSize()
            logo_w = logo_h * (iw / ih)
            c.drawImage(
                str(logo_path),
                MARGIN,
                y - logo_h,
                width=logo_w,
                height=logo_h,
                preserveAspectRatio=True,
                mask="auto",
            )
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

    # Left – Bill To (issuer / PO provider)
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
    addr_lines = [
        l.strip()
        for l in (supplier_address or "").replace(", ", "\n").split("\n")
        if l.strip()
    ][:5]
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

    # ── Prepared By ─────────────────────────────────────────────────────────
    prep_h = 17 * mm
    ensure_space(prep_h + 6 * mm)
    rounded_rect(c, MARGIN, y - prep_h, CONTENT_W, prep_h, fill=colors.HexColor("#f5f7f9"))
    label(c, MARGIN + 3 * mm, y - 4 * mm, "Prepared By")
    c.setFont(FONT_B, 9)
    c.setFillColor(NAVY)
    c.drawString(MARGIN + 3 * mm, y - 9 * mm, PREPARED_BY["name"])
    c.setFont(FONT_R, 8.5)
    c.setFillColor(TEXT_GREY)
    c.drawString(MARGIN + 3 * mm, y - 14 * mm, PREPARED_BY["title"])
    c.drawRightString(
        PAGE_W - MARGIN - 3 * mm,
        y - 14 * mm,
        f"{PREPARED_BY['email']}  |  {PREPARED_BY['phone']}",
    )
    down(prep_h + 6 * mm)

    # ── Reference / Expiry pills ─────────────────────────────────────────────
    box_w = 65 * mm
    box_h = 13 * mm

    ensure_space(box_h + 8 * mm)
    rounded_rect(c, MARGIN, y - box_h, box_w, box_h, fill=BG_BOX, stroke=MID_GREY)
    label(c, MARGIN + 3 * mm, y - 4.5 * mm, "Reference")
    c.setFont(FONT_B, 11)
    c.setFillColor(NAVY)
    c.drawString(MARGIN + 3 * mm, y - 9.5 * mm, data.get("reference_number") or "—")

    ex_x = MARGIN + box_w + 5 * mm
    rounded_rect(c, ex_x, y - box_h, box_w, box_h, fill=BG_BOX, stroke=MID_GREY)
    label(c, ex_x + 3 * mm, y - 4.5 * mm, "Quote Valid Until")
    c.setFont(FONT_B, 11)
    c.setFillColor(NAVY)
    c.drawString(ex_x + 3 * mm, y - 9.5 * mm, data.get("quote_expiry_date") or "—")

    down(box_h + 8 * mm)

    # ── Products & Services table ────────────────────────────────────────────
    col_w = [CONTENT_W * 0.50, CONTENT_W * 0.11, CONTENT_W * 0.19, CONTENT_W * 0.20]
    headers = ["PRODUCTS & SERVICES", "QUANTITY", "PRICE PER UNIT", "LINE TOTAL"]
    hdr_h = 9 * mm
    row_h = 8 * mm

    def draw_table_header():
        nonlocal y
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

    grand_total = 0.0
    line_items = data.get("line_items") or []
    for idx, item in enumerate(line_items):
        ensure_space(row_h, redraw=draw_table_header)

        desc = str(item.get("description") or "")
        qty = item.get("quantity", 1)
        try:
            qty_f = float(qty)
        except (TypeError, ValueError):
            qty_f = 1.0

        try:
            unit = float(item.get("unit_price") or 0)
        except (TypeError, ValueError):
            unit = 0.0

        try:
            total = float(item.get("line_total") or (qty_f * unit))
        except (TypeError, ValueError):
            total = qty_f * unit

        grand_total += total

        row_fill = LIGHT_ROW if idx % 2 == 0 else WHITE
        c.setFillColor(row_fill)
        c.rect(MARGIN, y - row_h, CONTENT_W, row_h, fill=1, stroke=0)
        c.setStrokeColor(MID_GREY)
        c.setLineWidth(0.3)
        c.line(MARGIN, y - row_h, MARGIN + CONTENT_W, y - row_h)

        text_y = y - row_h + 2.5 * mm
        max_desc = col_w[0] - 6 * mm

        desc_str = desc
        c.setFont(FONT_R, 9)
        while desc_str and c.stringWidth(desc_str, FONT_R, 9) > max_desc:
            desc_str = desc_str[:-1]
        if desc_str != desc and len(desc_str) > 1:
            desc_str = desc_str[:-1] + "…"

        c.setFillColor(DARK_GREY)
        c.drawString(MARGIN + 3 * mm, text_y, desc_str)

        rx = MARGIN + col_w[0]
        c.setFont(FONT_R, 9)
        c.drawRightString(
            rx + col_w[1] - 3 * mm,
            text_y,
            str(int(qty_f) if qty_f.is_integer() else qty_f),
        )
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

    rounded_rect(c, sum_x, y - sub_h, sum_w, sub_h, fill=GREEN_LIGHT)
    c.setFont(FONT_R, 9)
    c.setFillColor(NAVY)
    c.drawString(sum_x + 4 * mm, y - sub_h + 3 * mm, "One-time subtotal")
    c.setFont(FONT_B, 9)
    c.drawRightString(sum_x + sum_w - 4 * mm, y - sub_h + 3 * mm, money(grand_total))
    down(sub_h + 2 * mm)

    rounded_rect(c, sum_x, y - tot_h, sum_w, tot_h, r=3 * mm, fill=GREEN)
    c.setFont(FONT_B, 10)
    c.setFillColor(WHITE)
    c.drawString(sum_x + 5 * mm, y - tot_h / 2 - 1.5 * mm, "TOTAL")
    c.setFont(FONT_XB, 15)
    c.setFillColor(NAVY)
    c.drawRightString(sum_x + sum_w - 5 * mm, y - tot_h / 2 - 2.5 * mm, money(grand_total))
    down(tot_h + 10 * mm)

    # ── Caveats / Comments ───────────────────────────────────────────────────
    notes = str(data.get("notes") or "").strip()
    note_lines = wrap_text(c, notes, FONT_R, 9, CONTENT_W - 8 * mm) if notes else []
    note_idx = 0

    while True:
        ensure_space(22 * mm)
        remaining = y - MARGIN - 5 * mm
        comm_h = max(22 * mm, min(remaining, 45 * mm))

        rounded_rect(c, MARGIN, y - comm_h, CONTENT_W, comm_h, stroke=BORDER_CLR, lw=1.5)
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

    draw_footer()
    c.save()
    print(f"[ok] Quote saved: {out_path}")


# ─── entry point ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Waste Experts Quote Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python generate_quote.py po.pdf\n"
            "  python generate_quote.py po.pdf --job-name 'Fluorescent Tube Collection'\n"
            "  python generate_quote.py po.pdf --out quote-final.pdf"
        ),
    )
    ap.add_argument("input_pdf", help="Supplier purchase order PDF to read")
    ap.add_argument("--job-name", help="Override the quote title")
    ap.add_argument("--out", help="Output PDF path (default: auto-generated)")
    args = ap.parse_args()

    pdf_in = Path(args.input_pdf)
    if not pdf_in.exists():
        sys.exit(f"File not found: {pdf_in}")

    print("Checking fonts...")
    ensure_fonts()

    print(f"Extracting data from:  {pdf_in.name}")
    data = extract(pdf_in)

    if args.job_name:
        data["job_name"] = args.job_name

    print(f"  Supplier  : {data.get('supplier_name', '–')}")
    print(f"  Client    : {data.get('client_name', '–')}")
    print(f"  Reference : {data.get('reference_number', '–')}")
    print(f"  Expiry    : {data.get('quote_expiry_date', '–')}")
    print(f"  Items     : {len(data.get('line_items') or [])}")

    # Logo: prefer the WhatsApp image, fall back to generic names
    logo_path = next(
        (
            p
            for pattern in (
                "WhatsApp_Image_*.jpeg",
                "WhatsApp_Image_*.jpg",
                "logo.png",
                "logo.jpg",
            )
            for p in SCRIPT_DIR.glob(pattern)
        ),
        SCRIPT_DIR / "logo.png",
    )

    # Output path
    if args.out:
        out_path = Path(args.out)
    else:
        client = (data.get("client_name") or "customer").strip().lower()
        postcode = (data.get("site_postcode") or "unknown-postcode").strip().lower()

        def slugify(val: str) -> str:
            cleaned = "".join(ch if (ch.isalnum() or ch in " -_") else " " for ch in (val or ""))
            slug = "-".join(part for part in cleaned.replace("_", " ").split() if part)
            return slug[:60] or "quote"

        out_path = SCRIPT_DIR / f"{slugify(client)}-{slugify(postcode)}.pdf"

    print("Rendering PDF...")
    generate_pdf(data, logo_path, out_path)


if __name__ == "__main__":
    main()