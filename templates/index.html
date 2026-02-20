from flask import Flask, request, jsonify, render_template
import anthropic
import base64
import os
import json

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20MB

# ─── Broker list with addresses ───────────────────────────────────────────────
# Update addresses here as you gather them. Address is newline-separated.
BROKERS = {
    "ACM Environmental PLC": "",
    "Acumen Waste Services": "",
    "707 Ltd / Click Waste": "",
    "Aqua Force Special Waste Ltd": "",
    "AMA Waste": "",
    "Associated Waste Management Ltd": "",
    "Asprey St John & Co Ltd": "",
    "Ash Waste Services Ltd": "",
    "ACMS Waste Limited": "",
    "A1 Chemical Waste Management Ltd": "",
    "Alchemy Metals Ltd": "",
    "Bakers Waste Services Ltd": "",
    "Biffa Waste Services Limited": "Biffa House, Rigby Court\nWokingham\nBerkshire\nRG41 5BN",
    "BW Skip Hire": "",
    "Bywaters (Leyton) Limited": "",
    "Bagnall & Morris Waste Services Ltd": "",
    "Baileys Skip Hire and Recycling Ltd": "",
    "Business Waste Ltd": "",
    "Brown Recycling Ltd": "",
    "BKP Waste & Recycling Ltd": "",
    "Belford Bros Skip Hire Ltd": "",
    "Countrystyle Recycling Ltd": "",
    "Cartwrights Waste Disposal Services": "",
    "C & M Waste Management Ltd": "",
    "Change Waste Recycling Limited": "",
    "Chloros Environmental Ltd": "",
    "CHC Waste FM Ltd": "",
    "Cleansing Service Group Ltd": "",
    "Circle Waste Ltd": "",
    "City Waste London Ltd": "",
    "Cheshire Waste Skip Hire Limited": "",
    "Circom Ltd": "",
    "CITB": "",
    "DP Skip Hire Ltd": "",
    "Forward Environmental Ltd": "",
    "EMN Plant Ltd": "",
    "E-Cycle Limited": "",
    "Enva England Ltd": "",
    "Ellgia Ltd": "",
    "Eco-Cycle Waste Management Ltd": "",
    "Enva WEEE Recycling Scotland Ltd": "",
    "FPWM Ltd / Footprint Recycling": "",
    "Forward Waste Management Ltd": "",
    "Fresh Start Waste Ltd": "",
    "Forvis Mazars LLP": "",
    "Greenzone Facilities Management Ltd": "",
    "Greenway Environmental Ltd": "",
    "GPT Waste Management Ltd": "",
    "Go Green": "323 Bawtry Road\nDoncaster\nEngland DN4 7PB\nUnited Kingdom",
    "Germstar UK Ltd": "",
    "GD Environmental Services Ltd": "",
    "Great Western Recycling Ltd": "",
    "Grundon Waste Management Ltd": "",
    "Gillett Environmental Ltd": "",
    "Go For It Trading Ltd": "",
    "Go 4 Greener Waste Management Ltd": "",
    "Intelligent Waste Management Limited": "",
    "J & B Recycling Ltd": "",
    "Just Clear Ltd": "",
    "Just A Step UK Ltd": "",
    "J Dickinson & Sons (Horwich) Limited": "",
    "Kenny Waste Management Ltd": "",
    "Kane Management Consultancy Ltd": "",
    "LSS Waste Management": "",
    "LTL Systems Ltd": "",
    "Mitie Waste & Environmental Services Limited": "1 Bartholomew Lane\nLondon\nEC2N 2AX",
    "MJ Church Recycling Ltd": "",
    "M & M Skip Hire Ltd": "",
    "MVV Environment": "",
    "Mick George Recycling Ltd": "",
    "NWH Waste Services": "",
    "Nationwide Waste Services Limited": "",
    "Optima Health UK Ltd": "",
    "Premier Waste Recycling Ltd": "",
    "Pearce Recycling Company Ltd": "",
    "Phoenix Environmental Management Ltd": "",
    "Papilo Ltd": "",
    "RFMW UK Ltd": "",
    "Riverdale Paper PLC": "",
    "Remondis Ltd": "",
    "Roydon Resource Recovery Limited": "",
    "Risinxtreme Limited": "",
    "Recorra Ltd": "",
    "Sackers Ltd": "",
    "Suez Recycling and Recovery UK Ltd": "2100 Coventry Road\nSheldon\nBirmingham\nB26 3EA",
    "Safety Kleen UK Limited": "",
    "Mobius Environmental Ltd": "",
    "Sustainable Waste Services": "",
    "Select A Skip UK Ltd": "",
    "Slicker Recycling Ltd": "",
    "Sommers Waste Solutions Limited": "",
    "Saica Natur UK Ltd": "",
    "Site Clear Solutions Ltd": "",
    "Sharp Brothers (Skips) Ltd": "",
    "SLM Waste Management Limited": "",
    "Shredall (East Midlands) Limited": "",
    "Scott Waste Limited": "",
    "Smiths (Gloucester) Ltd": "",
    "Tradebe North West Ltd": "",
    "The Waste Brokerage Co Ltd": "",
    "T.Ward & Son Ltd": "",
    "Terracycle UK Limited": "",
    "UK Waste Solutions Ltd": "",
    "UBT (EU) Ltd": "",
    "Veolia ES (UK) Ltd": "Veolia House\n154A Pentonville Road\nLondon\nN1 9JE",
    "Verto Recycle Ltd": "",
    "Waste Management Facilities Ltd": "",
    "Reconomy (UK) Ltd": "",
    "Waterman Waste Management Ltd": "",
    "Wastenot Ltd": "",
    "Waste Wise Management Solutions": "",
    "WEEE (Scotland) Ltd": "",
    "WM101 Ltd": "",
    "Whitkirk Waste Solutions Ltd": "",
    "Williams Environmental Management Ltd": "",
    "Wastesolve Limited": "",
    "WMR Waste Solutions Ltd": "",
    "Wheeldon Brothers Waste Ltd": "",
    "Waste Cloud Limited": "",
    "Yorwaste Ltd": "",
    "Yes Waste Limited": "",
}

EXTRACT_PROMPT = """You are extracting data from a supplier purchase order PDF sent to Waste Experts.

STEP 1 — IDENTIFY THE SUPPLIER (Broker)
Scan the ENTIRE document — logo, header, footer, terms, email addresses, phrases like "X Ltd employee", "email to X", "accept X terms & conditions", "Registered Office" — and match against this approved broker list:

ACM Environmental PLC, Acumen Waste Services, 707 Ltd, Click Waste, Aqua Force Special Waste Ltd, AMA Waste, Associated Waste Management Ltd, Asprey St John & Co Ltd, Ash Waste Services Ltd, ACMS Waste Limited, A1 Chemical Waste Management Ltd, Alchemy Metals Ltd, Bakers Waste Services Ltd, Biffa Waste Services Limited, BW Skip Hire, Bywaters (Leyton) Limited, Bagnall & Morris Waste Services Ltd, Baileys Skip Hire and Recycling Ltd, Business Waste Ltd, Brown Recycling Ltd, BKP Waste & Recycling Ltd, Belford Bros Skip Hire Ltd, Countrystyle Recycling Ltd, Cartwrights Waste Disposal Services, C & M Waste Management Ltd, Change Waste Recycling Limited, Chloros Environmental Ltd, CHC Waste FM Ltd, Cleansing Service Group Ltd, Circle Waste Ltd, City Waste London Ltd, Cheshire Waste Skip Hire Limited, Circom Ltd, CITB, DP Skip Hire Ltd, Forward Environmental Ltd, EMN Plant Ltd, E-Cycle Limited, Enva England Ltd, Ellgia Ltd, Eco-Cycle Waste Management Ltd, Enva WEEE Recycling Scotland Ltd, FPWM Ltd, Footprint Recycling, Forward Waste Management Ltd, Fresh Start Waste Ltd, Forvis Mazars LLP, Greenzone Facilities Management Ltd, Greenway Environmental Ltd, GPT Waste Management Ltd, Go Green, Germstar UK Ltd, GD Environmental Services Ltd, Great Western Recycling Ltd, Grundon Waste Management Ltd, Gillett Environmental Ltd, Go For It Trading Ltd, Go 4 Greener Waste Management Ltd, Intelligent Waste Management Limited, J & B Recycling Ltd, Just Clear Ltd, Just A Step UK Ltd, J Dickinson & Sons (Horwich) Limited, Kenny Waste Management Ltd, Kane Management Consultancy Ltd, LSS Waste Management, LTL Systems Ltd, Mitie Waste & Environmental Services Limited, Mitie, MJ Church Recycling Ltd, M & M Skip Hire Ltd, MVV Environment, Mick George Recycling Ltd, NWH Waste Services, Nationwide Waste Services Limited, Optima Health UK Ltd, Premier Waste Recycling Ltd, Pearce Recycling Company Ltd, Phoenix Environmental Management Ltd, Papilo Ltd, RFMW UK Ltd, Riverdale Paper PLC, Remondis Ltd, Roydon Resource Recovery Limited, Risinxtreme Limited, Recorra Ltd, Sackers Ltd, Suez Recycling and Recovery UK Ltd, Suez, Safety Kleen UK Limited, Mobius Environmental Ltd, Sustainable Waste Services, Select A Skip UK Ltd, Slicker Recycling Ltd, Sommers Waste Solutions Limited, Saica Natur UK Ltd, Site Clear Solutions Ltd, Sharp Brothers (Skips) Ltd, SLM Waste Management Limited, Shredall (East Midlands) Limited, Scott Waste Limited, Smiths (Gloucester) Ltd, Tradebe North West Ltd, The Waste Brokerage Co Ltd, T.Ward & Son Ltd, Terracycle UK Limited, UK Waste Solutions Ltd, UBT (EU) Ltd, Veolia ES (UK) Ltd, Verto Recycle Ltd, Waste Management Facilities Ltd, Reconomy (UK) Ltd, Waterman Waste Management Ltd, Wastenot Ltd, Waste Wise Management Solutions, WEEE (Scotland) Ltd, WM101 Ltd, Whitkirk Waste Solutions Ltd, Williams Environmental Management Ltd, Wastesolve Limited, WMR Waste Solutions Ltd, Wheeldon Brothers Waste Ltd, Waste Cloud Limited, Yorwaste Ltd, Yes Waste Limited

Set "supplier" to the BEST MATCHING name from the list. NEVER use: Waste Experts, Electrical Waste Recycling Group, or the waste producer/site company.

STEP 2 — Extract all fields and return ONLY a valid JSON object:

{
  "supplier": "Best matching broker name from the approved list, or null",
  "purchase_order_number": "The PO or order reference number",
  "site_contact": "Primary site contact full name, or null",
  "site_contact_number": "Primary site contact phone number, or null",
  "site_contact_email": "Primary site contact email address, or null",
  "secondary_site_contact": "Secondary/alternative contact full name, or null",
  "secondary_site_contact_number": "Secondary contact phone number, or null",
  "secondary_site_contact_email": "Secondary contact email address, or null",
  "site_name": "Name of the collection/service site (e.g. 'Garic Hire Ltd - Sandy')",
  "site_address": "Full site street address, newline-separated (do NOT include postcode here)",
  "site_postcode": "Site postcode only (e.g. SG19 1QY)",
  "opening_times": "Site opening hours if explicitly stated, or null",
  "access": "Access time window and day restrictions (e.g. '8am - 5pm Monday - Friday'), or null",
  "site_restrictions": "PPE requirements, vehicle restrictions, height restrictions, or other site-specific rules, or null",
  "special_instructions": "Order notes, amended orders, call-ahead instructions, or any other important notes, or null",
  "waste_type": "Waste type or material as stated on the PO",
  "ewc_code": "EWC code if present (e.g. '20.01.21*'). Codes ending in * are hazardous.",
  "container_type": "Container type and size (e.g. 'Corrugated Flo Tube Pipe', '8ft Dura Pipe')",
  "movement_type": "Movement type (e.g. 'Exchange', 'Collection', 'Removal')",
  "transport_cost": 0.00,
  "note_already_included": true
}

RULES:
- note_already_included: true if transport/service cost already includes consignment or transfer note fee. false if note must be added separately.
- Use numeric type (not string) for transport_cost.
- Return ONLY the JSON object — no markdown fences, no explanation."""


@app.route('/')
def index():
    return render_template('index.html', brokers=json.dumps(list(BROKERS.keys())))


@app.route('/extract', methods=['POST'])
def extract():
    if 'pdf' not in request.files:
        return jsonify({'error': 'No PDF uploaded'}), 400

    pdf_file = request.files['pdf']
    pdf_bytes = pdf_file.read()
    b64 = base64.standard_b64encode(pdf_bytes).decode()

    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'ANTHROPIC_API_KEY not set'}), 500

    client = anthropic.Anthropic(api_key=api_key)

    try:
        resp = client.messages.create(
            model='claude-opus-4-6',
            max_tokens=2000,
            messages=[{
                'role': 'user',
                'content': [
                    {
                        'type': 'document',
                        'source': {
                            'type': 'base64',
                            'media_type': 'application/pdf',
                            'data': b64,
                        },
                    },
                    {'type': 'text', 'text': EXTRACT_PROMPT},
                ],
            }],
        )

        raw = resp.content[0].text.strip()
        if '```' in raw:
            raw = raw[raw.find('{'):raw.rfind('}') + 1]

        data = json.loads(raw)

        supplier = data.get('supplier') or ''
        data['supplier_address'] = BROKERS.get(supplier, '')
        data['supplier_found'] = bool(supplier and supplier in BROKERS)

        return jsonify({'success': True, 'data': data})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/broker-address', methods=['GET'])
def broker_address():
    name = request.args.get('name', '')
    address = BROKERS.get(name, '')
    return jsonify({'name': name, 'address': address})


@app.route('/brokers', methods=['GET'])
def get_brokers():
    return jsonify({'brokers': [{'name': k, 'address': v} for k, v in BROKERS.items()]})


if __name__ == '__main__':
    app.run(debug=True)