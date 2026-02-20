# CLAUDE.md

## Project Overview

**Supplier Purchase Order PDF Extractor** for Waste Experts — a Flask web application that uses Claude AI to extract structured data from waste management supplier purchase order PDFs. Users upload PDFs, review/edit extracted fields in a web UI, and download review PDFs.

## Tech Stack

- **Backend:** Python 3.11+, Flask 3.1.0
- **AI:** Anthropic Claude API (`anthropic` 0.40.0) — `claude-opus-4-1` in `app.py`, `claude-opus-4-6` in `generate_quote.py`
- **PDF Generation:** ReportLab 4.4.10, Pillow 12.1.1
- **Production Server:** Gunicorn 23.0.0
- **Frontend:** Vanilla HTML/CSS/JavaScript (no framework), templates in `templates/`
- **Deployment:** Azure App Service (Python 3.11, B1 SKU)

## Repository Structure

```
pdf-extractor/
├── app.py                  # Flask web app — routes, Claude extraction, PDF generation
├── brokers.py              # Broker master data (123 approved supplier names + addresses)
├── generate_quote.py       # Standalone CLI tool — generates branded Waste Experts quotes
├── requirements.txt        # Pinned Python dependencies
├── Procfile                # Heroku-style startup command
├── startup.txt             # Azure startup command reference
├── README.md               # Setup & deployment guide
├── fonts/                  # Montserrat TTF font files (used by generate_quote.py)
│   ├── Montserrat-Regular.ttf
│   ├── Montserrat-SemiBold.ttf
│   ├── Montserrat-Bold.ttf
│   └── Montserrat-ExtraBold.ttf
└── templates/
    ├── index.html          # Main UI — PDF upload, data review form, PDF download
    └── quote.html          # Quote generator UI (partial)
```

## Key Files

### `app.py` — Main Web Application

Flask app with these routes:

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Render upload/review UI with broker list |
| `/extract` | POST | Accept PDF upload, call Claude API, return extracted JSON |
| `/brokers` | GET | Return full broker list as JSON |
| `/broker-address` | GET | Look up a single broker's address by name |
| `/save-review` | POST | Persist reviewed data to in-memory global dict |
| `/download-review-pdf` | POST | Generate and return a ReportLab PDF of reviewed data |

Key internal functions:
- `_build_review_pdf(payload)` — generates a review PDF from extracted fields
- `_clean_json_payload(raw_text)` — parses Claude's response, handles markdown-wrapped JSON
- `_normalise_data(data)` — validates supplier against broker list, sets defaults, orders fields

### `brokers.py` — Broker Master Data

- `ACCOUNT_NAMES` — list of 123 approved waste supplier names
- `ADDRESS_OVERRIDES` — manual address entries for 5 major brokers (Biffa, Go Green, Mitie, Suez, Veolia)
- `BROKERS` — dict mapping name → address (used for lookups and validation)
- `BROKER_LIST_TEXT` — comma-separated names injected into the Claude prompt

### `generate_quote.py` — Quote Generator CLI

Standalone script (~1089 lines) that:
1. Reads a supplier PO PDF
2. Extracts data via Claude (`claude-opus-4-6`)
3. Detects hazardous waste (20+ keywords + EWC codes ending in `*`)
4. Generates a branded Waste Experts quote PDF with Montserrat fonts

Run with: `python generate_quote.py`

## Development Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."
python app.py
# Visit http://localhost:5000
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key for Claude. App returns 500 if missing. |

The `.env` file is gitignored. No `.env.example` exists — set the variable directly in the shell or deployment config.

## Production Deployment

Azure App Service with:
```
gunicorn --bind=0.0.0.0:8000 --timeout 120 --workers 2 app:app
```

Deploy via zip upload:
```bash
zip -r deploy.zip . -x "*.git*" -x "__pycache__/*" -x "*.pyc"
az webapp deploy --name waste-experts-extractor --resource-group pdf-extractor-rg --src-path deploy.zip --type zip
```

## Data Flow

```
PDF Upload → Base64 encode → Claude API (with extraction prompt + broker list)
  → JSON response → _clean_json_payload() → _normalise_data()
  → Broker validation (match against BROKERS dict)
  → Return 19-field ordered JSON to frontend
  → User reviews/edits in UI
  → Save to memory OR download as review PDF
```

## Extracted Fields

The extraction pipeline returns these 19 fields:
`account_name`, `supplier`, `purchase_order_number`, `site_contact`, `site_contact_number`, `site_contact_email`, `secondary_site_contact`, `secondary_site_contact_number`, `secondary_site_contact_email`, `site_name`, `site_address`, `site_postcode`, `opening_times`, `access`, `site_restrictions`, `special_instructions`, `document_type`, `supplier_address`, `supplier_found`

## Code Conventions

- **No test framework** — no pytest, unittest, or test directory exists
- **No linter/formatter config** — no flake8, black, pyproject.toml, or similar
- **No CI/CD pipeline** — deployment is manual via Azure CLI
- **No type hints** in `app.py`; `generate_quote.py` has some inline type annotations
- **Dependencies are pinned** to exact versions in `requirements.txt`
- **In-memory state** — `LAST_REVIEW_PAYLOAD` global dict stores the last reviewed extraction (not persistent, not thread-safe)
- **Flask debug mode** is enabled in `app.run(debug=True)` — the dev entry point at `app.py:256`
- **Max upload size** is 20 MB (`app.config["MAX_CONTENT_LENGTH"]`)

## Common Tasks

### Adding a new broker
Add the broker name (uppercase) to `ACCOUNT_NAMES` in `brokers.py`. If a specific address is needed, also add an entry to `ADDRESS_OVERRIDES`.

### Changing the extraction prompt
The `EXTRACT_PROMPT` variable is defined at `app.py:25`. It includes the broker list and a JSON schema for the expected output.

### Modifying the review PDF layout
Edit `_build_review_pdf()` at `app.py:60`. It uses ReportLab's `canvas.Canvas` API with Helvetica fonts.

### Changing the Claude model
- Web app extraction: `app.py:218` — currently `claude-opus-4-1`
- Quote generation: `generate_quote.py` — currently `claude-opus-4-6`

## Known Limitations

- **No authentication** — all endpoints are publicly accessible
- **In-memory storage** — `LAST_REVIEW_PAYLOAD` is not persistent across restarts and not thread-safe for concurrent users
- **Single broker address** per company — no facility/branch management
- **Claude model is hardcoded** — not configurable via environment variables
- **Duplicate imports** at top of `app.py` (lines 1-5 and 11-14)
