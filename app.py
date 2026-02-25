from flask import Flask, jsonify, render_template, request, send_file
import base64
import io
import json
import os
import re
import requests as req_lib
import urllib.request
from datetime import date, datetime
from io import BytesIO
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Google Sheets integration (optional)
_gspread_available = False
try:
    import gspread
    from google.oauth2.service_account import Credentials as ServiceCredentials
    _gspread_available = True
except ImportError:
    pass

import anthropic
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from brokers import BROKERS, BROKER_LIST_TEXT

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB
LAST_REVIEW_PAYLOAD = {}

SCRIPT_DIR = Path(__file__).parent

# ─── Supplier template storage ────────────────────────────────────────────────
# Use TEMPLATES_PATH env var to set the full path to the templates JSON file.
# Defaults to /data/supplier_templates.json (Railway persistent volume).
# Falls back to a local file next to app.py when /data/ is not available
# (e.g. local development without a mounted volume).
_default_templates_path = Path("/data/supplier_templates.json")
_local_templates_path = SCRIPT_DIR / "supplier_templates.json"

def _resolve_templates_path():
    """Pick the best writable location for the templates file."""
    explicit = os.environ.get("TEMPLATES_PATH")
    if explicit:
        p = Path(explicit)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    # Default: try /data/ (Railway volume)
    try:
        _default_templates_path.parent.mkdir(parents=True, exist_ok=True)
        # Verify the directory is actually writable
        test_file = _default_templates_path.parent / ".write_test"
        test_file.touch()
        test_file.unlink()
        return _default_templates_path
    except OSError:
        # /data/ not available — fall back to local file for development
        return _local_templates_path

TEMPLATES_FILE = _resolve_templates_path()

# Ensure the file exists on startup with an empty object
if not TEMPLATES_FILE.exists():
    TEMPLATES_FILE.parent.mkdir(parents=True, exist_ok=True)
    TEMPLATES_FILE.write_text("{}")
TRAINING_PDF_CACHE = {}  # supplier -> base64 pdf data (temporary, per-session)


def _load_templates():
    """Load supplier templates from JSON file."""
    if TEMPLATES_FILE.exists():
        try:
            with open(TEMPLATES_FILE, "r") as f:
                return json.loads(f.read())
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def _save_templates(templates):
    """Save supplier templates to JSON file."""
    with open(TEMPLATES_FILE, "w") as f:
        f.write(json.dumps(templates, indent=2))


EXTRACT_PROMPT = f"""You are extracting data from a supplier purchase order PDF sent to Waste Logics.

STEP 1 — IDENTIFY THE SUPPLIER
Scan the ENTIRE document and match supplier against this approved account list:
{BROKER_LIST_TEXT}

STEP 2 — Return ONLY valid JSON with this shape:
{{
  "account_name": "Account/client/supplier name from approved list, or null",
  "supplier": "Best matching supplier from approved list, or null",
  "purchase_order_number": "PO/order reference, or null",
  "service_description": "Short product/service description from the products or pricing table (e.g. 'Corrugated Flo Tube Pipe - Exchange'), or null",
  "customer_name": "Customer name or waste producer name from the PO, or null",
  "sic_code": "SIC code if mentioned anywhere in the document, or null",
  "site_contact": "Primary contact, or null",
  "site_contact_number": "Primary contact number, or null",
  "site_contact_email": "Primary contact email, or null",
  "secondary_site_contact": "Secondary contact, or null",
  "secondary_site_contact_number": "Secondary contact number, or null",
  "secondary_site_contact_email": "Secondary contact email, or null",
  "site_name": "Site name, or null",
  "site_address": "Site address excluding postcode, newline separated, or null",
  "site_postcode": "Site postcode, or null",
  "opening_times": "Opening hours, or null",
  "access": "Access details, or null",
  "site_restrictions": "Site restrictions, or null",
  "special_instructions": "Special instructions/notes, or null",
  "document_type": "Consignment Note or Waste Transfer Note if explicitly stated, else null",
  "line_items": [
    {{
      "description": "Product or service description (e.g. '8ft Dura Pipe - Exchange'). Max 8 words.",
      "container": "Container type (Green Steel, Magnum, Collapsible, Pallet, Non-Linear Crate, Durapipe, Battery POD). Default Magnum.",
      "waste_stream": "WEEE category name from the 15 categories listed below, or null",
      "quantity": 1,
      "unit_price": 0.00,
      "line_total": 0.00
    }}
  ],
  "overall_total": 0.00
}}

CONTAINER AUTO-DETECTION (for each line item, pick the best match from the description):
- If description contains "Green Steel" → "Green Steel"
- If description contains "Magnum" → "Magnum"
- If description contains "Collapsible" → "Collapsible"
- If description contains "Pallet" → "Pallet"
- If description contains "Non-Linear" or "Crate" → "Non-Linear Crate"
- If description contains "Dura" or "Durapipe" or "Flo Tube" → "Durapipe"
- If description contains "Battery" or "POD" → "Battery POD"
- If none match → default to "Magnum"

SMART LINE SPLITTING — MULTIPLE CONTAINERS:
If a product description references multiple container types from this list [Green Steel, Magnum, Collapsible, Pallet, Non-Linear Crate, Durapipe, Battery POD], create a SEPARATE line item for each container. Extract the quantity for each container if mentioned.

Examples:
- "Magnum & Green Steel Empty & Replace" → TWO line items:
  Line 1: container "Magnum", quantity 1, description "Empty & Replace"
  Line 2: container "Green Steel", quantity 1, description "Empty & Replace"
- "2x Magnum, 1x Collapsible - Mixed WEEE Collection" → TWO line items:
  Line 1: container "Magnum", quantity 2, description "Mixed WEEE Collection"
  Line 2: container "Collapsible", quantity 1, description "Mixed WEEE Collection"
- If only one container is mentioned, keep it as a single line item as normal.

QUANTITY EXTRACTION:
For each line item, extract the quantity from the PO text. Look for patterns like:
- "2x", "x2", "2 x", "qty 2", "quantity: 2", "qty: 2"
- A number immediately before the container name (e.g. "2 Magnum")
- If no quantity is found, default to 1.

WASTE STREAM AUTO-DETECTION (for each line item, pick the most appropriate WEEE category):
The 15 WEEE categories are:
1. Large Household Appliances
2. Small Household Appliances
3. IT and Telecommunications Equipment
4. Consumer Equipment
5. Lighting Equipment
6. Electrical and Electronic Tools
7. Toys, Leisure and Sports Equipment
8. Medical Devices
9. Monitoring and Control Equipment
10. Automatic Dispensers
11. Display Equipment
12. Appliances Containing Refrigerants
13. Gas Discharge Lamps and LED Light Sources
14. PV Panels (Solar Panels)
15. Vapes and Electronic Cigarettes

Hints for waste stream detection:
- "Lamp", "Fluorescent", "Tube", "LED" → 13. Gas Discharge Lamps and LED Light Sources
- "WEEE", "Mixed WEEE", "Small WEEE" → 2. Small Household Appliances
- "Fridge", "Refrigerant", "Cooling" → 12. Appliances Containing Refrigerants
- "Screen", "Monitor", "Display", "TV" → 11. Display Equipment
- "Battery", "Batteries" → 2. Small Household Appliances (default)
- "IT", "Computer", "Server" → 3. IT and Telecommunications Equipment
- "Vape", "E-Cig" → 15. Vapes and Electronic Cigarettes
- "Solar", "PV", "Panel" → 14. PV Panels (Solar Panels)
- "Consignment Note", "Waste Transfer Note" → leave waste_stream as null (it's a document, not waste)
- Use your own knowledge to intelligently map to the most appropriate WEEE category
- If no match → null (leave blank for manual selection)

RULES:
- Use null when a value is genuinely not found.
- Use numeric types for quantity, unit_price, line_total, overall_total.
- Extract ALL line items from the products/services/pricing table.
- If only one service with a transport/pricing cost, create one line item.
- overall_total is the sum of all line_total values.
- Return JSON only. No markdown. No explanation.
"""


def _build_all_template_hints(templates):
    """Build template hints for all trained suppliers to include in prompt."""
    if not templates:
        return ""
    lines = [
        "SUPPLIER TEMPLATE HINTS (after identifying the supplier, "
        "use the matching hints below to guide extraction):"
    ]
    for supplier, tmpl in templates.items():
        hints = []
        if tmpl.get("layout_description"):
            hints.append(tmpl["layout_description"])
        for field, location in (tmpl.get("field_locations") or {}).items():
            readable = field.replace("_", " ").title()
            hints.append(f"{readable}: {location}")
        if hints:
            lines.append(f"\n{supplier}:")
            for h in hints:
                lines.append(f"  - {h}")
    return "\n".join(lines)


def _calculate_confidence(data, template):
    """Calculate per-field confidence scores based on extraction and template."""
    confidence = {}
    fields = [
        "purchase_order_number", "service_description",
        "customer_name", "sic_code",
        "site_contact", "site_contact_number", "site_contact_email",
        "secondary_site_contact", "secondary_site_contact_number",
        "secondary_site_contact_email",
        "site_name", "site_address", "site_postcode",
        "opening_times", "access", "site_restrictions",
        "special_instructions", "document_type",
    ]
    auto_fields = set(template.get("auto_extracted_fields", [])) if template else set()
    corrected_fields = set(template.get("manually_corrected_fields", [])) if template else set()

    for field in fields:
        value = data.get(field)
        has_value = value is not None and str(value).strip() != ""
        if not has_value:
            confidence[field] = "low"
        elif template:
            if field in auto_fields:
                confidence[field] = "high"
            elif field in corrected_fields:
                confidence[field] = "medium"
            else:
                confidence[field] = "high"
        else:
            confidence[field] = "medium"

    line_items = data.get("line_items", [])
    confidence["line_items"] = ("high" if template else "medium") if line_items else "low"
    confidence["overall_total"] = confidence["line_items"]
    return confidence



# ─── Brand constants ──────────────────────────────────────────────────────────

NAVY = colors.HexColor("#1e2e3d")
GREEN = colors.HexColor("#8ec431")
GREEN_PALE = colors.HexColor("#f0f8e0")
WHITE = colors.white
LIGHT_ROW = colors.HexColor("#f4f7fb")
MID_GREY = colors.HexColor("#dde4ec")
TEXT_DARK = colors.HexColor("#1e2e3d")
TEXT_BODY = colors.HexColor("#2d3748")
LABEL_GREY = colors.HexColor("#718096")
BORDER_CLR = colors.HexColor("#c8d6e5")
BG_BOX = colors.HexColor("#f4f7fb")
SECTION_BG = colors.HexColor("#e8edf2")

PAGE_W, PAGE_H = A4
MARGIN = 14 * mm
CONTENT_W = PAGE_W - 2 * MARGIN
RADIUS = 2 * mm

WE_ADDRESS = ["School Lane, Kirkheaton", "Huddersfield, West Yorkshire", "HD5 0JS"]
PREPARED_BY = {
    "name": "Emma Dedeke",
    "title": "Internal Account Manager",
    "email": "emma-jane@wasteexperts.co.uk",
    "phone": "+441388721000",
}

BRAND_LOGO_URL = (
    "https://i0.wp.com/wasteexperts.co.uk/wp-content/uploads/"
    "2022/11/green-grey-logo-1080.png?w=1920&ssl=1"
)

FONT_DIR = SCRIPT_DIR / "fonts"

# Font names — overridden to Helvetica if Montserrat unavailable
FONT_R = "Montserrat"
FONT_SB = "Montserrat-SemiBold"
FONT_B = "Montserrat-Bold"
FONT_XB = "Montserrat-ExtraBold"
_fonts_registered = False


def _ensure_fonts():
    """Register Montserrat TTFs with ReportLab (once per process)."""
    global FONT_R, FONT_SB, FONT_B, FONT_XB, _fonts_registered
    if _fonts_registered:
        return
    _fonts_registered = True

    specs = [
        ("Montserrat", "Montserrat-Regular.ttf"),
        ("Montserrat-SemiBold", "Montserrat-SemiBold.ttf"),
        ("Montserrat-Bold", "Montserrat-Bold.ttf"),
        ("Montserrat-ExtraBold", "Montserrat-ExtraBold.ttf"),
    ]
    all_ok = True
    for face, fname in specs:
        path = FONT_DIR / fname
        if not path.exists():
            all_ok = False
            continue
        try:
            pdfmetrics.registerFont(TTFont(face, str(path)))
        except Exception:
            all_ok = False

    if not all_ok:
        FONT_R = "Helvetica"
        FONT_SB = "Helvetica"
        FONT_B = "Helvetica-Bold"
        FONT_XB = "Helvetica-Bold"


def _get_logo_reader():
    """Return an ImageReader for the Waste Experts logo, or None."""
    # Try local files first
    for pattern in ("logo.png", "logo.jpg"):
        p = SCRIPT_DIR / pattern
        if p.exists():
            try:
                return ImageReader(str(p))
            except Exception:
                pass
    # Fall back to remote CDN
    try:
        with urllib.request.urlopen(BRAND_LOGO_URL, timeout=8) as resp:
            return ImageReader(io.BytesIO(resp.read()))
    except Exception:
        return None


# ─── Drawing helpers ──────────────────────────────────────────────────────────

def _rounded_rect(c, x, y, w, h, r=RADIUS, fill=None, stroke=None, lw=0.5):
    """Draw a rounded-corner rectangle. y = BOTTOM of rect."""
    k = r * 0.5523
    p = c.beginPath()
    p.moveTo(x + r, y)
    p.lineTo(x + w - r, y)
    p.curveTo(x + w - k, y, x + w, y + k, x + w, y + r)
    p.lineTo(x + w, y + h - r)
    p.curveTo(x + w, y + h - k, x + w - k, y + h, x + w - r, y + h)
    p.lineTo(x + r, y + h)
    p.curveTo(x + k, y + h, x, y + h - k, x, y + h - r)
    p.lineTo(x, y + r)
    p.curveTo(x, y + k, x + k, y, x + r, y)
    p.close()
    if fill is not None:
        c.setFillColor(fill)
    if stroke is not None:
        c.setStrokeColor(stroke)
        c.setLineWidth(lw)
    c.drawPath(p, fill=int(fill is not None), stroke=int(stroke is not None))


def _wrap_text(c, text, font, size, max_width):
    """Split text into lines that fit within max_width."""
    words = (text or "").split()
    lines, line = [], ""
    for word in words:
        candidate = (line + " " + word).strip()
        if c.stringWidth(candidate, font, size) <= max_width:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines or [""]


def _sanitise_filename(text):
    """Replace characters that are invalid in filenames with a dash."""
    return re.sub(r'[/\\:*?"<>|]', "-", text).strip() or "Unknown"


# ─── WEEE categories (number → name) ─────────────────────────────────────────

WEEE_CATEGORIES = {
    1: "Large Household Appliances",
    2: "Small Household Appliances",
    3: "IT and Telecommunications Equipment",
    4: "Consumer Equipment",
    5: "Lighting Equipment",
    6: "Electrical and Electronic Tools",
    7: "Toys, Leisure and Sports Equipment",
    8: "Medical Devices",
    9: "Monitoring and Control Equipment",
    10: "Automatic Dispensers",
    11: "Display Equipment",
    12: "Appliances Containing Refrigerants",
    13: "Gas Discharge Lamps and LED Light Sources",
    14: "PV Panels (Solar Panels)",
    15: "Vapes and Electronic Cigarettes",
}


def _format_waste_stream_display(item):
    """Return the waste stream display string for a line item.
    Format: '{WEEE Category Name} | {Product Description}'
    If no WEEE number, just the product description."""
    weee_num = item.get("weee_number") or ""
    weee_cat = item.get("weee_category") or ""
    desc = str(item.get("description") or "").strip()

    # Try to resolve category from weee_number if weee_category is missing
    if weee_num and not weee_cat:
        try:
            weee_cat = WEEE_CATEGORIES.get(int(weee_num), "")
        except (ValueError, TypeError):
            pass

    # If no weee_number, try to parse from waste_stream string "N. Category Name"
    if not weee_cat:
        ws = str(item.get("waste_stream") or "").strip()
        if ws:
            m = re.match(r"^(\d{1,2})\.\s+(.+)", ws)
            if m:
                weee_cat = m.group(2).strip()
            elif ws in WEEE_CATEGORIES.values():
                weee_cat = ws

    if weee_cat and desc:
        return f"{weee_cat} | {desc}"
    elif weee_cat:
        return weee_cat
    elif desc:
        return desc
    return str(item.get("waste_stream") or item.get("description") or "")


def _waste_stream_parts(item):
    """Return (weee_line, desc_line) for two-line display.
    weee_line: e.g. '13. Gas Discharge Lamps and LED Light Sources'
    desc_line: e.g. 'Lamp Green Steel Empty & Replace'
    Either may be empty string if not available."""
    weee_num = item.get("weee_number") or ""
    weee_cat = item.get("weee_category") or ""
    desc = str(item.get("description") or "").strip()

    # Resolve category from weee_number if weee_category is missing
    if weee_num and not weee_cat:
        try:
            weee_cat = WEEE_CATEGORIES.get(int(weee_num), "")
        except (ValueError, TypeError):
            pass

    # Resolve weee_num from weee_category name if we have category but no number
    if weee_cat and not weee_num:
        for num, name in WEEE_CATEGORIES.items():
            if name == weee_cat:
                weee_num = str(num)
                break

    # If no weee_cat, try to parse from waste_stream string "N. Category Name"
    if not weee_cat:
        ws = str(item.get("waste_stream") or "").strip()
        if ws:
            m = re.match(r"^(\d{1,2})\.\s+(.+)", ws)
            if m:
                weee_num = m.group(1)
                weee_cat = m.group(2).strip()
            elif ws in WEEE_CATEGORIES.values():
                weee_cat = ws
                for num, name in WEEE_CATEGORIES.items():
                    if name == ws:
                        weee_num = str(num)
                        break

    # Build the WEEE display line with number prefix
    if weee_cat and weee_num:
        weee_line = f"{weee_num}. {weee_cat}"
    elif weee_cat:
        weee_line = weee_cat
    else:
        weee_line = ""

    return weee_line, desc


# ─── Helper: inject £40 doc-type line into line items if applicable ──────────

def _inject_doc_type_line(line_items, document_type):
    """Append a £40 Consignment Note / Waste Transfer Note line item
    to *line_items* (in-place) if *document_type* is one of the two note
    types and no such line already exists.  Returns line_items for chaining."""
    doc_type = (document_type or "").strip()
    if doc_type not in ("Consignment Note", "Waste Transfer Note"):
        return line_items
    note_names = {"consignment note", "waste transfer note"}
    already_listed = any(
        (it.get("description") or "").strip().lower() in note_names
        for it in line_items
    )
    if not already_listed:
        line_items.append({
            "description": doc_type, "container": "", "waste_stream": "",
            "movement_type": "", "price": 40.00,
            "_is_doc_type_line": True,
        })
    return line_items


# ─── Professional PDF builder ────────────────────────────────────────────────

def _build_review_pdf(payload: dict) -> BytesIO:
    _ensure_fonts()

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    y = PAGE_H - MARGIN

    logo_reader = _get_logo_reader()

    # ── Helper closures ───────────────────────────────────────────────────

    def down(delta):
        nonlocal y
        y -= delta

    def draw_top_border():
        """Green accent bar at top of page."""
        c.setFillColor(GREEN)
        c.rect(0, PAGE_H - 5, PAGE_W, 5, stroke=0, fill=1)

    def draw_footer():
        c.setStrokeColor(MID_GREY)
        c.setLineWidth(0.5)
        c.line(MARGIN, MARGIN - 2 * mm, PAGE_W - MARGIN, MARGIN - 2 * mm)
        c.setFont(FONT_R, 7)
        c.setFillColor(LABEL_GREY)
        c.drawCentredString(
            PAGE_W / 2,
            MARGIN / 2,
            "Waste Experts Ltd  \u2022  School Lane, Kirkheaton, Huddersfield HD5 0JS"
            "  \u2022  emma-jane@wasteexperts.co.uk  \u2022  +441388721000",
        )

    def ensure_space(needed):
        nonlocal y
        if (y - MARGIN - 5 * mm) >= needed:
            return
        draw_footer()
        c.showPage()
        y = PAGE_H - MARGIN
        draw_top_border()

    def section_label(x, sy, text):
        c.setFont(FONT_B, 7)
        c.setFillColor(NAVY)
        c.drawString(x, sy, text.upper())

    # ── Page background ──────────────────────────────────────────────────
    draw_top_border()

    # ── Logo (centred) ────────────────────────────────────────────────────
    logo_h = 16 * mm
    if logo_reader:
        try:
            iw, ih = logo_reader.getSize()
            logo_w = logo_h * (iw / ih)
            c.drawImage(
                logo_reader,
                (PAGE_W - logo_w) / 2,
                y - logo_h,
                width=logo_w,
                height=logo_h,
                preserveAspectRatio=True,
                mask="auto",
            )
        except Exception:
            pass
    down(logo_h + 6 * mm)

    # ── Title (use service description if available) ─────────────────────
    service_desc = (payload.get("service_description") or "").strip()
    if service_desc:
        title_text = service_desc.upper()
        c.setFillColor(NAVY)
        font_size = 16
        while font_size > 9 and c.stringWidth(title_text, FONT_XB, font_size) > CONTENT_W:
            font_size -= 0.5
        c.setFont(FONT_XB, font_size)
        c.drawCentredString(PAGE_W / 2, y, title_text)
        down(6 * mm)

    # ── Green horizontal rule ────────────────────────────────────────────
    c.setStrokeColor(GREEN)
    c.setLineWidth(2)
    c.line(MARGIN, y, PAGE_W - MARGIN, y)
    down(7 * mm)

    # ── Two-column: Bill To / From ────────────────────────────────────────
    col1_x = MARGIN
    col2_x = PAGE_W / 2 + 6 * mm
    addr_top = y

    # Left — BILL TO
    section_label(col1_x, y, "Bill To")
    down(4.5 * mm)
    account_name = (payload.get("account_name") or "").strip() or "Unknown"
    supplier_address = (payload.get("supplier_address") or "").strip()

    c.setFont(FONT_B, 10)
    c.setFillColor(NAVY)
    c.drawString(col1_x, y, account_name)
    down(5 * mm)
    c.setFont(FONT_R, 9)
    c.setFillColor(TEXT_BODY)
    if supplier_address:
        for al in [l.strip() for l in supplier_address.split("\n") if l.strip()][:6]:
            c.drawString(col1_x, y, al)
            down(4.5 * mm)
    bottom_left = y

    # Right — FROM
    y = addr_top
    section_label(col2_x, y, "From")
    y -= 4.5 * mm
    c.setFont(FONT_B, 10)
    c.setFillColor(NAVY)
    c.drawString(col2_x, y, "Waste Experts")
    y -= 5 * mm
    c.setFont(FONT_R, 9)
    c.setFillColor(TEXT_BODY)
    for we_line in WE_ADDRESS:
        c.drawString(col2_x, y, we_line)
        y -= 4.5 * mm
    bottom_right = y

    y = min(bottom_left, bottom_right)
    down(6 * mm)

    # ── Prepared By row ───────────────────────────────────────────────────
    # Use person details from payload if available (CEF), else default
    prep_person = {
        "name": payload.get("person_name") or PREPARED_BY["name"],
        "title": payload.get("person_title") or PREPARED_BY["title"],
        "email": payload.get("person_email") or PREPARED_BY["email"],
        "phone": payload.get("person_phone") or PREPARED_BY["phone"],
    }
    prep_h = 17 * mm
    ensure_space(prep_h + 6 * mm)
    _rounded_rect(c, MARGIN, y - prep_h, CONTENT_W, prep_h, fill=BG_BOX, stroke=MID_GREY)
    section_label(MARGIN + 3 * mm, y - 4 * mm, "Prepared By")
    c.setFont(FONT_B, 9)
    c.setFillColor(NAVY)
    c.drawString(MARGIN + 3 * mm, y - 9 * mm, prep_person["name"])
    c.setFont(FONT_R, 8.5)
    c.setFillColor(TEXT_BODY)
    c.drawString(MARGIN + 3 * mm, y - 14 * mm, prep_person["title"])
    c.drawRightString(
        PAGE_W - MARGIN - 3 * mm,
        y - 14 * mm,
        f"{prep_person['email']}  |  {prep_person['phone']}",
    )
    down(prep_h + 6 * mm)

    # ── Reference / Document Type / Quote Valid Until boxes ──────────────
    box_gap = 4 * mm
    box_w = (CONTENT_W - 2 * box_gap) / 3
    box_h = 13 * mm
    ensure_space(box_h + 8 * mm)

    _rounded_rect(c, MARGIN, y - box_h, box_w, box_h, fill=BG_BOX, stroke=MID_GREY)
    section_label(MARGIN + 3 * mm, y - 4.5 * mm, "Reference")
    c.setFont(FONT_B, 11)
    c.setFillColor(NAVY)
    c.drawString(MARGIN + 3 * mm, y - 9.5 * mm, payload.get("purchase_order_number") or "\u2014")

    dt_x = MARGIN + box_w + box_gap
    _rounded_rect(c, dt_x, y - box_h, box_w, box_h, fill=BG_BOX, stroke=MID_GREY)
    section_label(dt_x + 3 * mm, y - 4.5 * mm, "Document Type")
    c.setFont(FONT_B, 10)
    c.setFillColor(NAVY)
    c.drawString(dt_x + 3 * mm, y - 9.5 * mm, payload.get("document_type") or "\u2014")

    ex_x = MARGIN + 2 * (box_w + box_gap)
    _rounded_rect(c, ex_x, y - box_h, box_w, box_h, fill=BG_BOX, stroke=MID_GREY)
    section_label(ex_x + 3 * mm, y - 4.5 * mm, "Quote Valid Until")
    c.setFont(FONT_B, 11)
    c.setFillColor(NAVY)
    c.drawString(ex_x + 3 * mm, y - 9.5 * mm, "\u2014")

    down(box_h + 8 * mm)

    # ── Products & Services table (Container | Qty | Waste Stream | Movement Type | Price) ──
    line_items = list(payload.get("line_items") or [])

    # Inject document type as a £40 line item if applicable
    _inject_doc_type_line(line_items, payload.get("document_type"))

    if line_items:
        ps_col_w = [CONTENT_W * 0.17, CONTENT_W * 0.07, CONTENT_W * 0.32, CONTENT_W * 0.19, CONTENT_W * 0.25]
        ps_headers = ["CONTAINER", "QTY", "WASTE STREAM", "MOVEMENT TYPE", "PRICE"]
        ps_hdr_h = 9 * mm
        ps_row_h_single = 8 * mm   # rows without two-line waste stream
        ps_row_h_double = 14 * mm  # rows with WEEE category + description

        def draw_ps_header():
            nonlocal y
            _rounded_rect(c, MARGIN, y - ps_hdr_h, CONTENT_W, ps_hdr_h, r=2 * mm, fill=GREEN)
            c.setFont(FONT_B, 7.5)
            c.setFillColor(WHITE)
            hx = MARGIN + 3 * mm
            for i, hdr in enumerate(ps_headers):
                if i == len(ps_headers) - 1:
                    c.drawRightString(hx + ps_col_w[i] - 3 * mm, y - ps_hdr_h + 2.5 * mm, hdr)
                else:
                    c.drawString(hx, y - ps_hdr_h + 2.5 * mm, hdr)
                hx += ps_col_w[i]
            down(ps_hdr_h)

        ensure_space(ps_hdr_h)
        draw_ps_header()

        def _money(val):
            try:
                return f"\u00a3{float(val):,.2f}"
            except (TypeError, ValueError):
                return "\u00a30.00"

        grand_total = 0.0
        for idx, item in enumerate(line_items):
            container_val = str(item.get("container") or "")
            quantity_val = item.get("quantity", 1)
            try:
                quantity_val = int(quantity_val)
            except (TypeError, ValueError):
                quantity_val = 1
            weee_line, desc_line = _waste_stream_parts(item)
            movement_type = str(item.get("movement_type") or "")
            try:
                price = float(item.get("price") or item.get("line_total") or item.get("unit_price") or 0)
            except (TypeError, ValueError):
                price = 0.0

            grand_total += price

            # Use taller row when we have both WEEE category and description
            has_two_lines = bool(weee_line and desc_line)
            ps_row_h = ps_row_h_double if has_two_lines else ps_row_h_single
            ensure_space(ps_row_h)

            row_fill = LIGHT_ROW if idx % 2 == 0 else WHITE
            c.setFillColor(row_fill)
            c.rect(MARGIN, y - ps_row_h, CONTENT_W, ps_row_h, fill=1, stroke=0)
            c.setStrokeColor(MID_GREY)
            c.setLineWidth(0.3)
            c.line(MARGIN, y - ps_row_h, MARGIN + CONTENT_W, y - ps_row_h)

            # Waste Stream column — two-line layout
            ws_x = MARGIN + ps_col_w[0] + ps_col_w[1]
            max_ws_w = ps_col_w[2] - 6 * mm

            if has_two_lines:
                # Line 1: WEEE category (smaller font, muted grey)
                line1_y = y - 5.5 * mm
                c.setFont(FONT_R, 6.5)
                c.setFillColor(LABEL_GREY)
                weee_str = weee_line
                if c.stringWidth(weee_str, FONT_R, 6.5) > max_ws_w:
                    weee_str = _wrap_text(c, weee_str, FONT_R, 6.5, max_ws_w)[0]
                c.drawString(ws_x + 3 * mm, line1_y, weee_str)

                # Line 2: Product description (normal font)
                line2_y = y - ps_row_h + 2.5 * mm
                c.setFont(FONT_R, 8)
                c.setFillColor(TEXT_BODY)
                desc_str = desc_line
                if c.stringWidth(desc_str, FONT_R, 8) > max_ws_w:
                    desc_str = _wrap_text(c, desc_str, FONT_R, 8, max_ws_w)[0]
                c.drawString(ws_x + 3 * mm, line2_y, desc_str)

                # Vertical midpoint for other columns
                text_y = y - ps_row_h / 2 - 1 * mm
            else:
                # Single line: show whichever is available
                text_y = y - ps_row_h + 2.5 * mm
                single_text = weee_line or desc_line or str(item.get("waste_stream") or item.get("description") or "")
                c.setFont(FONT_R, 8)
                c.setFillColor(TEXT_BODY)
                if c.stringWidth(single_text, FONT_R, 8) > max_ws_w:
                    single_text = _wrap_text(c, single_text, FONT_R, 8, max_ws_w)[0]
                c.drawString(ws_x + 3 * mm, text_y, single_text)

            # Container
            c.setFont(FONT_R, 8)
            c.setFillColor(TEXT_BODY)
            c.drawString(MARGIN + 3 * mm, text_y, container_val or "\u2014")

            # Quantity (skip for auto-added document type line)
            qty_x = MARGIN + ps_col_w[0]
            c.setFont(FONT_R, 8)
            c.setFillColor(TEXT_BODY)
            if not item.get("_is_doc_type_line"):
                c.drawString(qty_x + 3 * mm, text_y, str(quantity_val))

            # Movement Type
            mt_x = MARGIN + ps_col_w[0] + ps_col_w[1] + ps_col_w[2]
            c.setFont(FONT_R, 8)
            c.setFillColor(TEXT_BODY)
            c.drawString(mt_x + 3 * mm, text_y, movement_type or "\u2014")

            # Price
            price_x = MARGIN + ps_col_w[0] + ps_col_w[1] + ps_col_w[2] + ps_col_w[3]
            c.setFont(FONT_B, 9)
            c.setFillColor(NAVY)
            c.drawRightString(price_x + ps_col_w[3] - 3 * mm, text_y, _money(price))

            down(ps_row_h)

        # Overall total row
        overall_total = grand_total
        tot_row_h = 10 * mm
        ensure_space(tot_row_h)
        _rounded_rect(c, MARGIN, y - tot_row_h, CONTENT_W, tot_row_h, r=2 * mm, fill=GREEN)
        c.setFont(FONT_B, 10)
        c.setFillColor(WHITE)
        c.drawString(MARGIN + 5 * mm, y - tot_row_h + 3 * mm, "TOTAL")
        c.setFont(FONT_XB, 12)
        c.setFillColor(WHITE)
        c.drawRightString(PAGE_W - MARGIN - 5 * mm, y - tot_row_h + 3 * mm, _money(overall_total))
        down(tot_row_h + 8 * mm)

    # ── Main data table ───────────────────────────────────────────────────
    label_col_w = CONTENT_W * 0.38
    value_col_w = CONTENT_W * 0.62
    row_h = 8 * mm
    section_hdr_h = 7 * mm

    sections = [
        ("ORDER DETAILS", [
            ("Account Name (Waste Logics)", payload.get("account_name")),
            ("Supplier", "Waste Experts"),
            ("Purchase Order Number", payload.get("purchase_order_number")),
        ]),
        ("CONTACT INFORMATION", [
            ("Site Contact", payload.get("site_contact")),
            ("Site Contact Number", payload.get("site_contact_number")),
            ("Site Contact Email", payload.get("site_contact_email")),
            ("Secondary Site Contact", payload.get("secondary_site_contact")),
            ("Secondary Site Contact Number", payload.get("secondary_site_contact_number")),
            ("Secondary Site Contact Email", payload.get("secondary_site_contact_email")),
        ]),
        ("SITE INFORMATION", [
            ("Site Name", payload.get("site_name")),
            ("Site Address", payload.get("site_address")),
            ("Site Postcode", payload.get("site_postcode")),
        ]),
        ("ACCESS & INSTRUCTIONS", [
            ("Opening Times", payload.get("opening_times")),
            ("Access", payload.get("access")),
            ("Site Restrictions", payload.get("site_restrictions")),
            ("Special Instructions", payload.get("special_instructions")),
        ]),
    ]

    row_index = 0
    for section_title, fields in sections:
        # Section header
        ensure_space(section_hdr_h + row_h)
        c.setFillColor(SECTION_BG)
        c.rect(MARGIN, y - section_hdr_h, CONTENT_W, section_hdr_h, fill=1, stroke=0)
        c.setStrokeColor(MID_GREY)
        c.setLineWidth(0.3)
        c.line(MARGIN, y - section_hdr_h, MARGIN + CONTENT_W, y - section_hdr_h)
        c.setFont(FONT_B, 7.5)
        c.setFillColor(LABEL_GREY)
        c.drawString(MARGIN + 4 * mm, y - section_hdr_h + 2 * mm, section_title)
        down(section_hdr_h)

        for field_label, field_value in fields:
            value_str = str(field_value or "").strip()
            if not value_str:
                value_str = "\u2014"

            # Wrap long values across multiple lines
            value_lines = []
            for raw_line in value_str.split("\n"):
                value_lines.extend(
                    _wrap_text(c, raw_line.strip(), FONT_R, 9, value_col_w - 8 * mm)
                )

            this_row_h = max(row_h, len(value_lines) * 4 * mm + 4 * mm)
            ensure_space(this_row_h)

            # Alternate row shading
            row_fill = LIGHT_ROW if row_index % 2 == 0 else WHITE
            c.setFillColor(row_fill)
            c.rect(MARGIN, y - this_row_h, CONTENT_W, this_row_h, fill=1, stroke=0)

            # Row border
            c.setStrokeColor(MID_GREY)
            c.setLineWidth(0.3)
            c.line(MARGIN, y - this_row_h, MARGIN + CONTENT_W, y - this_row_h)

            # Vertical divider
            c.line(MARGIN + label_col_w, y, MARGIN + label_col_w, y - this_row_h)

            # Label text (bold, navy)
            c.setFont(FONT_B, 9)
            c.setFillColor(NAVY)
            label_y = y - (this_row_h / 2) - 1 * mm if len(value_lines) <= 1 else y - 5 * mm
            c.drawString(MARGIN + 4 * mm, label_y, field_label)

            # Value text
            c.setFont(FONT_R, 9)
            c.setFillColor(TEXT_BODY)
            val_y = y - 5 * mm
            for vl in value_lines:
                c.drawString(MARGIN + label_col_w + 4 * mm, val_y, vl)
                val_y -= 4 * mm

            down(this_row_h)
            row_index += 1

    # Table bottom border
    c.setStrokeColor(MID_GREY)
    c.setLineWidth(0.5)
    c.line(MARGIN, y, MARGIN + CONTENT_W, y)

    down(8 * mm)

    # ── Caveats / Comments box ────────────────────────────────────────────
    special = str(payload.get("special_instructions") or "").strip()
    note_lines = _wrap_text(c, special, FONT_R, 9, CONTENT_W - 8 * mm) if special else []

    comm_content_h = max(18 * mm, (len(note_lines) * 4.5 * mm) + 14 * mm)
    ensure_space(comm_content_h)

    _rounded_rect(
        c, MARGIN, y - comm_content_h, CONTENT_W, comm_content_h,
        stroke=BORDER_CLR, lw=1.5,
    )
    section_label(MARGIN + 4 * mm, y - 5 * mm, "Caveats / Comments")

    if note_lines:
        c.setFont(FONT_R, 9)
        c.setFillColor(TEXT_BODY)
        note_y = y - 11 * mm
        for nl in note_lines:
            c.drawString(MARGIN + 4 * mm, note_y, nl)
            note_y -= 4.5 * mm
    else:
        c.setFont(FONT_R, 9)
        c.setFillColor(LABEL_GREY)
        c.drawString(MARGIN + 4 * mm, y - 11 * mm, "No additional notes.")

    down(comm_content_h)

    # ── Footer ────────────────────────────────────────────────────────────
    draw_footer()
    c.save()
    buffer.seek(0)
    return buffer

def _clean_json_payload(raw_text: str):
    payload = raw_text.strip()
    if "```" in payload and "{" in payload and "}" in payload:
        payload = payload[payload.find("{") : payload.rfind("}") + 1]
    return json.loads(payload)


def _normalise_data(data: dict):
    supplier = (data.get("supplier") or "").strip()

    data["supplier"] = supplier
    data["account_name"] = supplier if supplier and supplier in BROKERS else ""
    data["supplier_found"] = bool(supplier and supplier in BROKERS)
    data["supplier_address"] = BROKERS.get(supplier, "")
    data["document_type"] = data.get("document_type") or "Consignment Note"

    # Normalise line_items
    line_items = data.get("line_items") or []
    for item in line_items:
        item["description"] = str(item.get("description") or "")
        item["movement_type"] = str(item.get("movement_type") or "")
        item["container"] = str(item.get("container") or "Magnum")
        item["waste_stream"] = str(item.get("waste_stream") or "")
        try:
            item["quantity"] = max(1, int(item.get("quantity", 1)))
        except (TypeError, ValueError):
            item["quantity"] = 1
        # Support both old format (quantity/unit_price/line_total) and new format (price)
        if "price" in item:
            try:
                item["price"] = float(item.get("price") or 0)
            except (TypeError, ValueError):
                item["price"] = 0.0
        else:
            # Convert from old format
            try:
                unit_price = float(item.get("unit_price") or 0)
            except (TypeError, ValueError):
                unit_price = 0.0
            try:
                line_total = float(item.get("line_total") or unit_price)
            except (TypeError, ValueError):
                line_total = unit_price
            item["price"] = line_total if line_total else unit_price

    try:
        overall_total = float(data.get("overall_total") or sum(i["price"] for i in line_items))
    except (TypeError, ValueError):
        overall_total = sum(i["price"] for i in line_items)

    ordered_fields = [
        "account_name",
        "supplier",
        "purchase_order_number",
        "service_description",
        "customer_name",
        "sic_code",
        "site_contact",
        "site_contact_number",
        "site_contact_email",
        "secondary_site_contact",
        "secondary_site_contact_number",
        "secondary_site_contact_email",
        "site_name",
        "site_address",
        "site_postcode",
        "opening_times",
        "access",
        "site_restrictions",
        "special_instructions",
        "document_type",
        "supplier_address",
        "supplier_found",
    ]
    result = {key: data.get(key) for key in ordered_fields}
    result["line_items"] = line_items
    result["overall_total"] = overall_total
    return result




@app.route("/download-review-pdf", methods=["POST"])
def download_review_pdf():
    payload = request.get_json(silent=True) or {}
    account_name = (payload.get("account_name") or "").strip()
    if account_name not in BROKERS:
        return jsonify({"error": "Please choose supplier"}), 400

    payload["supplier_address"] = payload.get("supplier_address") or BROKERS.get(account_name, "")

    try:
        pdf_buffer = _build_review_pdf(payload)
    except Exception as exc:
        return jsonify({"error": f"PDF generation failed: {exc}"}), 500

    # Filename: use service description as the core name
    service_desc = (payload.get("service_description") or "").strip()
    safe_account = _sanitise_filename(account_name) or "Unknown"
    safe_postcode = _sanitise_filename((payload.get("site_postcode") or "").strip()) or "Unknown"
    if service_desc:
        safe_service = _sanitise_filename(service_desc)
        filename = f"{safe_account} - {safe_service} - {safe_postcode}.pdf"
    else:
        filename = f"{safe_account} - {safe_postcode}.pdf"

    return send_file(
        pdf_buffer,
        as_attachment=True,
        download_name=filename,
        mimetype="application/pdf",
    )


@app.route("/save-review", methods=["POST"])
def save_review():
    payload = request.get_json(silent=True) or {}
    account_name = (payload.get("account_name") or "").strip()
    if account_name not in BROKERS:
        return jsonify({"error": "Please choose supplier"}), 400

    payload["supplier_address"] = payload.get("supplier_address") or BROKERS.get(account_name, "")

    LAST_REVIEW_PAYLOAD.clear()
    LAST_REVIEW_PAYLOAD.update(payload)
    return jsonify({"success": True, "message": "Review saved.", "data": payload})


@app.route("/")
def index():
    brokers = [{"name": name, "address": address} for name, address in BROKERS.items()]
    return render_template("index.html", brokers_json=json.dumps(brokers))


@app.route("/extract", methods=["POST"])
def extract():
    if "pdf" not in request.files:
        return jsonify({"error": "No PDF uploaded"}), 400

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 500

    pdf_bytes = request.files["pdf"].read()
    b64_pdf = base64.standard_b64encode(pdf_bytes).decode()
    client = anthropic.Anthropic(api_key=api_key)

    is_training = request.form.get("training") == "true"
    training_supplier = request.form.get("training_supplier", "")

    try:
        templates = _load_templates()

        # Build prompt — include template hints when available
        prompt = EXTRACT_PROMPT
        if not is_training and templates:
            hints = _build_all_template_hints(templates)
            if hints:
                prompt += "\n\n" + hints

        resp = client.messages.create(
            model="claude-opus-4-1",
            max_tokens=1800,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": b64_pdf,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )

        parsed = _clean_json_payload(resp.content[0].text)
        normalised = _normalise_data(parsed)

        # Cache PDF for training if applicable
        if is_training and training_supplier:
            TRAINING_PDF_CACHE[training_supplier] = b64_pdf

        # Determine template status for the extracted supplier
        supplier = normalised.get("supplier", "")
        template = templates.get(supplier)
        confidence = _calculate_confidence(normalised, template)

        return jsonify({
            "success": True,
            "data": normalised,
            "confidence": confidence,
            "template_used": template is not None,
            "supplier_trained": bool(supplier and supplier in templates),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ─── CEF Purchase Order Extraction ───────────────────────────────────────────

CEF_EXTRACT_PROMPT = """\
You are extracting data from a C.E.F. (City Electrical Factors) purchase order PDF.

All CEF POs follow a consistent format. Extract the following fields and return ONLY valid JSON.

EXTRACTION RULES:

1. PO Reference: Always in the top-right area of the PO (e.g. "AIR/135200", "HRG/121460"). This is the purchase order reference number.

2. CEF Branch (Supplier/Customer): The branch details are at the top of the PO. Examples:
   - "C.E.F. (Airdrie), Unit 3, Block 2, Victoria Industrial Estate, Airdrie, Lanarkshire, ML6 9BY"
   - "C.E.F. (Harrogate), Unit A, Claro Way, Harrogate, North Yorkshire, HG1 4DE"
   Extract the full branch name (e.g. "C.E.F. (Airdrie)"), full address, phone number, and email if present.

3. Delivery Address — IMPORTANT, follow these rules strictly:
   - If no "Delivery Details" section exists at all on the PO → set delivery_address and delivery_postcode to null (the system will use the CEF branch address automatically).
   - If the "Delivery Details" section says "AS PER CEF", "AS PER CEF [BRANCH NAME]", "AS ABOVE", or contains text that refers back to the CEF branch rather than providing a distinct address → set delivery_address and delivery_postcode to null.
   - ONLY set delivery_address and delivery_postcode to non-null values if the "Delivery Details" section contains a genuinely different address that is NOT the CEF branch address from the top of the PO.
   - When in doubt, set them to null — the system will fall back to the branch address.

4. Line Items / Products & Services: From the table in the middle of the PO:
   - Extract: Qty, Item code, Description, Cost, Per unit, Required date, Goods total
   - Each line item (e.g. "WEEE Green Empty & Replace", "Lamp Green Steel Empty & Replace", "Ea Consignment Note")
   - Use the description and goods total for each line.
   - For each line item, also determine the container type and waste stream (WEEE category).

5. Entered By: The person who entered/created the PO (e.g. "Lewis Grant", "George Hall"). Usually near top or bottom.

6. Total: The Goods Total at the bottom of the PO (e.g. "£504.64", "£977.43").

7. Date: The PO date, usually in the top-right area near the PO reference.

8. Contact Name on PO: Some POs have a contact name at the bottom of the line items area or in delivery details (e.g. "katie", "KATIE WOOTON"). Extract this as the site contact.

Return ONLY valid JSON with this shape:
{
  "po_reference": "PO reference number (e.g. AIR/135200), or null",
  "cef_branch_name": "Full CEF branch name (e.g. C.E.F. (Airdrie)), or null",
  "cef_branch_address": "Full branch address excluding postcode, newline separated, or null",
  "cef_branch_postcode": "Branch postcode, or null",
  "cef_branch_phone": "Branch phone number, or null",
  "cef_branch_email": "Branch email, or null",
  "delivery_address": "Delivery address ONLY if genuinely different from the CEF branch address, or null. If delivery says 'AS PER CEF' or similar, use null.",
  "delivery_postcode": "Delivery postcode ONLY if genuinely different from branch postcode, or null. If delivery says 'AS PER CEF' or similar, use null.",
  "entered_by": "Person who entered/created the PO, or null",
  "po_date": "PO date as found on document, or null",
  "site_contact": "Contact name found on PO (e.g. from delivery details or bottom of items), or null",
  "line_items": [
    {
      "description": "Product or service description. Max 8 words.",
      "container": "Container type: Green Steel, Magnum, Collapsible, Pallet, Non-Linear Crate, Durapipe, or Battery POD. Default Magnum.",
      "waste_stream": "WEEE category from the 15 categories below, or null",
      "quantity": 1,
      "item_code": "Item/product code if present, or null",
      "unit_price": 0.00,
      "line_total": 0.00
    }
  ],
  "overall_total": 0.00
}

CONTAINER AUTO-DETECTION (for each line item):
- "Green Steel" → "Green Steel"
- "Magnum" → "Magnum"
- "Collapsible" → "Collapsible"
- "Pallet" → "Pallet"
- "Non-Linear" or "Crate" → "Non-Linear Crate"
- "Dura" or "Durapipe" or "Flo Tube" → "Durapipe"
- "Battery" or "POD" → "Battery POD"
- Default → "Magnum"

SMART LINE SPLITTING — MULTIPLE CONTAINERS:
If a product description references multiple container types from this list [Green Steel, Magnum, Collapsible, Pallet, Non-Linear Crate, Durapipe, Battery POD], create a SEPARATE line item for each container. Extract the quantity for each container if mentioned.

Examples:
- "Magnum & Green Steel Empty & Replace" → TWO line items:
  Line 1: container "Magnum", quantity 1, description "Empty & Replace"
  Line 2: container "Green Steel", quantity 1, description "Empty & Replace"
- "2x Magnum, 1x Collapsible - Mixed WEEE Collection" → TWO line items:
  Line 1: container "Magnum", quantity 2, description "Mixed WEEE Collection"
  Line 2: container "Collapsible", quantity 1, description "Mixed WEEE Collection"
- If only one container is mentioned, keep it as a single line item as normal.

QUANTITY EXTRACTION:
For each line item, extract the quantity from the PO text. Look for patterns like:
- "2x", "x2", "2 x", "qty 2", "quantity: 2", "qty: 2"
- A number immediately before the container name (e.g. "2 Magnum")
- If no quantity is found, default to 1.

WASTE STREAM — pick from the 15 WEEE categories:
1. Large Household Appliances | 2. Small Household Appliances | 3. IT and Telecommunications Equipment
4. Consumer Equipment | 5. Lighting Equipment | 6. Electrical and Electronic Tools
7. Toys, Leisure and Sports Equipment | 8. Medical Devices | 9. Monitoring and Control Equipment
10. Automatic Dispensers | 11. Display Equipment | 12. Appliances Containing Refrigerants
13. Gas Discharge Lamps and LED Light Sources | 14. PV Panels (Solar Panels) | 15. Vapes and Electronic Cigarettes

Hints: "Lamp/Fluorescent/LED"→13, "WEEE/Mixed WEEE"→2, "Fridge/Refrigerant"→12, "Screen/Monitor/TV"→11,
"Battery"→2, "IT/Computer"→3, "Vape/E-Cig"→15, "Solar/PV"→14, "Consignment/Waste Transfer Note"→null.
Use your knowledge for best match. If no match → null.

RULES:
- Use null when a value is genuinely not found.
- Use numeric types for quantity, unit_price, line_total, overall_total.
- Extract ALL line items from the products/services table.
- overall_total is the goods total at the bottom of the PO.
- Return JSON only. No markdown. No explanation.
"""


def _normalise_cef_data(parsed):
    """Normalise CEF extraction response into a consistent shape."""
    result = {}
    for key in [
        "po_reference", "cef_branch_name", "cef_branch_address",
        "cef_branch_postcode", "cef_branch_phone", "cef_branch_email",
        "delivery_address", "delivery_postcode",
        "entered_by", "po_date", "site_contact",
    ]:
        val = parsed.get(key)
        result[key] = str(val).strip() if val is not None else None

    # Delivery address: treat "AS PER CEF …" / "AS ABOVE" as null (same as branch)
    _del_addr = result.get("delivery_address") or ""
    if re.search(r"(?i)\b(as\s+per\s+cef|as\s+above)\b", _del_addr):
        result["delivery_address"] = None
        result["delivery_postcode"] = None

    # Line items
    raw_items = parsed.get("line_items") or []
    line_items = []
    for item in raw_items:
        if isinstance(item, dict):
            li = {
                "description": str(item.get("description") or "").strip(),
                "container": str(item.get("container") or "Magnum"),
                "waste_stream": str(item.get("waste_stream") or ""),
                "quantity": item.get("quantity", 1),
                "item_code": item.get("item_code"),
                "unit_price": float(item.get("unit_price") or 0),
                "line_total": float(item.get("line_total") or 0),
            }
            # Use line_total as price for frontend compatibility
            li["price"] = li["line_total"] if li["line_total"] else li["unit_price"]
            line_items.append(li)

    overall_total = parsed.get("overall_total")
    if overall_total is not None:
        try:
            overall_total = float(overall_total)
        except (ValueError, TypeError):
            overall_total = 0.0
    else:
        overall_total = sum(li.get("price", 0) for li in line_items)

    result["line_items"] = line_items
    result["overall_total"] = overall_total
    return result


def _calculate_cef_confidence(data):
    """Calculate per-field confidence for CEF extraction (no templates needed)."""
    confidence = {}
    fields = [
        "po_reference", "cef_branch_name", "cef_branch_address",
        "cef_branch_postcode", "cef_branch_phone", "cef_branch_email",
        "delivery_address", "delivery_postcode",
        "entered_by", "po_date", "site_contact",
    ]
    for field in fields:
        value = data.get(field)
        has_value = value is not None and str(value).strip() != ""
        # CEF POs follow a consistent format so confidence is high when found
        confidence[field] = "high" if has_value else "low"

    line_items = data.get("line_items", [])
    confidence["line_items"] = "high" if line_items else "low"
    confidence["overall_total"] = confidence["line_items"]
    return confidence


@app.route("/extract-cef", methods=["POST"])
def extract_cef():
    """Extract data from a CEF purchase order PDF."""
    if "pdf" not in request.files:
        return jsonify({"error": "No PDF uploaded"}), 400

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 500

    pdf_bytes = request.files["pdf"].read()
    b64_pdf = base64.standard_b64encode(pdf_bytes).decode()
    client = anthropic.Anthropic(api_key=api_key)

    try:
        resp = client.messages.create(
            model="claude-opus-4-1",
            max_tokens=1800,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": b64_pdf,
                            },
                        },
                        {"type": "text", "text": CEF_EXTRACT_PROMPT},
                    ],
                }
            ],
        )

        parsed = _clean_json_payload(resp.content[0].text)
        normalised = _normalise_cef_data(parsed)
        confidence = _calculate_cef_confidence(normalised)

        return jsonify({
            "success": True,
            "data": normalised,
            "confidence": confidence,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/download-cef-review-pdf", methods=["POST"])
def download_cef_review_pdf():
    """Generate a branded PDF for a CEF purchase order review."""
    payload = request.get_json(silent=True) or {}

    # Map CEF fields into the standard payload format for PDF generation
    mapped = {
        "account_name": payload.get("cef_branch_name") or "C.E.F.",
        "supplier_address": payload.get("cef_branch_address") or "",
        "purchase_order_number": payload.get("po_reference") or "",
        "service_description": payload.get("service_description") or "",
        "document_type": payload.get("document_type") or "",
        "customer_name": payload.get("cef_branch_name") or "",
        "sic_code": "",
        "site_contact": payload.get("site_contact") or "",
        "site_contact_number": payload.get("cef_branch_phone") or "",
        "site_contact_email": payload.get("cef_branch_email") or "",
        "secondary_site_contact": "",
        "secondary_site_contact_number": "",
        "secondary_site_contact_email": "",
        "site_name": payload.get("cef_branch_name") or "",
        "site_address": payload.get("delivery_address") or payload.get("cef_branch_address") or "",
        "site_postcode": payload.get("delivery_postcode") or payload.get("cef_branch_postcode") or "",
        "opening_times": "",
        "access": "",
        "site_restrictions": "",
        "special_instructions": payload.get("special_instructions") or "",
        "line_items": payload.get("line_items") or [],
        "overall_total": payload.get("overall_total") or 0,
        "person_name": payload.get("person_name") or "",
        "person_title": payload.get("person_title") or "",
        "person_email": payload.get("person_email") or "",
        "person_phone": payload.get("person_phone") or "",
    }

    try:
        pdf_buffer = _build_review_pdf(mapped)
    except Exception as exc:
        return jsonify({"error": f"PDF generation failed: {exc}"}), 500

    po_ref = _sanitise_filename(mapped["purchase_order_number"]) or "Unknown"
    branch = _sanitise_filename(mapped["account_name"]) or "CEF"
    postcode = _sanitise_filename(mapped["site_postcode"]) or "Unknown"
    filename = f"{branch} - {po_ref} - {postcode}.pdf"

    return send_file(
        pdf_buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/api/send-cef-delivery-email", methods=["POST"])
def send_cef_delivery_email():
    """Send a CEF delivery email via Microsoft Graph API."""
    body = request.get_json(silent=True) or {}
    to_email = (body.get("to_email") or "").strip()
    payload = body.get("payload") or {}
    person = body.get("person") or "Katie Wooton"

    if not to_email:
        return jsonify({"error": "No recipient email provided"}), 400

    # Map CEF fields into standard payload format for email/PDF
    mapped = {
        "account_name": payload.get("cef_branch_name") or "C.E.F.",
        "supplier_address": payload.get("cef_branch_address") or "",
        "purchase_order_number": payload.get("po_reference") or "",
        "service_description": payload.get("service_description") or "",
        "document_type": payload.get("document_type") or "",
        "customer_name": payload.get("cef_branch_name") or "",
        "sic_code": "",
        "site_contact": payload.get("site_contact") or "",
        "site_contact_number": payload.get("cef_branch_phone") or "",
        "site_contact_email": payload.get("cef_branch_email") or "",
        "secondary_site_contact": "",
        "secondary_site_contact_number": "",
        "secondary_site_contact_email": "",
        "site_name": payload.get("cef_branch_name") or "",
        "site_address": payload.get("delivery_address") or payload.get("cef_branch_address") or "",
        "site_postcode": payload.get("delivery_postcode") or payload.get("cef_branch_postcode") or "",
        "opening_times": "",
        "access": "",
        "site_restrictions": "",
        "special_instructions": payload.get("special_instructions") or "",
        "line_items": payload.get("line_items") or [],
        "overall_total": payload.get("overall_total") or 0,
        "person": person,
        "person_name": payload.get("person_name") or "",
        "person_title": payload.get("person_title") or "",
        "person_email": payload.get("person_email") or "",
        "person_phone": payload.get("person_phone") or "",
        "po_date": payload.get("po_date") or "",
    }

    # Generate the PDF
    try:
        pdf_buffer = _build_review_pdf(mapped)
    except Exception as exc:
        return jsonify({"error": f"PDF generation failed: {exc}"}), 500

    po_number = mapped["purchase_order_number"] or "Unknown"
    supplier_name = "Waste Experts"

    pdf_bytes = pdf_buffer.getvalue()

    safe_po = _sanitise_filename(po_number)
    attachment_filename = f"CEF_PO_{safe_po}.pdf"

    email_html = _build_delivery_email_html(mapped)

    subject = f"CEF Purchase Order \u2014 {po_number} \u2014 {supplier_name}"

    try:
        ok, err = _send_email_via_graph(to_email, subject, email_html, attachment_filename, pdf_bytes)
        if ok:
            return jsonify({"success": True}), 200
        else:
            return jsonify({"error": err or "Failed to send email"}), 500
    except Exception as exc:
        return jsonify({"error": f"Failed to send email: {exc}"}), 500


@app.route("/brokers", methods=["GET"])
def get_brokers():
    return jsonify({"brokers": [{"name": name, "address": addr} for name, addr in BROKERS.items()]})


@app.route("/broker-address", methods=["GET"])
def broker_address():
    name = request.args.get("name", "")
    return jsonify({"name": name, "address": BROKERS.get(name, "")})


# ─── Supplier template management ─────────────────────────────────────────────


@app.route("/api/templates", methods=["GET"])
def get_templates():
    """Return all suppliers with their template training status."""
    templates = _load_templates()
    suppliers = []
    for name in BROKERS:
        tmpl = templates.get(name)
        suppliers.append({
            "name": name,
            "trained": tmpl is not None,
            "trained_date": tmpl.get("trained_date") if tmpl else None,
        })
    trained_count = sum(1 for s in suppliers if s["trained"])
    return jsonify({
        "suppliers": suppliers,
        "trained_count": trained_count,
        "total_count": len(suppliers),
    })


@app.route("/api/templates/save", methods=["POST"])
def save_template():
    """Save a supplier template with corrected data and AI layout hints."""
    payload = request.get_json(silent=True) or {}
    supplier = (payload.get("supplier") or "").strip()
    if not supplier or supplier not in BROKERS:
        return jsonify({"error": "Invalid supplier name"}), 400

    original_data = payload.get("original_data", {})
    corrected_data = payload.get("corrected_data", {})

    tracked_fields = [
        "purchase_order_number", "service_description",
        "site_contact", "site_contact_number", "site_contact_email",
        "secondary_site_contact", "secondary_site_contact_number",
        "secondary_site_contact_email",
        "site_name", "site_address", "site_postcode",
        "opening_times", "access", "site_restrictions",
        "special_instructions", "document_type",
    ]

    auto_extracted = []
    manually_corrected = []
    for field in tracked_fields:
        orig_val = str(original_data.get(field) or "").strip()
        corr_val = str(corrected_data.get(field) or "").strip()
        if corr_val:
            if orig_val == corr_val:
                auto_extracted.append(field)
            else:
                manually_corrected.append(field)

    # Generate layout description using cached training PDF
    layout_description = ""
    field_locations = {}

    b64_pdf = TRAINING_PDF_CACHE.pop(supplier, "")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if api_key and b64_pdf:
        try:
            client = anthropic.Anthropic(api_key=api_key)
            layout_prompt = (
                f"Analyze this PDF from {supplier} and describe the layout "
                "for future extraction.\n\n"
                f"The correct extracted data is:\n"
                f"{json.dumps(corrected_data, indent=2)}\n\n"
                "For each non-null field, describe WHERE in the PDF it was found. "
                "Return JSON:\n"
                "{\n"
                '  "layout_description": "Brief overall layout (e.g. \'PO number '
                "top-right, line items in center table, total at bottom')\",\n"
                '  "field_locations": {\n'
                '    "purchase_order_number": "location description",\n'
                '    "site_contact": "location description"\n'
                "  }\n"
                "}\n\n"
                "Be specific about positions (top-left, top-right, center, bottom) "
                "and nearby labels.\nJSON only. No markdown."
            )
            layout_resp = client.messages.create(
                model="claude-opus-4-1",
                max_tokens=800,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": b64_pdf,
                            },
                        },
                        {"type": "text", "text": layout_prompt},
                    ],
                }],
            )
            layout_data = _clean_json_payload(layout_resp.content[0].text)
            layout_description = layout_data.get("layout_description", "")
            field_locations = layout_data.get("field_locations", {})
        except Exception:
            pass

    templates = _load_templates()
    templates[supplier] = {
        "trained_date": datetime.now().isoformat(),
        "layout_description": layout_description,
        "field_locations": field_locations,
        "auto_extracted_fields": auto_extracted,
        "manually_corrected_fields": manually_corrected,
    }
    _save_templates(templates)

    return jsonify({"success": True, "supplier": supplier})


@app.route("/api/templates", methods=["POST"])
def create_template():
    """Create a new supplier template (alias for /api/templates/save)."""
    return save_template()


@app.route("/api/templates/<path:supplier>", methods=["PUT"])
def update_template(supplier):
    """Update an existing supplier template."""
    payload = request.get_json(silent=True) or {}
    if not supplier or supplier not in BROKERS:
        return jsonify({"error": "Invalid supplier name"}), 400

    templates = _load_templates()
    if supplier not in templates:
        return jsonify({"error": "Template not found — use POST to create"}), 404

    # Merge provided fields into existing template
    existing = templates[supplier]
    for key in ("layout_description", "field_locations",
                "auto_extracted_fields", "manually_corrected_fields"):
        if key in payload:
            existing[key] = payload[key]
    existing["trained_date"] = datetime.now().isoformat()
    _save_templates(templates)
    return jsonify({"success": True, "supplier": supplier})


@app.route("/api/templates/<path:supplier>", methods=["DELETE"])
def delete_template(supplier):
    """Delete a supplier template."""
    templates = _load_templates()
    if supplier in templates:
        del templates[supplier]
        _save_templates(templates)
    return jsonify({"success": True})


# ─── Make.com Webhook integration ────────────────────────────────────────────

MAKE_WEBHOOK_URL = os.environ.get("MAKE_WEBHOOK_URL") or "https://hook.eu1.make.com/79cktukjwpsyscc507c6p1nddb61pydo"


@app.route("/api/send-to-webhook", methods=["POST"])
def send_to_webhook():
    """Forward extracted PO data to Make.com webhook."""
    body = request.get_json(silent=True) or {}
    data = body.get("data") or {}
    pdf_filename = body.get("pdf_filename") or "\u2014"

    reference = str(data.get("purchase_order_number") or "").strip()
    if not reference:
        return jsonify({"error": "No PO reference number"}), 400

    # Build the 11-field payload
    date_extracted = date.today().strftime("%d/%m/%Y")
    title_desc = str(data.get("service_description") or "").strip() or "\u2014"
    # Use person details from data if available, otherwise fall back to default
    prepared_by = str(data.get("person_name") or "").strip() or PREPARED_BY["name"]
    customer_company = str(data.get("account_name") or data.get("supplier") or "").strip() or "\u2014"

    supplier_addr = str(data.get("supplier_address") or "").strip()
    customer_address = supplier_addr.replace("\n", ", ") if supplier_addr else "\u2014"

    customer_email = str(data.get("site_contact_email") or "").strip() or "\u2014"

    # Inject £40 doc-type line if applicable
    wh_line_items = list(data.get("line_items") or [])
    _inject_doc_type_line(wh_line_items, data.get("document_type"))
    line_items_cell = _format_line_items_cell(wh_line_items)

    # Recalculate total to include injected line
    try:
        total_amount = f"\u00a3{sum(float(it.get('price', 0)) for it in wh_line_items):.2f}"
    except (TypeError, ValueError):
        total_amount = "\u00a30.00"

    caveats = str(data.get("special_instructions") or "").strip() or "\u2014"

    webhook_payload = {
        "date_extracted": date_extracted,
        "pdf_filename": pdf_filename,
        "title": title_desc,
        "reference": reference,
        "prepared_by": prepared_by,
        "prepared_by_title": str(data.get("person_title") or "").strip() or "\u2014",
        "prepared_by_email": str(data.get("person_email") or "").strip() or "\u2014",
        "prepared_by_phone": str(data.get("person_phone") or "").strip() or "\u2014",
        "po_date": str(data.get("po_date") or "").strip() or "\u2014",
        "customer_company": customer_company,
        "customer_address": customer_address,
        "customer_email": customer_email,
        "line_items": line_items_cell,
        "total_amount": total_amount,
        "caveats": caveats,
        # Customer / site detail fields
        "customer_name": str(data.get("customer_name") or data.get("account_name") or "").strip() or "\u2014",
        "site_contact": str(data.get("site_contact") or "").strip() or "\u2014",
        "site_contact_number": str(data.get("site_contact_number") or "").strip() or "\u2014",
        "site_contact_email": str(data.get("site_contact_email") or "").strip() or "\u2014",
        "secondary_site_contact": str(data.get("secondary_site_contact") or "").strip() or "\u2014",
        "secondary_site_contact_number": str(data.get("secondary_site_contact_number") or "").strip() or "\u2014",
        "secondary_site_contact_email": str(data.get("secondary_site_contact_email") or "").strip() or "\u2014",
        "site_name": str(data.get("site_name") or "").strip() or "\u2014",
        "site_address": str(data.get("site_address") or "").strip() or "\u2014",
        "site_postcode": str(data.get("site_postcode") or "").strip() or "\u2014",
        "opening_times": str(data.get("opening_times") or "").strip() or "\u2014",
        "access_details": str(data.get("access") or "").strip() or "\u2014",
        "site_restrictions": str(data.get("site_restrictions") or "").strip() or "\u2014",
        "special_instructions": str(data.get("special_instructions") or "").strip() or "\u2014",
    }

    try:
        resp = req_lib.post(
            MAKE_WEBHOOK_URL,
            headers={"Content-Type": "application/json"},
            json=webhook_payload,
            timeout=30,
        )
        if resp.ok:
            return jsonify({"success": True, "message": "Sent to WasteLogics"}), 200
        else:
            return jsonify({"error": f"Webhook returned {resp.status_code}: {resp.text[:200]}"}), 502
    except Exception as exc:
        return jsonify({"error": f"Webhook request failed: {exc}"}), 500


# ─── Google Sheets integration ────────────────────────────────────────────────

GOOGLE_SHEET_ID = "1xSK6hGLYd9jVbCtU7NVtOlWJLED3_-sB1LdsUyPlKHU"
_gs_client = None


def _get_gsheets_client():
    """Lazily initialise and return a gspread client, or None."""
    global _gs_client
    if _gs_client is not None:
        return _gs_client
    if not _gspread_available:
        return None
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
    if not creds_json:
        return None
    try:
        info = json.loads(creds_json)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceCredentials.from_service_account_info(info, scopes=scopes)
        _gs_client = gspread.authorize(creds)
        return _gs_client
    except Exception as exc:
        print(f"[Google Sheets] Failed to initialise credentials: {exc}")
        return None


def _format_line_items_cell(line_items):
    """Format line items into a single cell string with newlines.
    Uses two-line format: WEEE category on first line, product description on second."""
    lines = []
    for item in (line_items or []):
        container = str(item.get("container") or "")
        qty = item.get("quantity", 1)
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            qty = 1
        weee_line, desc_line = _waste_stream_parts(item)
        mt = str(item.get("movement_type") or "")
        try:
            price = float(item.get("price") or item.get("line_total") or item.get("unit_price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        mt_str = f" [{mt}]" if mt else ""
        container_str = f"{container} | " if container else ""
        is_doc_line = item.get("_is_doc_type_line", False)
        qty_str = "" if is_doc_line else (f"x{qty} | " if qty and qty > 1 else "")
        if weee_line and desc_line:
            lines.append(f"{container_str}{qty_str}{weee_line}\n  {desc_line}{mt_str} | \u00a3{price:.2f}")
        else:
            display_desc = weee_line or desc_line or _format_waste_stream_display(item)
            lines.append(f"{container_str}{qty_str}{display_desc}{mt_str} | \u00a3{price:.2f}")
    return "\n".join(lines) if lines else "\u2014"


@app.route("/api/save-to-sheets", methods=["POST"])
def save_to_sheets():
    """Push extracted PO data to Google Sheets."""
    client = _get_gsheets_client()
    if client is None:
        msg = "Google Sheets not configured"
        if not _gspread_available:
            msg += " \u2014 gspread package not installed"
        elif not os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip():
            msg += " \u2014 GOOGLE_CREDENTIALS_JSON not set"
        return jsonify({"error": msg}), 500

    body = request.get_json(silent=True) or {}
    data = body.get("data") or {}
    pdf_filename = body.get("pdf_filename") or "\u2014"

    reference = str(data.get("purchase_order_number") or "").strip()
    if not reference:
        return jsonify({"error": "No PO reference number to log"}), 400

    try:
        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1

        # Duplicate prevention: check if reference already exists in column D
        existing_refs = sheet.col_values(4)  # Column D = Reference
        if reference in existing_refs:
            return jsonify({"duplicate": True, "message": "This PO is already in Google Sheets"}), 200

        # Build row (11 columns A-K)
        date_extracted = date.today().strftime("%d/%m/%Y")
        title_desc = str(data.get("service_description") or "").strip() or "\u2014"
        prepared_by = PREPARED_BY["name"]
        customer_company = str(data.get("account_name") or data.get("supplier") or "").strip() or "\u2014"

        # Bill-to address (supplier_address), not site address
        supplier_addr = str(data.get("supplier_address") or "").strip()
        customer_address = supplier_addr.replace("\n", ", ") if supplier_addr else "\u2014"

        customer_email = str(data.get("site_contact_email") or "").strip() or "\u2014"

        # Inject £40 doc-type line if applicable
        gs_line_items = list(data.get("line_items") or [])
        _inject_doc_type_line(gs_line_items, data.get("document_type"))
        line_items_cell = _format_line_items_cell(gs_line_items)

        try:
            total_amount = f"\u00a3{sum(float(it.get('price', 0)) for it in gs_line_items):.2f}"
        except (TypeError, ValueError):
            total_amount = "\u00a30.00"

        caveats = str(data.get("special_instructions") or "").strip() or "\u2014"

        row = [
            date_extracted,       # A — Date Extracted
            pdf_filename,         # B — PDF Filename
            title_desc,           # C — Title/Description
            "'" + reference,      # D — Reference (prefixed with ' to force text in Sheets)
            prepared_by,          # E — Prepared By
            customer_company,     # F — Customer Company
            customer_address,     # G — Customer Address
            customer_email,       # H — Customer Email
            line_items_cell,      # I — Line Items
            total_amount,         # J — Total Amount
            caveats,              # K — Caveats/Comments
        ]

        sheet.append_row(row, value_input_option="USER_ENTERED")
        return jsonify({"success": True, "message": "Saved to Google Sheets"}), 200

    except Exception as exc:
        print(f"[Google Sheets] Error saving row: {exc}")
        return jsonify({"error": f"Google Sheets error: {exc}"}), 500


# Check credentials at startup
if not os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip():
    print("[Google Sheets] WARNING: GOOGLE_CREDENTIALS_JSON not set — Google Sheets logging disabled")
elif not _gspread_available:
    print("[Google Sheets] WARNING: gspread/google-auth not installed — Google Sheets logging disabled")
else:
    print("[Google Sheets] Credentials found — logging enabled")


# ─── HubSpot integration ─────────────────────────────────────────────────────

HUBSPOT_TOKEN = os.environ.get("HUBSPOT_TOKEN", "")
HUBSPOT_PORTAL_ID = os.environ.get("HUBSPOT_PORTAL_ID", "26464920")
HUBSPOT_BASE = "https://api.hubapi.com/crm/v3"
HUBSPOT_APP_BASE = "https://app-eu1.hubspot.com"

_cached_owner_id = None  # Cache Nathan Malone's owner ID

# Pipeline cache: resolve "Repeat Business (Waste Experts)" on first use
_REPEAT_BIZ_PIPELINE_NAME = "Repeat Business (Waste Experts)"
_cached_pipeline = {"id": None, "closedwon_stage": None}


def _resolve_pipeline():
    """Fetch deal pipelines and cache the ID + closedwon stage for 'Repeat Business (Waste Experts)'."""
    if _cached_pipeline["id"]:
        return _cached_pipeline["id"], _cached_pipeline["closedwon_stage"]

    status, data = _hs_request("GET", "/pipelines/deals")
    if status != 200:
        return None, None

    for pipe in data.get("results", []):
        if pipe.get("label", "").strip() == _REPEAT_BIZ_PIPELINE_NAME:
            pipeline_id = pipe["id"]
            closedwon_stage = None
            for stage in pipe.get("stages", []):
                label_lower = stage.get("label", "").lower()
                if label_lower == "closed won" or stage.get("id") == "closedwon":
                    closedwon_stage = stage["id"]
                    break
            # If no explicit match, look for any stage with "closed" and "won"
            if not closedwon_stage:
                for stage in pipe.get("stages", []):
                    label_lower = stage.get("label", "").lower()
                    if "closed" in label_lower and "won" in label_lower:
                        closedwon_stage = stage["id"]
                        break
            # Fallback: use "closedwon" as the stage ID
            if not closedwon_stage:
                closedwon_stage = "closedwon"

            _cached_pipeline["id"] = pipeline_id
            _cached_pipeline["closedwon_stage"] = closedwon_stage
            return pipeline_id, closedwon_stage

    return None, None


def _hs_headers():
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def _hs_request(method, path, body=None):
    """Make a HubSpot API request. Returns (status_code, parsed_json)."""
    url = f"{HUBSPOT_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=_hs_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode())
        except Exception:
            err_body = {"message": str(e)}
        return e.code, err_body


@app.route("/api/hubspot/test", methods=["GET"])
def hubspot_test():
    """Quick connectivity check: fetch account info from HubSpot."""
    if not HUBSPOT_TOKEN:
        return jsonify({
            "ok": False,
            "error": "HUBSPOT_TOKEN environment variable is not set. "
                     "Set it in your hosting platform (e.g. Railway) environment variables.",
        }), 500

    # Show masked token for debugging (first 8 + last 4 chars)
    masked = HUBSPOT_TOKEN[:8] + "***" + HUBSPOT_TOKEN[-4:] if len(HUBSPOT_TOKEN) > 12 else "***"

    status, data = _hs_request("GET", "/owners?limit=1")
    return jsonify({
        "ok": status == 200,
        "status": status,
        "token_preview": masked,
        "data": data,
    }), status if status != 200 else 200


@app.route("/api/hubspot/owners", methods=["GET"])
def hubspot_owners():
    """Fetch all HubSpot owners. Also caches Nathan Malone's ID."""
    global _cached_owner_id
    status, data = _hs_request("GET", "/owners?limit=100")
    if status != 200:
        return jsonify({"error": data.get("message", "Failed to fetch owners"), "status": status}), status

    owners = []
    for o in data.get("results", []):
        first = o.get("firstName", "")
        last = o.get("lastName", "")
        name = f"{first} {last}".strip()
        oid = o.get("id")
        owners.append({
            "id": oid,
            "firstName": first,
            "lastName": last,
            "name": name,
            "email": o.get("email", ""),
        })
        if name.lower() == "nathan malone":
            _cached_owner_id = oid

    return jsonify({"owners": owners, "nathan_id": _cached_owner_id})


# ─── Individual HubSpot proxy routes ─────────────────────────────────────────

@app.route("/api/hubspot/companies/search", methods=["POST"])
def hubspot_companies_search():
    """Proxy: POST /api/hubspot/companies/search → HubSpot companies search."""
    body = request.get_json(silent=True) or {}
    status, data = _hs_request("POST", "/objects/companies/search", body)
    return jsonify(data), status


@app.route("/api/hubspot/contacts/search", methods=["POST"])
def hubspot_contacts_search():
    """Proxy: POST /api/hubspot/contacts/search → HubSpot contacts search."""
    body = request.get_json(silent=True) or {}
    status, data = _hs_request("POST", "/objects/contacts/search", body)
    return jsonify(data), status


@app.route("/api/hubspot/deals", methods=["POST"])
def hubspot_deals_create():
    """Proxy: POST /api/hubspot/deals → HubSpot create deal."""
    body = request.get_json(silent=True) or {}
    status, data = _hs_request("POST", "/objects/deals", body)
    return jsonify(data), status


@app.route("/api/hubspot/line-items", methods=["POST"])
def hubspot_line_items_create():
    """Proxy: POST /api/hubspot/line-items → HubSpot create line item."""
    body = request.get_json(silent=True) or {}
    status, data = _hs_request("POST", "/objects/line_items", body)
    return jsonify(data), status


@app.route("/api/hubspot/deals/<deal_id>/associations/<path:rest>", methods=["PUT"])
def hubspot_deal_associations(deal_id, rest):
    """Proxy: PUT /api/hubspot/deals/:dealId/associations/* → HubSpot associations."""
    body = request.get_json(silent=True)
    status, data = _hs_request("PUT", f"/objects/deals/{deal_id}/associations/{rest}", body)
    return jsonify(data), status


# ─── Orchestrated deal creation (uses proxy routes internally) ────────────────

@app.route("/api/hubspot/create-deal", methods=["POST"])
def hubspot_create_deal():
    """
    Full deal creation flow:
    1. Resolve owner ID
    2. Search for company
    3. Search for contact
    4. Create deal
    5. Associate company/contact
    6. Create line items
    """
    global _cached_owner_id
    payload = request.get_json(silent=True) or {}

    deal_name = payload.get("deal_name", "Untitled Deal")
    amount = payload.get("amount", 0)
    po_number = payload.get("po_number", "")
    supplier_name = payload.get("supplier_name", "")
    line_items = list(payload.get("line_items", []))
    description = payload.get("description", "")

    # Inject £40 doc-type line if applicable
    document_type = payload.get("document_type", "")
    _inject_doc_type_line(line_items, document_type)
    # Recalculate amount to include the injected line
    amount = sum(float(it.get("price", 0)) for it in line_items)
    delivery_details = payload.get("delivery_details", {})

    today_iso = date.today().isoformat()

    # Step 1 — Use owner ID from frontend, fall back to cached/fetched Nathan Malone
    owner_id = payload.get("owner_id") or _cached_owner_id
    if not owner_id:
        status, data = _hs_request("GET", "/owners?limit=100")
        if status == 200:
            for o in data.get("results", []):
                name = f"{o.get('firstName', '')} {o.get('lastName', '')}".strip()
                if name.lower() == "nathan malone":
                    owner_id = o.get("id")
                    _cached_owner_id = owner_id
                    break
        elif status == 401:
            return jsonify({"error": "HubSpot authentication failed — check your API token"}), 401

    # Step 2 — Search for existing company
    company_id = None
    company_name_found = None
    if supplier_name:
        search_body = {
            "filterGroups": [{
                "filters": [{
                    "propertyName": "name",
                    "operator": "CONTAINS_TOKEN",
                    "value": supplier_name.split()[0] if supplier_name.split() else supplier_name,
                }]
            }],
            "properties": ["name"],
            "limit": 10,
        }
        status, data = _hs_request("POST", "/objects/companies/search", search_body)
        if status == 200:
            supplier_lower = supplier_name.lower()
            for comp in data.get("results", []):
                comp_name = (comp.get("properties", {}).get("name") or "").lower()
                if supplier_lower in comp_name or comp_name in supplier_lower:
                    company_id = comp["id"]
                    company_name_found = comp.get("properties", {}).get("name")
                    break
            # If no fuzzy match, try the first result as a fallback
            if not company_id and data.get("results"):
                company_id = data["results"][0]["id"]
                company_name_found = data["results"][0].get("properties", {}).get("name")

    # Step 3 — Search for existing contact
    contact_id = None
    if supplier_name:
        contact_body = {
            "filterGroups": [{
                "filters": [{
                    "propertyName": "company",
                    "operator": "CONTAINS_TOKEN",
                    "value": supplier_name.split()[0] if supplier_name.split() else supplier_name,
                }]
            }],
            "properties": ["firstname", "lastname", "company"],
            "limit": 5,
        }
        status, data = _hs_request("POST", "/objects/contacts/search", contact_body)
        if status == 200 and data.get("results"):
            contact_id = data["results"][0]["id"]

    # Step 4 — Create the deal
    # Resolve the "Repeat Business (Waste Experts)" pipeline
    pipeline_id, closedwon_stage = _resolve_pipeline()
    deal_properties = {
        "dealname": deal_name,
        "createdate": today_iso,
        "closedate": today_iso,
        "amount": str(amount),
        "dealstage": closedwon_stage or "closedwon",
        "pipeline": pipeline_id or "default",
        "description": description,
    }
    if owner_id:
        deal_properties["hubspot_owner_id"] = owner_id
    if po_number:
        deal_properties["po_number"] = po_number

    status, data = _hs_request("POST", "/objects/deals", {"properties": deal_properties})

    if status == 401:
        return jsonify({"error": "HubSpot authentication failed — check your API token"}), 401
    if status == 429:
        return jsonify({"error": "HubSpot rate limit exceeded — please wait a moment and retry"}), 429

    # Check for po_number property error
    if status != 201:
        err_msg = data.get("message", "")
        if "po_number" in err_msg.lower() or "property" in err_msg.lower():
            # Retry without po_number
            deal_properties.pop("po_number", None)
            status, data = _hs_request("POST", "/objects/deals", {"properties": deal_properties})
            if status == 201:
                po_number_warning = "Custom property 'po_number' not found — create it in HubSpot under Settings → Properties → Deal properties, then retry"
            else:
                return jsonify({"error": data.get("message", "Failed to create deal"), "status": status}), status
        else:
            return jsonify({"error": data.get("message", "Failed to create deal"), "status": status}), status
    else:
        po_number_warning = None

    deal_id = data["id"]

    # Step 5 — Associate company and contact
    association_errors = []
    if company_id:
        assoc_status, assoc_data = _hs_request(
            "PUT",
            f"/objects/deals/{deal_id}/associations/companies/{company_id}/deal_to_company",
            None,
        )
        if assoc_status not in (200, 201):
            association_errors.append(f"Company association failed: {assoc_data.get('message', '')}")

    if contact_id:
        assoc_status, assoc_data = _hs_request(
            "PUT",
            f"/objects/deals/{deal_id}/associations/contacts/{contact_id}/deal_to_contact",
            None,
        )
        if assoc_status not in (200, 201):
            association_errors.append(f"Contact association failed: {assoc_data.get('message', '')}")

    # Step 6 — Create line items (two-line waste stream in name)
    line_item_ids = []
    for item in line_items:
        weee_line, desc_line = _waste_stream_parts(item)
        container = item.get("container", "")
        mt = item.get("movement_type", "")
        parts = []
        if container:
            parts.append(container)
        if weee_line and desc_line:
            parts.append(f"{weee_line} / {desc_line}")
        else:
            parts.append(weee_line or desc_line or _format_waste_stream_display(item) or "Item")
        if mt:
            parts.append(f"[{mt}]")
        li_name = " ".join(parts)
        # Use 'price' field (new format) or fall back to 'unit_price' (old format)
        item_price = item.get("price", item.get("unit_price", 0))
        item_qty = item.get("quantity", 1)
        try:
            item_qty = max(1, int(item_qty))
        except (TypeError, ValueError):
            item_qty = 1
        # Skip quantity for auto-added document type line
        li_props = {
            "name": li_name,
            "price": str(item_price),
        }
        if not item.get("_is_doc_type_line"):
            li_props["quantity"] = str(item_qty)
        else:
            li_props["quantity"] = "1"
        li_body = {"properties": li_props}

        li_status, li_data = _hs_request("POST", "/objects/line_items", li_body)
        if li_status in (200, 201):
            li_id = li_data["id"]
            line_item_ids.append(li_id)
            # Associate line item to deal
            _hs_request(
                "PUT",
                f"/objects/deals/{deal_id}/associations/line_items/{li_id}/deal_to_line_item",
                None,
            )

    result = {
        "success": True,
        "deal_id": deal_id,
        "deal_url": f"{HUBSPOT_APP_BASE}/contacts/{HUBSPOT_PORTAL_ID}/deal/{deal_id}",
        "company_found": company_name_found,
        "company_id": company_id,
        "contact_id": contact_id,
        "line_items_created": len(line_item_ids),
        "owner_id": owner_id,
    }
    if po_number_warning:
        result["po_number_warning"] = po_number_warning
    if association_errors:
        result["association_warnings"] = association_errors

    return jsonify(result), 201


# ─── Customer Delivery Email (via Microsoft Graph API) ───────────────────────

MS_TENANT_ID = os.environ.get("MS_TENANT_ID", "").strip()
MS_CLIENT_ID = os.environ.get("MS_CLIENT_ID", "").strip()
MS_CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "").strip()
MS_SENDER_EMAIL = os.environ.get("MS_SENDER_EMAIL", "orders@wasteexperts.co.uk").strip()

# Token cache
_ms_token_cache = {"token": None, "expires_at": 0}


def _get_ms_token():
    """Obtain an OAuth2 token using client credentials flow, with caching."""
    import time
    now = time.time()
    if _ms_token_cache["token"] and _ms_token_cache["expires_at"] > now + 60:
        return _ms_token_cache["token"]

    if not all([MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET]):
        return None

    resp = req_lib.post(
        f"https://login.microsoftonline.com/{MS_TENANT_ID}/oauth2/v2.0/token",
        data={
            "client_id": MS_CLIENT_ID,
            "client_secret": MS_CLIENT_SECRET,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"[MS Graph] Token fetch failed: {resp.status_code} {resp.text[:200]}")
        return None

    data = resp.json()
    _ms_token_cache["token"] = data["access_token"]
    _ms_token_cache["expires_at"] = now + data.get("expires_in", 3600)
    return _ms_token_cache["token"]


def _send_email_via_graph(to_email, subject, html_body, attachment_name, attachment_bytes):
    """Send an email via Microsoft Graph API."""
    token = _get_ms_token()
    if not token:
        return None, "Email service not configured — set MS_TENANT_ID, MS_CLIENT_ID, and MS_CLIENT_SECRET"

    attachment_b64 = base64.standard_b64encode(attachment_bytes).decode()

    resp = req_lib.post(
        f"https://graph.microsoft.com/v1.0/users/{MS_SENDER_EMAIL}/sendMail",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "message": {
                "subject": subject,
                "body": {
                    "contentType": "HTML",
                    "content": html_body,
                },
                "toRecipients": [{"emailAddress": {"address": to_email}}],
                "attachments": [{
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": attachment_name,
                    "contentType": "application/pdf",
                    "contentBytes": attachment_b64,
                }],
            }
        },
        timeout=30,
    )
    if resp.ok or resp.status_code == 202:
        return True, None
    else:
        err_msg = resp.text[:300]
        print(f"[MS Graph] Send failed: {resp.status_code} {err_msg}")
        return None, f"Graph API error ({resp.status_code}): {err_msg}"


# Check MS credentials at startup
if all([MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET]):
    print(f"[MS Graph] Credentials found — email via {MS_SENDER_EMAIL}")
else:
    print("[MS Graph] WARNING: MS_TENANT_ID / MS_CLIENT_ID / MS_CLIENT_SECRET not fully set — email sending disabled")


def _esc(text):
    """HTML-escape a string."""
    return (str(text or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _build_delivery_email_html(payload):
    """Build a branded HTML email with full operations detail in a styled table."""
    def _val(key, default="\u2014"):
        v = str(payload.get(key) or "").strip()
        return v if v else default

    # CSS for the email
    row_style = (
        'style="border-bottom:1px solid #e2e8f0;"'
    )
    label_style = (
        'style="padding:10px 14px; font-weight:700; font-size:13px; '
        'color:#1e2e3d; width:40%; vertical-align:top; '
        'border-right:1px solid #e2e8f0; background:#f8fafc;"'
    )
    value_style = (
        'style="padding:10px 14px; font-size:13px; color:#2d3748; '
        'vertical-align:top;"'
    )
    section_style = (
        'style="padding:8px 14px; font-size:11px; font-weight:700; '
        'text-transform:uppercase; letter-spacing:0.5px; color:#718096; '
        'background:#edf2f7; border-bottom:1px solid #e2e8f0;"'
    )

    def row(label, value):
        v = _esc(value) if value and value != "\u2014" else "\u2014"
        # Preserve newlines in values
        v = v.replace("\n", "<br/>")
        return (
            f'<tr {row_style}>'
            f'<td {label_style}>{_esc(label)}</td>'
            f'<td {value_style}>{v}</td>'
            f'</tr>'
        )

    def section_header(title):
        return (
            f'<tr><td colspan="2" {section_style}>{_esc(title)}</td></tr>'
        )

    # Prepared By details
    prep_name = _val("person_name", "")
    prep_title = _val("person_title", "")
    prep_email = _val("person_email", "")
    prep_phone = _val("person_phone", "")

    table_rows = "".join([
        # Order Details
        section_header("Order Details"),
        row("Account Name (Waste Logics)", _val("account_name")),
        row("Supplier", "Waste Experts"),
        row("Purchase Order Number", _val("purchase_order_number")),
        row("PO Date", _val("po_date")),

        # Prepared By
        *(([
            section_header("Prepared By"),
            row("Name", prep_name),
            row("Title", prep_title),
            row("Email", prep_email),
            row("Phone", prep_phone),
        ]) if prep_name else []),

        # Contact Information
        section_header("Contact Information"),
        row("Site Contact", _val("site_contact")),
        row("Site Contact Number", _val("site_contact_number")),
        row("Site Contact Email", _val("site_contact_email")),
        row("Secondary Site Contact", _val("secondary_site_contact")),
        row("Secondary Site Contact Number", _val("secondary_site_contact_number")),
        row("Secondary Site Contact Email", _val("secondary_site_contact_email")),

        # Site Information
        section_header("Site Information"),
        row("Site Name", _val("site_name")),
        row("Site Address", _val("site_address")),
        row("Site Postcode", _val("site_postcode")),

        # Access & Instructions
        section_header("Access & Instructions"),
        row("Opening Times", _val("opening_times")),
        row("Access", _val("access")),
        row("Site Restrictions", _val("site_restrictions")),
        row("Special Instructions", _val("special_instructions")),
    ])

    # Products & Services section with two-line waste stream format
    email_line_items = list(payload.get("line_items") or [])
    _inject_doc_type_line(email_line_items, payload.get("document_type"))
    if email_line_items:
        li_rows = "".join([section_header("Products & Services")])
        for li_item in email_line_items:
            container = _esc(str(li_item.get("container") or "") or "\u2014")
            li_qty = li_item.get("quantity", 1)
            try:
                li_qty = int(li_qty)
            except (TypeError, ValueError):
                li_qty = 1
            # Skip quantity display for auto-added document type line
            if li_item.get("_is_doc_type_line"):
                qty_html = ""
            else:
                qty_html = f' <span style="font-size:11px; color:#718096;">(x{li_qty})</span>' if li_qty > 1 else ""
            weee_line, desc_line = _waste_stream_parts(li_item)
            mt = _esc(str(li_item.get("movement_type") or "") or "\u2014")
            try:
                li_price = float(li_item.get("price") or li_item.get("line_total") or li_item.get("unit_price") or 0)
            except (TypeError, ValueError):
                li_price = 0.0
            # Two-line waste stream: WEEE category (small/grey) + description (normal)
            if weee_line and desc_line:
                ws_html = (
                    f'<span style="font-size:11px; color:#718096;">{_esc(weee_line)}</span>'
                    f'<br/>{_esc(desc_line)}'
                )
            else:
                ws_html = _esc(weee_line or desc_line or _format_waste_stream_display(li_item))
            li_rows += (
                f'<tr {row_style}>'
                f'<td {label_style}>{container}{qty_html}</td>'
                f'<td {value_style}>'
                f'{ws_html}<br/>'
                f'<span style="font-size:11px; color:#718096;">{mt}</span>'
                f' &mdash; \u00a3{li_price:.2f}'
                f'</td>'
                f'</tr>'
            )
        table_rows += li_rows

    logo_url = (
        "https://i0.wp.com/wasteexperts.co.uk/wp-content/uploads/"
        "2022/11/green-grey-logo-1080.png?w=1920&ssl=1"
    )

    html = f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"/></head>
<body style="margin:0; padding:0; background:#f4f7fb; font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f7fb; padding:24px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff; border-radius:4px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.08);">

  <!-- Logo header -->
  <tr>
    <td style="background:#1e2e3d; padding:20px 24px; text-align:center;">
      <img src="{logo_url}" alt="Waste Experts" width="180" style="display:block; margin:0 auto;" />
    </td>
  </tr>

  <!-- Green accent bar -->
  <tr><td style="background:#8ec431; height:4px; font-size:0; line-height:0;">&nbsp;</td></tr>

  <!-- Title bar -->
  <tr>
    <td style="padding:18px 24px; background:#ffffff; border-bottom:1px solid #e2e8f0;">
      <h1 style="margin:0; font-size:18px; font-weight:700; color:#1e2e3d;">
        Delivery Details
      </h1>
      <p style="margin:4px 0 0; font-size:13px; color:#718096;">
        Purchase Order &mdash; {_esc(_val("purchase_order_number"))}
      </p>
    </td>
  </tr>

  <!-- Data table -->
  <tr>
    <td style="padding:0;">
      <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
        {table_rows}
      </table>
    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="background:#8ec431; padding:14px 24px; text-align:center;">
      <p style="margin:0; font-size:12px; color:#ffffff; font-weight:600;">
        Made with &#x1F49A; by Marketing &mdash; Waste Experts
      </p>
    </td>
  </tr>

</table>
</td></tr>
</table>
</body>
</html>"""

    return html


@app.route("/api/send-delivery-email/status", methods=["GET"])
def delivery_email_status():
    """Debug: check if Microsoft Graph email is configured."""
    configured = all([MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET])
    return jsonify({
        "configured": configured,
        "sender": MS_SENDER_EMAIL if configured else None,
    })


@app.route("/api/send-delivery-email", methods=["POST"])
def send_delivery_email():
    """Generate PDF and send it via Microsoft Graph API."""
    body = request.get_json(silent=True) or {}
    to_email = (body.get("to_email") or "").strip()
    payload = body.get("payload") or {}

    if not to_email:
        return jsonify({"error": "No recipient email provided"}), 400

    account_name = (payload.get("account_name") or "").strip()
    if account_name not in BROKERS:
        return jsonify({"error": "Please choose a valid supplier first"}), 400

    payload["supplier_address"] = payload.get("supplier_address") or BROKERS.get(account_name, "")

    # Generate the PDF (same logic as download)
    try:
        pdf_buffer = _build_review_pdf(payload)
    except Exception as exc:
        return jsonify({"error": f"PDF generation failed: {exc}"}), 500

    # Prepare email fields
    po_number = (payload.get("purchase_order_number") or "").strip() or "Unknown"
    supplier_name = "Waste Experts"

    pdf_bytes = pdf_buffer.getvalue()

    # Sanitised filename
    safe_po = _sanitise_filename(po_number)
    safe_supplier = _sanitise_filename(supplier_name)
    attachment_filename = f"PO_{safe_po}_{safe_supplier}.pdf"

    # Build branded HTML email body with full operations detail
    email_html = _build_delivery_email_html(payload)

    subject = f"Purchase Order \u2014 {po_number} \u2014 {supplier_name}"

    try:
        ok, err = _send_email_via_graph(to_email, subject, email_html, attachment_filename, pdf_bytes)
        if ok:
            return jsonify({"success": True}), 200
        else:
            return jsonify({"error": err or "Failed to send email"}), 500
    except Exception as exc:
        return jsonify({"error": f"Failed to send email: {exc}"}), 500


if __name__ == "__main__":
    app.run(debug=True)
