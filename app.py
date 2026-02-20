from flask import Flask, jsonify, render_template, request
import base64
import json
import os

import anthropic

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB

ACCOUNT_NAMES = [
    "ACM ENVIRONMENTAL PLC",
    "ACUMEN WASTE SERVICES",
    "707 LTD - CLICK WASTE",
    "AQUA FORCE SPECIAL WASTE LTD",
    "AMA WASTE",
    "ASSOCIATED WASTE MANAGEMENT LTD",
    "ASPREY ST JOHN & CO LTD",
    "ASH WASTE SERVICES LTD",
    "ACMS WASTE LIMITED",
    "A1 CHEMICAL WASTE MANAGEMENT LTD",
    "ALCHEMY METALS LTD",
    "BAKERS WASTE SERVICES LTD",
    "BIFFA WASTE SERVICES LIMITED",
    "BW SKIP HIRE",
    "BYWATERS (LEYTON) LIMITED",
    "BAGNALL & MORRIS WASTE SERVICES LTD",
    "BAILEYS SKIP HIRE AND RECYCLING LTD",
    "BUSINESS WASTE LTD",
    "BROWN RECYCLING LTD",
    "BKP WASTE & RECYCLING LTD",
    "BELFORD BROS SKIP HIRE LTD",
    "COUNTRYSTYLE RECYCLING LTD",
    "CARTWRIGHTS WASTE DISPOSAL SERVICES",
    "C & M WASTE MANAGEMENT LTD",
    "CHANGE WASTE RECYCLING LIMITED",
    "CHLOROS ENVIRONMENTAL LTD",
    "CHC WASTE FM LTD",
    "CLEANSING SERVICE GROUP LTD",
    "CIRCLE WASTE LTD",
    "CITY WASTE LONDON LTD",
    "CHESHIRE WASTE SKIP HIRE LIMITED",
    "CIRCOM LTD",
    "CITB",
    "DP SKIP HIRE LTD",
    "FORWARD ENVIRONMENTAL LTD",
    "EMN PLANT LTD",
    "E-CYCLE LIMITED",
    "ENVA ENGLAND LTD",
    "ELLGIA LTD",
    "ECO-CYCLE WASTE MANAGEMENT LTD",
    "ENVA WEEE RECYCLING SCOTLAND LTD",
    "FPWM LTD T/A FOOTPRINT RECYCLING",
    "FORWARD WASTE MANAGEMENT LTD",
    "FRESH START WASTE LTD",
    "FORVIS MAZARS LLP",
    "GREENZONE FACILITIES MANAGEMENT LTD",
    "GREENWAY ENVIRONMENTAL LTD",
    "GPT WASTE MANAGEMENT LTD",
    "GO GREEN",
    "GERMSTAR UK LTD",
    "GD ENVIRONMENTAL SERVICES LTD",
    "GREAT WESTERN RECYCLING LTD",
    "GRUNDON WASTE MANAGEMENT LTD",
    "GILLETT ENVIRONMENTAL LTD",
    "GO FOR IT TRADING LTD",
    "GO 4 GREENER WASTE MANAGEMENT LTD",
    "INTELLIGENT WASTE MANAGEMENT LIMITED",
    "J & B RECYCLING LTD",
    "JUST CLEAR LTD.",
    "JUST A STEP UK LTD",
    "J DICKINSON & SONS (HORWICH) LIMITED",
    "KENNY WASTE MANAGEMENT LTD",
    "KANE MANAGEMENT CONSULTANCY LTD",
    "LSS WASTE MANAGEMENT",
    "LTL SYSTEMS LTD",
    "MITIE WASTE & ENVIRONMENTAL SERVICES LIMITED",
    "M J CHURCH RECYCLING LTD",
    "M & M SKIP HIRE LTD",
    "MVV ENVIRONMENT",
    "MICK GEORGE RECYCLING LTD",
    "NWH WASTE SERVICES",
    "NATIONWIDE WASTE SERVICES LIMITED",
    "OPTIMA HEALTH UK LTD",
    "PREMIER WASTE RECYCLING LTD",
    "PEARCE RECYCLING COMPANY LTD",
    "PHOENIX ENVIRONMENTAL MANAGEMENT LTD",
    "PAPILO LTD",
    "RFMW UK LTD",
    "RIVERDALE PAPER PLC",
    "REMONDIS LTD",
    "ROYDON RESOURCE RECOVERY LIMITED",
    "RISINXTREME LIMITED",
    "RECORRA LTD",
    "SACKERS LTD",
    "SUEZ RECYCLING AND RECOVERY UK LTD",
    "SAFETY KLEEN UK LIMITED",
    "MOBIUS ENVIRONMENTAL LTD",
    "SUSTAINABLE WASTE SERVICES",
    "SELECT A SKIP UK LTD",
    "SLICKER RECYCLING LTD",
    "SOMMERS WASTE SOLUTIONS LIMITED",
    "SAICA NATUR UK LTD",
    "SITE CLEAR SOLUTIONS LTD",
    "SHARP BROTHERS (SKIPS) LTD",
    "SLM WASTE MANAGEMENT LIMITED",
    "SHREDALL (EAST MIDLANDS) LIMITED",
    "SCOTT WASTE LIMITED",
    "SMITHS (GLOUCESTER) LTD.",
    "TRADEBE NORTH WEST LTD",
    "THE WASTE BROKERAGE CO LTD",
    "T.WARD & SON LTD",
    "TERRACYCLE UK LIMITED",
    "UK WASTE SOLUTIONS LTD",
    "UBT (EU) LTD",
    "VEOLIA ES (UK) LTD",
    "VERTO RECYCLE LTD",
    "WASTE MANAGEMENT FACILITIES LTD",
    "RECONOMY (UK) LTD",
    "WATERMAN WASTE MANAGEMENT LTD",
    "WASTENOT LTD",
    "WASTE WISE MANAGEMENT SOLUTIONS",
    "WEEE (SCOTLAND) LTD",
    "WM101 LTD",
    "WHITKIRK WASTE SOLUTIONS LTD",
    "WILLIAMS ENVIRONMENTAL MANAGEMENT LTD",
    "WASTESOLVE LIMITED",
    "WMR WASTE SOLUTIONS LTD",
    "WHEELDON BROTHERS WASTE LTD",
    "WASTE CLOUD LIMITED",
    "YORWASTE LTD",
    "YES WASTE LIMITED",
]

ADDRESS_OVERRIDES = {
    "BIFFA WASTE SERVICES LIMITED": "Biffa House, Rigby Court\nWokingham\nBerkshire\nRG41 5BN",
    "GO GREEN": "323 Bawtry Road\nDoncaster\nEngland DN4 7PB\nUnited Kingdom",
    "MITIE WASTE & ENVIRONMENTAL SERVICES LIMITED": "1 Bartholomew Lane\nLondon\nEC2N 2AX",
    "SUEZ RECYCLING AND RECOVERY UK LTD": "2100 Coventry Road\nSheldon\nBirmingham\nB26 3EA",
    "VEOLIA ES (UK) LTD": "Veolia House\n154A Pentonville Road\nLondon\nN1 9JE",
}

BROKERS = {name: ADDRESS_OVERRIDES.get(name, "") for name in ACCOUNT_NAMES}
BROKER_LIST_TEXT = ", ".join(BROKERS.keys())

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
