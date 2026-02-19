"""
app.py — Waste Experts Tools
 • /         Supplier Order Extractor  (HubSpot CSV export)
 • /quote    Quote Generator           (branded PDF output)
"""

import os
import io
import re
import json
import base64
import csv
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, render_template, send_file
import anthropic

# Load .env if present
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Quote-generator integration ───────────────────────────────────────────────
try:
    import generate_quote as gq
    gq.ensure_fonts()
    QUOTE_AVAILABLE = True
except Exception as _gq_err:
    QUOTE_AVAILABLE = False
    print(f"[warn] Quote generator unavailable: {_gq_err}")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB max upload

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

SYSTEM_PROMPT = """You are a data extraction assistant for a WEEE recycling company called Waste Experts.
You will be given a supplier purchase order PDF (typically from brokers like Go Green, Mitie, Suez, etc.)
and must extract specific fields for entry into HubSpot CRM.

Extract ALL of the following fields. If a field is not found, return null.

Return ONLY a valid JSON object with exactly these keys:

{
  "client_name": "Name of the broker/client who sent the PO (e.g. Go Green, Mitie, Suez)",
  "order_number": "Their PO/order reference number",
  "order_raised_date": "Date the order was raised (DD/MM/YYYY)",
  "job_date": "Date the job is scheduled (DD/MM/YYYY HH:MM or DD/MM/YYYY)",
  "movement_type": "e.g. Exchange, Collection, Delivery",
  "container_type": "Container/equipment type as stated on the PO",
  "waste_type": "Description of the waste type",
  "ewc_code": "European Waste Catalogue code",
  "waste_producer": "Name of the company producing the waste",
  "site_name": "Name of the site",
  "site_address": "Full site address",
  "site_postcode": "Site postcode",
  "site_contact_name": "Name of the site contact person",
  "site_contact_number": "Site contact phone number(s)",
  "access_times": "Site access hours",
  "order_notes": "Any special instructions or order notes",
  "transport_cost": "Transport cost as a number (no £ symbol)",
  "per_hour_cost": "Per hour charge as a number (0 if not applicable)",
  "per_tonne_cost": "Per tonne cost as a number (0 if not applicable)",
  "total_cost": "Total cost as a number (no £ symbol)",
  "facility_name": "Waste processing facility name",
  "wcl_number": "Waste Carriers Licence number",
  "wml_number": "Waste Management Licence number",
  "broker_contact_email": "Email address from the order if present",
  "requires_review": true or false (true if anything is unclear or missing),
  "review_notes": ["list of anything that needs human verification"]
}

Return ONLY the JSON object. No preamble, no explanation, no markdown fences."""


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/extract", methods=["POST"])
def extract():
    if "pdf" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["pdf"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "File must be a PDF"}), 400

    pdf_bytes = file.read()
    pdf_base64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    try:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_base64,
                            },
                        },
                        {
                            "type": "text",
                            "text": "Extract all fields from this supplier purchase order.",
                        },
                    ],
                }
            ],
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        data = json.loads(raw)
        data["_filename"] = file.filename
        data["_extracted_at"] = datetime.now().strftime("%d/%m/%Y %H:%M")
        return jsonify({"success": True, "data": data})

    except json.JSONDecodeError:
        return jsonify({"error": "Could not parse extraction result. Please try again."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/download-csv", methods=["POST"])
def download_csv():
    data = request.json.get("data", {})
    if not data:
        return jsonify({"error": "No data"}), 400

    # HubSpot-friendly CSV field mapping
    hubspot_fields = {
        "Client Name": data.get("client_name", ""),
        "Order Number": data.get("order_number", ""),
        "Order Raised Date": data.get("order_raised_date", ""),
        "Job Date": data.get("job_date", ""),
        "Movement Type": data.get("movement_type", ""),
        "Container Type": data.get("container_type", ""),
        "Waste Type": data.get("waste_type", ""),
        "EWC Code": data.get("ewc_code", ""),
        "Waste Producer": data.get("waste_producer", ""),
        "Site Name": data.get("site_name", ""),
        "Site Address": data.get("site_address", ""),
        "Site Postcode": data.get("site_postcode", ""),
        "Site Contact Name": data.get("site_contact_name", ""),
        "Site Contact Number": data.get("site_contact_number", ""),
        "Access Times": data.get("access_times", ""),
        "Order Notes": data.get("order_notes", ""),
        "Transport Cost": data.get("transport_cost", ""),
        "Per Hour Cost": data.get("per_hour_cost", ""),
        "Per Tonne Cost": data.get("per_tonne_cost", ""),
        "Total Cost": data.get("total_cost", ""),
        "Facility Name": data.get("facility_name", ""),
        "WCL Number": data.get("wcl_number", ""),
        "WML Number": data.get("wml_number", ""),
        "Broker Contact Email": data.get("broker_contact_email", ""),
        "Extracted At": data.get("_extracted_at", ""),
        "Source File": data.get("_filename", ""),
        "Review Required": "Yes" if data.get("requires_review") else "No",
        "Review Notes": " | ".join(data.get("review_notes", [])),
    }

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=hubspot_fields.keys())
    writer.writeheader()
    writer.writerow(hubspot_fields)

    output.seek(0)
    filename = f"order-extract-{data.get('order_number', 'unknown')}.csv"

    return send_file(
        io.BytesIO(output.getvalue().encode()),
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename,
    )



# ── Quote Generator routes ────────────────────────────────────────────────────

@app.route("/quote")
def quote_index():
    return render_template("quote.html")


@app.route("/logo")
def serve_logo():
    """Serve the Waste Experts logo from the project directory."""
    logo = next(
        (p for pattern in (
            "WhatsApp_Image_*.jpeg", "WhatsApp_Image_*.jpg",
            "logo.png", "logo.jpg",
        ) for p in Path(__file__).parent.glob(pattern)),
        None,
    )
    if logo:
        mime = "image/jpeg" if logo.suffix.lower() in (".jpg", ".jpeg") else "image/png"
        return send_file(str(logo), mimetype=mime)
    return "", 404


def _build_quote_filename(data: dict) -> str:
    """Build 'Supplier - JobType - Postcode.pdf' from data fields."""
    def sanitize(s, maxlen=30):
        s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', (s or "")).strip()
        return s[:maxlen].strip(" -")

    supplier = sanitize(data.get("client_name") or "Quote")
    job      = sanitize(data.get("job_name") or "")
    postcode = (data.get("site_postcode") or "").strip().split()[0]  # e.g. "SG19"
    postcode = sanitize(postcode)

    parts = [p for p in [supplier, job, postcode] if p]
    name  = " - ".join(parts) if parts else "quote"
    return name + ".pdf"


@app.route("/extract-quote-data", methods=["POST"])
def extract_quote_data():
    """Step 1: extract fields from a PO PDF and return them as JSON."""
    if not QUOTE_AVAILABLE:
        return jsonify({"error": "Quote generator unavailable."}), 500
    if "pdf" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["pdf"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "File must be a PDF"}), 400

    tmp_dir = Path(tempfile.mkdtemp())
    try:
        input_path = tmp_dir / "input.pdf"
        file.save(str(input_path))
        data = gq.extract(input_path)
        return jsonify({"success": True, "data": data})
    except (SystemExit, json.JSONDecodeError) as exc:
        return jsonify({"error": str(exc) or "Could not parse extraction result."}), 500
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.route("/render-quote", methods=["POST"])
def render_quote():
    """Step 2: generate a branded quote PDF from submitted (possibly edited) JSON data."""
    if not QUOTE_AVAILABLE:
        return jsonify({"error": "Quote generator unavailable."}), 500

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No data provided"}), 400

    tmp_dir = Path(tempfile.mkdtemp())
    try:
        logo_path = next(
            (p for pattern in (
                "WhatsApp_Image_*.jpeg", "WhatsApp_Image_*.jpg",
                "logo.png", "logo.jpg",
            ) for p in gq.SCRIPT_DIR.glob(pattern)),
            gq.SCRIPT_DIR / "logo.png",
        )
        out_name = _build_quote_filename(data)
        out_path = tmp_dir / out_name
        gq.generate_pdf(data, logo_path, out_path)
        pdf_bytes = out_path.read_bytes()
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=out_name,
        )
    except (SystemExit, json.JSONDecodeError) as exc:
        return jsonify({"error": str(exc) or "Could not generate PDF."}), 500
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.route("/generate-quote", methods=["POST"])
def generate_quote_route():
    if not QUOTE_AVAILABLE:
        return jsonify({"error": "Quote generator is not available. Check server logs."}), 500

    if "pdf" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["pdf"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "File must be a PDF"}), 400

    tmp_dir = Path(tempfile.mkdtemp())
    try:
        # Save uploaded PDF
        input_path = tmp_dir / "input.pdf"
        file.save(str(input_path))

        # Extract data from PDF using Claude
        data = gq.extract(input_path)

        # Locate logo
        logo_path = next(
            (p for pattern in (
                "WhatsApp_Image_*.jpeg", "WhatsApp_Image_*.jpg",
                "logo.png", "logo.jpg",
            ) for p in gq.SCRIPT_DIR.glob(pattern)),
            gq.SCRIPT_DIR / "logo.png",
        )

        # Generate output PDF
        ref  = (data.get("reference_number") or "quote")
        safe = "".join(ch for ch in ref if ch.isalnum() or ch in "-_")[:30]
        out_name = f"quote-{safe}-{datetime.now().strftime('%Y%m%d')}.pdf"
        out_path = tmp_dir / out_name

        gq.generate_pdf(data, logo_path, out_path)

        # Read into memory so we can clean up temp dir immediately
        pdf_bytes = out_path.read_bytes()

        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=out_name,
        )

    except (SystemExit, json.JSONDecodeError) as exc:
        return jsonify({"error": str(exc) or "Could not parse extraction result."}), 500
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
