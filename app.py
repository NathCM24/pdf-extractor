from flask import Flask, jsonify, render_template, request, send_file
import base64
import json
import os
from datetime import datetime
from io import BytesIO

import anthropic
from reportlab.lib.pagesizes import A4
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




def _build_review_pdf(payload: dict) -> BytesIO:
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    y = height - 50
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(40, y, "Waste Experts - Reviewed Extraction")
    y -= 24
    pdf.setFont("Helvetica", 10)
    pdf.drawString(40, y, f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    y -= 22

    fields = [
        ("Account Name", payload.get("account_name")),
        ("Bill To Address", payload.get("supplier_address")),
        ("Purchase Order Number", payload.get("purchase_order_number")),
        ("Document Type", payload.get("document_type")),
        ("Site Contact", payload.get("site_contact")),
        ("Site Contact Number", payload.get("site_contact_number")),
        ("Site Contact Email", payload.get("site_contact_email")),
        ("Secondary Site Contact", payload.get("secondary_site_contact")),
        ("Secondary Site Contact Number", payload.get("secondary_site_contact_number")),
        ("Secondary Site Contact Email", payload.get("secondary_site_contact_email")),
        ("Site Name", payload.get("site_name")),
        ("Site Address", payload.get("site_address")),
        ("Site Postcode", payload.get("site_postcode")),
        ("Opening Times", payload.get("opening_times")),
        ("Access", payload.get("access")),
        ("Site Restrictions", payload.get("site_restrictions")),
        ("Special Instructions", payload.get("special_instructions")),
    ]

    for label, value in fields:
        text = str(value or "")
        if y < 90:
            pdf.showPage()
            y = height - 50
            pdf.setFont("Helvetica", 10)

        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(40, y, f"{label}:")
        y -= 14

        pdf.setFont("Helvetica", 10)
        lines = text.splitlines() if text else ["-"]
        for line in lines:
            if y < 70:
                pdf.showPage()
                y = height - 50
                pdf.setFont("Helvetica", 10)
            pdf.drawString(55, y, line[:140])
            y -= 12
        y -= 4

    pdf.save()
    buffer.seek(0)
    return buffer

def _clean_json_payload(raw_text: str):
    payload = raw_text.strip()
    if "```" in payload and "{" in payload and "}" in payload:
        payload = payload[payload.find("{") : payload.rfind("}") + 1]
    return json.loads(payload)


def _normalise_data(data: dict):
    supplier = (data.get("supplier") or "").strip()
    if supplier not in BROKERS:
        supplier = ""

    data["supplier"] = supplier
    data["account_name"] = supplier or data.get("account_name") or ""
    data["supplier_found"] = bool(supplier)
    data["supplier_address"] = BROKERS.get(supplier, "")
    data["document_type"] = data.get("document_type") or "Consignment Note"

    ordered_fields = [
        "account_name",
        "supplier",
        "purchase_order_number",
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
    safe_account = account_name.replace("/", "-").replace(" ", "_") or "review"
    filename = f"{safe_account}_review.pdf"
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
