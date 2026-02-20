from flask import Flask, jsonify, render_template, request, send_file
import base64
import io
import json
import os
import re
import urllib.request
from datetime import datetime
from io import BytesIO
from pathlib import Path

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
  "document_type": "Consignment Note or Waste Transfer Note if explicitly stated, else null"
}}

RULES:
- Use null when a value is genuinely not found.
- Return JSON only. No markdown. No explanation.
"""




# ─── Brand constants ──────────────────────────────────────────────────────────

NAVY = colors.HexColor("#1e2e3d")
GREEN = colors.HexColor("#8ec431")
WHITE = colors.white
LIGHT_ROW = colors.HexColor("#f0f4f8")
MID_GREY = colors.HexColor("#e2e8f0")
TEXT_GREY = colors.HexColor("#2d3748")
LABEL_GREY = colors.HexColor("#718096")
BORDER_CLR = colors.HexColor("#c8d6e5")
BG_BOX = colors.HexColor("#f0f4f8")
SECTION_BG = colors.HexColor("#e8edf2")

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
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

SCRIPT_DIR = Path(__file__).parent
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

    # ── Green top border ──────────────────────────────────────────────────
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

    # ── Title ─────────────────────────────────────────────────────────────
    site_name = (payload.get("site_name") or "").strip() or "Unknown"
    site_postcode = (payload.get("site_postcode") or "").strip() or "Unknown"
    service_desc = (payload.get("service_description") or "").strip()
    if service_desc:
        title_text = f"{site_name} - {service_desc} - {site_postcode}".upper()
    else:
        title_text = f"{site_name} | {site_postcode}".upper()

    c.setFillColor(NAVY)
    font_size = 16
    while font_size > 9 and c.stringWidth(title_text, FONT_XB, font_size) > CONTENT_W:
        font_size -= 0.5
    c.setFont(FONT_XB, font_size)
    c.drawCentredString(PAGE_W / 2, y, title_text)
    down(6 * mm)

    # ── Full-width horizontal rule ────────────────────────────────────────
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
    c.setFillColor(TEXT_GREY)
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
    c.setFillColor(TEXT_GREY)
    for we_line in WE_ADDRESS:
        c.drawString(col2_x, y, we_line)
        y -= 4.5 * mm
    bottom_right = y

    y = min(bottom_left, bottom_right)
    down(6 * mm)

    # ── Prepared By row ───────────────────────────────────────────────────
    prep_h = 17 * mm
    ensure_space(prep_h + 6 * mm)
    _rounded_rect(c, MARGIN, y - prep_h, CONTENT_W, prep_h, fill=colors.HexColor("#f5f7f9"))
    section_label(MARGIN + 3 * mm, y - 4 * mm, "Prepared By")
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

    # ── Reference / Quote Valid Until boxes ────────────────────────────────
    box_w = 65 * mm
    box_h = 13 * mm
    ensure_space(box_h + 8 * mm)

    _rounded_rect(c, MARGIN, y - box_h, box_w, box_h, fill=BG_BOX, stroke=MID_GREY)
    section_label(MARGIN + 3 * mm, y - 4.5 * mm, "Reference")
    c.setFont(FONT_B, 11)
    c.setFillColor(NAVY)
    c.drawString(MARGIN + 3 * mm, y - 9.5 * mm, payload.get("purchase_order_number") or "\u2014")

    ex_x = MARGIN + box_w + 5 * mm
    _rounded_rect(c, ex_x, y - box_h, box_w, box_h, fill=BG_BOX, stroke=MID_GREY)
    section_label(ex_x + 3 * mm, y - 4.5 * mm, "Quote Valid Until")
    c.setFont(FONT_B, 11)
    c.setFillColor(NAVY)
    c.drawString(ex_x + 3 * mm, y - 9.5 * mm, "\u2014")

    down(box_h + 8 * mm)

    # ── Main data table ───────────────────────────────────────────────────
    label_col_w = CONTENT_W * 0.38
    value_col_w = CONTENT_W * 0.62
    row_h = 8 * mm
    section_hdr_h = 7 * mm

    sections = [
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

            # Vertical divider between label and value columns
            c.line(MARGIN + label_col_w, y, MARGIN + label_col_w, y - this_row_h)

            # Label text (bold)
            c.setFont(FONT_B, 9)
            c.setFillColor(NAVY)
            label_y = y - (this_row_h / 2) - 1 * mm if len(value_lines) <= 1 else y - 5 * mm
            c.drawString(MARGIN + 4 * mm, label_y, field_label)

            # Value text
            c.setFont(FONT_R, 9)
            c.setFillColor(TEXT_GREY)
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
        c.setFillColor(TEXT_GREY)
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
    data["account_name"] = ""  # User must select via typeahead
    data["supplier_found"] = bool(supplier and supplier in BROKERS)
    data["supplier_address"] = BROKERS.get(supplier, "")
    data["document_type"] = data.get("document_type") or "Consignment Note"

    ordered_fields = [
        "account_name",
        "supplier",
        "purchase_order_number",
        "service_description",
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
    return {key: data.get(key) for key in ordered_fields}




@app.route("/download-review-pdf", methods=["POST"])
def download_review_pdf():
    payload = request.get_json(silent=True) or {}
    account_name = (payload.get("account_name") or "").strip()
    if account_name not in BROKERS:
        return jsonify({"error": "Please choose supplier"}), 400

    payload["supplier_address"] = payload.get("supplier_address") or BROKERS.get(account_name, "")

    pdf_buffer = _build_review_pdf(payload)

    # Filename: {Account Name} - {Service Description} - {Site Postcode}.pdf
    safe_account = _sanitise_filename(account_name) or "Unknown"
    safe_postcode = _sanitise_filename((payload.get("site_postcode") or "").strip()) or "Unknown"
    service_desc = (payload.get("service_description") or "").strip()
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
                        {"type": "text", "text": EXTRACT_PROMPT},
                    ],
                }
            ],
        )

        parsed = _clean_json_payload(resp.content[0].text)
        return jsonify({"success": True, "data": _normalise_data(parsed)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/brokers", methods=["GET"])
def get_brokers():
    return jsonify({"brokers": [{"name": name, "address": addr} for name, addr in BROKERS.items()]})


@app.route("/broker-address", methods=["GET"])
def broker_address():
    name = request.args.get("name", "")
    return jsonify({"name": name, "address": BROKERS.get(name, "")})


if __name__ == "__main__":
    app.run(debug=True)
