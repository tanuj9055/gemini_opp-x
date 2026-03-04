# GeM Procurement Audit Service

Automated auditing of **Government e-Marketplace (GeM)** procurement documents
powered by **Gemini 1.5 Pro** multimodal AI.

---

## Quick Start

```bash
# 1. Create a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
copy .env.example .env
# Edit .env and set your GOOGLE_API_KEY

# 4. Run the server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Swagger UI → **http://localhost:8000/docs**
ReDoc      → **http://localhost:8000/redoc**

---

## Endpoints

| Method | Path               | Description                                      |
| ------ | ------------------ | ------------------------------------------------ |
| GET    | `/health`          | Health check                                     |
| POST   | `/analyze-bid`     | Upload a GeM Bid PDF → structured eligibility    |
| POST   | `/evaluate-vendor` | Bid JSON + vendor PDFs → scored vendor audit     |

### POST `/analyze-bid`

- **Input**: A single GeM Bid PDF (`multipart/form-data`, field: `file`).
- **Output**: JSON with eligibility criteria, EMD, scope of work, risks,
  bounding boxes, and full OCR text.

### POST `/evaluate-vendor`

- **Input** (multipart/form-data):
  - `bid_json` — stringified JSON from `/analyze-bid`.
  - `files` — 6-7 vendor PDFs (GST, PAN, balance sheets, work orders, etc.).
- **Output**: JSON with eligibility score (0-100), criterion verdicts,
  vendor profile, risks, and recommendation (APPROVE / REJECT / REVIEW).

---

## Architecture

```
app/
├── main.py                 # FastAPI app + lifespan
├── config.py               # pydantic-settings (env vars)
├── logging_cfg.py          # Structured logging
├── schemas.py              # Pydantic request / response models
├── routers/
│   ├── bid.py              # /analyze-bid endpoint
│   └── vendor.py           # /evaluate-vendor endpoint
└── services/
    ├── gemini_client.py    # Gemini Files API upload + generation
    └── prompts.py          # Prompt templates
```

---

## Key Design Decisions

| Decision                       | Rationale                                                        |
| ------------------------------ | ---------------------------------------------------------------- |
| No RAG / text-chunking         | Files uploaded to Gemini Files API; native multimodal reasoning.  |
| `response_mime_type=json`      | Forces the model to return parseable JSON directly.              |
| Bounding-box coordinates       | Every `reference` includes `(ymin, xmin, ymax, xmax)` for UI.   |
| Strict key ordering in schemas | Pydantic models enforce the canonical key order.                 |
| Async file handling            | `asyncio.to_thread` keeps the event loop non-blocking.           |

---

## Environment Variables

| Variable           | Required | Default           | Description                    |
| ------------------ | -------- | ----------------- | ------------------------------ |
| `GOOGLE_API_KEY`   | **Yes**  | —                 | Gemini API key                 |
| `GEMINI_MODEL`     | No       | `gemini-1.5-pro`  | Model name                     |
| `APP_ENV`          | No       | `development`     | `development` / `production`   |
| `LOG_LEVEL`        | No       | `DEBUG`           | Python log level               |
| `MAX_FILE_SIZE_MB` | No       | `50`              | Max upload size per file       |

---

## Troubleshooting

### Error: "Model gemini-1.5-pro is not found"

**Cause:** Invalid, revoked, or incorrectly configured API key.

**Fix:**

1. **Verify your API key is valid:**
   - Go to [Google AI Studio — API Keys](https://ai.google.dev/gemini-api/docs/api-key)
   - Create a new API key if needed (free tier is available)
   - Copy the full key (no spaces or line breaks) into your `.env` file

2. **Check available models:**
   ```bash
   # Activate venv first
   .venv\Scripts\Activate.ps1  # Windows
   
   # List available models
   python -c "
   from google import genai
   import os
   client = genai.Client(api_key=os.getenv('GOOGLE_API_KEY'))
   for model in client.models.list():
       print(model.name)
   "
   ```

3. **Try an alternative model** (if `gemini-1.5-pro` is unavailable):
   - `gemini-1.5-pro-latest`
   - `gemini-2.0-flash`
   - `gemini-1.5-flash`
   
   Update the `GEMINI_MODEL` variable in your `.env` and restart the server.

### File Upload Fails

- **Verify file format:** Only PDFs are accepted
- **Check file size:** Default limit is 50 MB (set `MAX_FILE_SIZE_MB` in `.env`)
- **Validate PDF:** Ensure the PDF is not corrupted

### Slow Response Times

- **Bid analysis:** 30–60 seconds (PDF pages + Gemini latency)
- **Vendor evaluation:** 60–120 seconds (6-7 documents cross-referenced)
- **Concurrent requests:** Service can handle ~50 concurrent requests

### Gemini API Rate Limits

- Free tier has generous limits (~1,500 requests/minute)
- Check your usage at [Google AI Studio — API Overview](https://ai.google.dev/gemini-api/docs/usage-limits)
- For production, consider upgrading or implementing request throttling
