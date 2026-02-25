# Supplier Order Extractor
## Setup & Deployment Guide

Upload a supplier purchase order PDF (Go Green, Mitie, Suez, etc.) and get all key fields extracted instantly, ready to paste into HubSpot or download as a CSV.  

---

## What It Extracts

From any broker purchase order PDF:

| Field | Example |
|-------|---------|
| Client / Broker | Go Green |
| Order Number | 26131771615 |
| Job Date | 16/02/2026 08:00 |
| Movement Type | Exchange |
| Container Type | Flo Tube - Corrugated Flo Tube Pipe |
| Waste Type | Fluorescent tubes and other mercury-containing waste |
| EWC Code | 20.01.21* |
| Waste Producer | GARIC LIMITED |
| Site Name | Garic Hire Ltd - Sandy |
| Site Address | 74 Sunderland Road, Sandy |
| Site Postcode | SG19 1QY |
| Site Contact | Andrew Owen |
| Contact Number | 07384 514076 |
| Access Times | 8am - 5pm Monday - Friday |
| Transport Cost | £180.03 |
| WCL / WML Numbers | CBDU166985 / WEX361400 |
| Facility Name | Electrical Waste - Anglia Cargo Terminal |
| Order Notes | Special instructions |

---

## Deploy to Azure App Service (Recommended)

This keeps everything consistent with your quote automation system.

### Step 1: Install prerequisites
```bash
# Azure CLI
https://docs.microsoft.com/en-us/cli/azure/install-azure-cli

# Python 3.11+
https://www.python.org/downloads/
```

### Step 2: Login and create resources
```bash
az login

az group create \
  --name pdf-extractor-rg \
  --location uksouth

az appservice plan create \
  --name pdf-extractor-plan \
  --resource-group pdf-extractor-rg \
  --sku B1 \
  --is-linux

az webapp create \
  --name waste-experts-extractor \
  --resource-group pdf-extractor-rg \
  --plan pdf-extractor-plan \
  --runtime "PYTHON:3.11"
```

### Step 3: Set your API key
```bash
az webapp config appsettings set \
  --name waste-experts-extractor \
  --resource-group pdf-extractor-rg \
  --settings ANTHROPIC_API_KEY="your-anthropic-api-key-here"
```

### Step 4: Set startup command
```bash
az webapp config set \
  --name waste-experts-extractor \
  --resource-group pdf-extractor-rg \
  --startup-file "gunicorn --bind=0.0.0.0:8000 --timeout 120 --workers 2 app:app"
```

### Step 5: Deploy the code
```bash
# From inside the pdf-extractor folder:
zip -r deploy.zip . -x "*.git*" -x "__pycache__/*" -x "*.pyc"

az webapp deploy \
  --name waste-experts-extractor \
  --resource-group pdf-extractor-rg \
  --src-path deploy.zip \
  --type zip
```

### Step 6: Open in browser
```
https://waste-experts-extractor.azurewebsites.net
```

---

## Test Locally First

```bash
cd pdf-extractor
pip install -r requirements.txt
export ANTHROPIC_API_KEY="your-key-here"
python app.py
# Visit http://localhost:5000
```

---

## Cost Estimate (Azure, monthly)

| Service | Cost |
|---------|------|
| App Service B1 (Basic) | ~£11/month |
| Anthropic API (~200 PDFs/month) | ~£1–2 |
| **Total** | **~£12–13/month** |

> You can drop to the **F1 Free tier** for testing, but it sleeps after 20 mins of inactivity (slow first load).

---

## File Structure

```
pdf-extractor/
├── app.py              # Flask app — upload, extract, download
├── requirements.txt    # Python dependencies
├── startup.txt         # Azure startup command (reference)
├── README.md           # This file
└── templates/
    └── index.html      # Full frontend UI
```

---

## Adding New Brokers

The AI automatically adapts to any broker's PDF format. No configuration needed — just upload and it extracts. If a field isn't on a particular PO format, it returns "Not found" and flags it for review.
