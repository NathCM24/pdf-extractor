#!/usr/bin/env python3
"""
generate_quote.py — Waste Experts Quote Generator

Reads a purchase order PDF, extracts key fields with Claude AI,
and renders a branded Waste Experts quote PDF.

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
import io
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
BRAND_LOGO_URL = "https://i0.wp.com/wasteexperts.co.uk/wp-content/uploads/2022/11/green-grey-logo-1080.png?w=1920&ssl=1"

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

EXTRACT_PROMPT = """You are extracting data from a supplier purchase order PDF sent to Waste Experts.

STEP 1 — IDENTIFY THE BROKER (Bill To)
The company who sent this PO is one of our approved brokers. Scan the ENTIRE document — logo, header, footer, terms, email addresses, phrases like "X Ltd employee" or "email to X" or "accept X terms & conditions" or "Registered Office" — and match against this approved broker list:

ACM Environmental PLC, Acumen Waste Services, 707 Ltd, Click Waste, Aqua Force Special Waste Ltd, AMA Waste, Associated Waste Management Ltd, Asprey St John & Co Ltd, Ash Waste Services Ltd, ACMS Waste Limited, A1 Chemical Waste Management Ltd, Alchemy Metals Ltd, Bakers Waste Services Ltd, Biffa Waste Services Limited, BW Skip Hire, Bywaters (Leyton) Limited, Bagnall & Morris Waste Services Ltd, Baileys Skip Hire and Recycling Ltd, Business Waste Ltd, Brown Recycling Ltd, BKP Waste & Recycling Ltd, Belford Bros Skip Hire Ltd, Countrystyle Recycling Ltd, Cartwrights Waste Disposal Services, C & M Waste Management Ltd, Change Waste Recycling Limited, Chloros Environmental Ltd, CHC Waste FM Ltd, Cleansing Service Group Ltd, Circle Waste Ltd, City Waste London Ltd, Cheshire Waste Skip Hire Limited, Circom Ltd, CITB, DP Skip Hire Ltd, Forward Environmental Ltd, EMN Plant Ltd, E-Cycle Limited, Enva England Ltd, Ellgia Ltd, Eco-Cycle Waste Management Ltd, Enva WEEE Recycling Scotland Ltd, FPWM Ltd, Footprint Recycling, Forward Waste Management Ltd, Fresh Start Waste Ltd, Forvis Mazars LLP, Greenzone Facilities Management Ltd, Greenway Environmental Ltd, GPT Waste Management Ltd, Go Green, Germstar UK Ltd, GD Environmental Services Ltd, Great Western Recycling Ltd, Grundon Waste Management Ltd, Gillett Environmental Ltd, Go For It Trading Ltd, Go 4 Greener Waste Management Ltd, Intelligent Waste Management Limited, J & B Recycling Ltd, Just Clear Ltd, Just A Step UK Ltd, J Dickinson & Sons (Horwich) Limited, Kenny Waste Management Ltd, Kane Management Consultancy Ltd, LSS Waste Management, LTL Systems Ltd, Mitie Waste & Environmental Services Limited, Mitie, MJ Church Recycling Ltd, M & M Skip Hire Ltd, MVV Environment, Mick George Recycling Ltd, NWH Waste Services, Nationwide Waste Services Limited, Optima Health UK Ltd, Premier Waste Recycling Ltd, Pearce Recycling Company Ltd, Phoenix Environmental Management Ltd, Papilo Ltd, RFMW UK Ltd, Riverdale Paper PLC, Remondis Ltd, Roydon Resource Recovery Limited, Risinxtreme Limited, Recorra Ltd, Sackers Ltd, Suez Recycling and Recovery UK Ltd, Suez, Safety Kleen UK Limited, Mobius Environmental Ltd, Sustainable Waste Services, Select A Skip UK Ltd, Slicker Recycling Ltd, Sommers Waste Solutions Limited, Saica Natur UK Ltd, Site Clear Solutions Ltd, Sharp Brothers (Skips) Ltd, SLM Waste Management Limited, Shredall (East Midlands) Limited, Scott Waste Limited, Smiths (Gloucester) Ltd, Tradebe North West Ltd, The Waste Brokerage Co Ltd, T.Ward & Son Ltd, Terracycle UK Limited, UK Waste Solutions Ltd, UBT (EU) Ltd, Veolia ES (UK) Ltd, Verto Recycle Ltd, Waste Management Facilities Ltd, Reconomy (UK) Ltd, Waterman Waste Management Ltd, Wastenot Ltd, Waste Wise Management Solutions, WEEE (Scotland) Ltd, WM101 Ltd, Whitkirk Waste Solutions Ltd, Williams Environmental Management Ltd, Wastesolve Limited, WMR Waste Solutions Ltd, Wheeldon Brothers Waste Ltd, Waste Cloud Limited, Yorwaste Ltd, Yes Waste Limited

Set po_provider_name to the BEST MATCHING name from the list above. If no match found, set to null.
NEVER use: Waste Experts, Electrical Waste Recycling Group, Electrical Waste Recycling Group Limited, or the waste producer / site company as the broker.

STEP 2 — EXTRACT ALL FIELDS and return ONLY a valid JSON object:

{
  "po_provider_name":    "Best matching broker name from the approved list above, or null if genuinely not found",
  "po_provider_address": "Full postal address of the broker, exactly as it appears on the document. Newline-separated. null if not found.",
  "po_provider_email":   "Email address belonging to the broker's domain (not wasteexperts.co.uk). null if not found.",

  "supplier_name":       "Same as po_provider_name",
  "supplier_address":    "Same as po_provider_address",
  "supplier_email":      "Same as po_provider_email",

  "client_name":      "Company name of the buyer/client if present, or null",
  "client_address":   "Full postal address of the client, newline-separated, or null",
  "client_email":     "Client email address, or null",
  "reference_number": "The PO or order reference number (e.g. 26131771615)",
  "quote_expiry_date":"Look for: 'Quote expires', 'Valid until', 'Valid to', 'Expiry date', 'Order valid until'. Return DD/MM/YYYY format, or null.",
  "job_name":         "SHORT 3-6 word job title based on container and service type (e.g. '8ft Dura Pipe Exchange', 'Fluorescent Tube Collection')",
  "waste_type":       "Waste type or material as stated on the PO",
  "ewc_code":         "EWC code if present (e.g. '20.01.21*'). Codes ending in * are hazardous.",
  "site_postcode":    "Site/collection postcode, or null",
  "note_already_included": true or false — true if the transport/service price already includes the consignment or transfer note fee,
  "line_items": [
    {
      "description": "Style: 'Container - Service Type' e.g. '8ft Dura Pipe - Exchange', 'Mixed Lamps - Full Service Charge'. Max 6 words. No EWC codes.",
      "quantity":    1,
      "unit_price":  0.00,
      "line_total":  0.00
    }
  ],
  "notes": "Special instructions, access info, site contact details. Empty string if none.",
  "terms_important_info": "Verbatim footer/terms block text, or empty string"
}

RULES:
- For POs with a 'Pricing / Transport Cost' section (broker-format): create ONE line item from the container+service description using Transport Cost as unit_price. Set note_already_included to true.
- Do NOT add consignment or transfer notes as line items — the system adds these automatically.
- Use numeric types for quantity, unit_price, line_total.
- Return ONLY the JSON object — no markdown, no explanation."""

INVALID_BILL_TO_PATTERNS = (
    "waste experts",
    "electrical waste",
    "electrical waste recycling group",
)

# ─── full broker list ─────────────────────────────────────────────────────────
# Canonical names exactly as they should appear on the quote's "Bill To" field.
# Matched against raw PDF text using normalised substring search.

BROKER_LIST = [
    "ACM Environmental PLC",
    "Acumen Waste Services",
    "707 Ltd",
    "Click Waste",
    "Aqua Force Special Waste Ltd",
    "AMA Waste",
    "Associated Waste Management Ltd",
    "Asprey St John & Co Ltd",
    "Ash Waste Services Ltd",
    "ACMS Waste Limited",
    "A1 Chemical Waste Management Ltd",
    "Alchemy Metals Ltd",
    "Bakers Waste Services Ltd",
    "Biffa Waste Services Limited",
    "BW Skip Hire",
    "Bywaters (Leyton) Limited",
    "Bagnall & Morris Waste Services Ltd",
    "Baileys Skip Hire and Recycling Ltd",
    "Business Waste Ltd",
    "Brown Recycling Ltd",
    "BKP Waste & Recycling Ltd",
    "Belford Bros Skip Hire Ltd",
    "Countrystyle Recycling Ltd",
    "Cartwrights Waste Disposal Services",
    "C & M Waste Management Ltd",
    "Change Waste Recycling Limited",
    "Chloros Environmental Ltd",
    "CHC Waste FM Ltd",
    "Cleansing Service Group Ltd",
    "Circle Waste Ltd",
    "City Waste London Ltd",
    "Cheshire Waste Skip Hire Limited",
    "Circom Ltd",
    "CITB",
    "DP Skip Hire Ltd",
    "Forward Environmental Ltd",
    "EMN Plant Ltd",
    "E-Cycle Limited",
    "Enva England Ltd",
    "Ellgia Ltd",
    "Eco-Cycle Waste Management Ltd",
    "Enva WEEE Recycling Scotland Ltd",
    "FPWM Ltd",
    "Footprint Recycling",
    "Forward Waste Management Ltd",
    "Fresh Start Waste Ltd",
    "Forvis Mazars LLP",
    "Greenzone Facilities Management Ltd",
    "Greenway Environmental Ltd",
    "GPT Waste Management Ltd",
    "Go Green",
    "Germstar UK Ltd",
    "GD Environmental Services Ltd",
    "Great Western Recycling Ltd",
    "Grundon Waste Management Ltd",
    "Gillett Environmental Ltd",
    "Go For It Trading Ltd",
    "Go 4 Greener Waste Management Ltd",
    "Intelligent Waste Management Limited",
    "J & B Recycling Ltd",
    "Just Clear Ltd",
    "Just A Step UK Ltd",
    "J Dickinson & Sons (Horwich) Limited",
    "Kenny Waste Management Ltd",
    "Kane Management Consultancy Ltd",
    "LSS Waste Management",
    "LTL Systems Ltd",
    "Mitie Waste & Environmental Services Limited",
    "Mitie",
    "MJ Church Recycling Ltd",
    "M & M Skip Hire Ltd",
    "MVV Environment",
    "Mick George Recycling Ltd",
    "NWH Waste Services",
    "Nationwide Waste Services Limited",
    "Optima Health UK Ltd",
    "Premier Waste Recycling Ltd",
    "Pearce Recycling Company Ltd",
    "Phoenix Environmental Management Ltd",
    "Papilo Ltd",
    "RFMW UK Ltd",
    "Riverdale Paper PLC",
    "Remondis Ltd",
    "Roydon Resource Recovery Limited",
    "Risinxtreme Limited",
    "Recorra Ltd",
    "Sackers Ltd",
    "Suez Recycling and Recovery UK Ltd",
    "Suez",
    "Safety Kleen UK Limited",
    "Mobius Environmental Ltd",
    "Sustainable Waste Services",
    "Select A Skip UK Ltd",
    "Slicker Recycling Ltd",
    "Sommers Waste Solutions Limited",
    "Saica Natur UK Ltd",
    "Site Clear Solutions Ltd",
    "Sharp Brothers (Skips) Ltd",
    "SLM Waste Management Limited",
    "Shredall (East Midlands) Limited",
    "Scott Waste Limited",
    "Smiths (Gloucester) Ltd",
    "Tradebe North West Ltd",
    "The Waste Brokerage Co Ltd",
    "T.Ward & Son Ltd",
    "Terracycle UK Limited",
    "UK Waste Solutions Ltd",
    "UBT (EU) Ltd",
    "Veolia ES (UK) Ltd",
    "Verto Recycle Ltd",
    "Waste Management Facilities Ltd",
    "Reconomy (UK) Ltd",
    "Waterman Waste Management Ltd",
    "Wastenot Ltd",
    "Waste Wise Management Solutions",
    "WEEE (Scotland) Ltd",
    "WM101 Ltd",
    "Whitkirk Waste Solutions Ltd",
    "Williams Environmental Management Ltd",
    "Wastesolve Limited",
    "WMR Waste Solutions Ltd",
    "Wheeldon Brothers Waste Ltd",
    "Waste Cloud Limited",
    "Yorwaste Ltd",
    "Yes Waste Limited",
]

# Pre-compute normalised keys for fast matching
def _normalise(text: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation for fuzzy matching."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()

_BROKER_NORMALISED = [(_normalise(b), b) for b in BROKER_LIST]


def _extract_pdf_text(pdf_path: Path) -> str:
    """Extract raw text from PDF using pypdf if available, else return empty string."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except ImportError:
        pass
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(str(pdf_path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except ImportError:
        pass
    return ""  # fall through to Claude-only path


def match_broker_in_text(raw_text: str) -> str | None:
    """Scan raw PDF text for any known broker name. Returns canonical name or None.

    Strategy:
      1. Exact normalised substring match (catches full legal names)
      2. Short-name match for brokers with common short forms (e.g. 'go green', 'suez', 'mitie')
         — only triggered when name is ≥4 chars to avoid false positives
    """
    normalised_doc = _normalise(raw_text)

    # Longest-match first — prevents 'Suez' matching before 'Suez Recycling and Recovery UK Ltd'
    sorted_brokers = sorted(_BROKER_NORMALISED, key=lambda x: len(x[0]), reverse=True)

    for norm_broker, canonical in sorted_brokers:
        if len(norm_broker) < 4:
            continue  # too short — skip to avoid false positives
        if norm_broker in normalised_doc:
            return canonical

    return None


def _is_invalid_supplier(name: str) -> bool:
    normalized = (name or "").strip().lower()
    return any(pat in normalized for pat in INVALID_BILL_TO_PATTERNS)


def _extract_company_candidates(text: str) -> list:
    """Find likely UK company names from free text (e.g. footer terms)."""
    if not text:
        return []
    pattern = re.compile(
        r"\b([A-Z][A-Za-z&'.,-]*(?:\s+[A-Z][A-Za-z&'.,-]*){0,5}\s+(?:Ltd|Limited|PLC|LLP))\b"
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
        r"Registered Office:\s*(.+?)(?:Registered in|Company Number|$)",
        text,
        flags=re.I | re.S,
    )
    if not m:
        return ""
    return " ".join(m.group(1).replace("\n", " ").split()).strip(" ,")


def normalize_extracted_data(data: dict) -> dict:
    """Consolidate po_provider fields into supplier fields. Broker matching happens separately."""
    # Prefer po_provider fields over supplier fields
    supplier_name    = (data.get("po_provider_name")    or data.get("supplier_name")    or "").strip()
    supplier_address = (data.get("po_provider_address") or data.get("supplier_address") or "").strip()
    supplier_email   = (data.get("po_provider_email")   or data.get("supplier_email")   or "").strip()

    # Clear out anything that's actually us
    if _is_invalid_supplier(supplier_name):
        supplier_name    = ""
        supplier_address = ""
        supplier_email   = ""

    data["supplier_name"]    = supplier_name    or None
    data["supplier_address"] = supplier_address or None
    data["supplier_email"]   = supplier_email   or None
    return data


# ─── hazardous waste detection ───────────────────────────────────────────────

# EWC codes ending in * are always hazardous by EU/UK definition.
# These keywords in waste type descriptions also indicate hazardous waste.
HAZARDOUS_KEYWORDS = {
    "mercury", "fluorescent", "crt", "cathode ray", "asbestos", "clinical",
    "hazardous", "oil", "solvent", "pcb", "cyanide", "acid", "alkali",
    "paint", "varnish", "adhesive", "resin", "infectious", "cytotoxic",
    "pharmaceutical", "amalgam", "lead", "cadmium", "arsenic", "chromium",
    "photochemical", "developer", "fixer", "toner", "ink", "vape", "e-cigarette",
    "lithium", "nicad", "nickel cadmium", "battery", "ionisation", "smoke detector",
}


def is_hazardous(data: dict) -> bool:
    """Return True if the job involves hazardous waste.

    Checks:
      1. EWC code ending in * (definitive — all * codes are hazardous)
      2. Keyword match in waste_type, job_name, or any line item description
    """
    ewc = (data.get("ewc_code") or "").strip()
    if ewc.endswith("*"):
        return True

    # Build a combined text blob to search
    texts = [
        data.get("waste_type") or "",
        data.get("job_name") or "",
    ]
    for item in (data.get("line_items") or []):
        texts.append(item.get("description") or "")

    combined = " ".join(texts).lower()
    return any(kw in combined for kw in HAZARDOUS_KEYWORDS)


def inject_note_charge(data: dict) -> dict:
    """Append the appropriate statutory note charge to line_items.

    Hazardous waste  → Consignment Note    £40.00
    Non-hazardous    → Waste Transfer Note £40.00

    Skips if:
      - Claude flagged note_already_included (embedded in transport cost)
      - A consignment/transfer note line item already exists
    """
    # If Claude detected the note is already baked into the transport price, skip
    if data.get("note_already_included"):
        return data

    hazardous = is_hazardous(data)
    note_label = "Consignment Note" if hazardous else "Waste Transfer Note"

    # Avoid duplicating if Claude already extracted one as a line item
    existing = [
        i for i in (data.get("line_items") or [])
        if "consignment" in (i.get("description") or "").lower()
        or "transfer note" in (i.get("description") or "").lower()
    ]
    if existing:
        return data

    charge = {"description": note_label, "quantity": 1, "unit_price": 40.0, "line_total": 40.0}
    data.setdefault("line_items", []).append(charge)
    data["_note_type"] = note_label
    return data


def extract(pdf_path: Path) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        sys.exit("Error: ANTHROPIC_API_KEY environment variable is not set.")

    client = anthropic.Anthropic(api_key=api_key)
    pdf_bytes = pdf_path.read_bytes()
    b64 = base64.standard_b64encode(pdf_bytes).decode()

    resp = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2000,
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
    if "```" in raw:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        raw = raw[start:end]

    parsed = json.loads(raw)
    data = normalize_extracted_data(parsed)

    # ── DEBUG ─────────────────────────────────────────────────────────────────
    print(f"  [debug] Claude po_provider_name : {parsed.get('po_provider_name')}")
    print(f"  [debug] Claude supplier_name    : {parsed.get('supplier_name')}")
    print(f"  [debug] After normalize         : {data.get('supplier_name')}")

    # ── Broker list matching — ALWAYS wins ────────────────────────────────────
    # Scan the full raw Claude response text (which contains everything Claude
    # read from the PDF including terms/footer) against our broker list.
    # No pypdf needed. If we find a match, it's definitive.
    scan_text = raw + " " + " ".join([
        str(data.get("terms_important_info") or ""),
        str(data.get("notes") or ""),
    ])

    matched_broker = match_broker_in_text(scan_text)
    print(f"  [debug] Broker list match       : {matched_broker}")

    if matched_broker:
        data["supplier_name"] = matched_broker
        print(f"  [broker] ✓ Set supplier to: {matched_broker}")
    elif not data.get("supplier_name"):
        print("  [warn] No broker matched — Bill To will show as not found")

    return inject_note_charge(data)


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
    remote_logo_reader = None

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
        draw_top_border()
        c.setFont(FONT_R, 9)
        c.setFillColor(TEXT_GREY)
        c.setStrokeColor(MID_GREY)
        if redraw:
            redraw()

    def get_remote_logo_reader():
        nonlocal remote_logo_reader
        if remote_logo_reader is not None:
            return remote_logo_reader
        try:
            with urllib.request.urlopen(BRAND_LOGO_URL, timeout=8) as resp:
                remote_logo_reader = ImageReader(io.BytesIO(resp.read()))
        except Exception:
            remote_logo_reader = False
        return remote_logo_reader or None

    def get_brand_logo_reader():
        if logo_path.exists():
            try:
                return ImageReader(str(logo_path))
            except Exception:
                pass
        return get_remote_logo_reader()

    def draw_brand_logo(*args, draw=True):
        """Draw logo and return width.

        Supports both signatures:
          - draw_brand_logo(logo_reader, x, top_y, logo_h, draw=True)
          - draw_brand_logo(x, top_y, logo_h, draw=True)  # auto-fetches logo
        """
        if len(args) == 4:
            logo_reader, x, top_y, logo_h = args
        elif len(args) == 3:
            x, top_y, logo_h = args
            logo_reader = get_brand_logo_reader()
        else:
            raise TypeError("draw_brand_logo expects 3 or 4 positional arguments")

        if not logo_reader:
            return None
        try:
            iw, ih = logo_reader.getSize()
            logo_w = logo_h * (iw / ih)
            if draw:
                c.drawImage(
                    logo_reader,
                    x,
                    top_y - logo_h,
                    width=logo_w,
                    height=logo_h,
                    preserveAspectRatio=True,
                    mask="auto",
                )
            return logo_w
        except Exception:
            return None

    draw_top_border()

    # ── Logo (centered at top) ─────────────────────────────────────────────
    logo_h = 16 * mm
    top_logo = get_brand_logo_reader()
    top_logo_w = draw_brand_logo(top_logo, 0, y, logo_h, draw=False)
    if top_logo_w is not None:
        draw_brand_logo(top_logo, (PAGE_W - top_logo_w) / 2, y, logo_h)
    else:
        c.setFont(FONT_XB, 11)
        c.setFillColor(NAVY)
        c.drawCentredString(PAGE_W / 2, y - logo_h + 2 * mm, "LOGO")

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
    addr_lines = [
        l.strip()
        for l in (supplier_address or "").split("\n")
        if l.strip()
    ][:6]
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

    # Data rows
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

        # Truncate description if too wide
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
        c.drawRightString(rx + col_w[1] - 3 * mm, text_y, str(int(qty_f) if qty_f.is_integer() else qty_f))
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

    # ── Footer ───────────────────────────────────────────────────────────────
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
