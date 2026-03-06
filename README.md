# GeM Procurement Audit Service

Automated auditing of **Government e-Marketplace (GeM)** procurement documents
powered by **Gemini 1.5 Pro** multimodal AI.

---

## Quick Start

### Prerequisites
- Python 3.9+
- RabbitMQ running (default: `amqp://localhost`)
- Gemini API key (free tier available at https://ai.google.dev/gemini-api/docs/api-key)

### Setup on Your Machine

```bash
# 1. Clone the repository
git clone <your-repo-url>
cd gemini_opp-x

# 2. Create a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
copy .env.example .env
# Edit .env and set GOOGLE_API_KEY and RABBITMQ_URL

# 5. Run the FastAPI server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Swagger UI → **http://localhost:8000/docs**
ReDoc      → **http://localhost:8000/redoc**

### Setup on Friend's Machine (Different Environment)

Your friend can follow the same steps **WITHOUT manual data entry** because the system is event-driven:

1. **Clone your repo** from GitHub
2. **Install dependencies** (`pip install -r requirements.txt`)
3. **Set up `.env`** with their own credentials:
   - `GOOGLE_API_KEY` — their own Gemini API key
   - `RABBITMQ_URL` — point to shared RabbitMQ broker
   - `AWS_*` — optional, only if using S3 for documents
4. **Run the server** — same command as above
5. **NestJS publishes events** to RabbitMQ → Python consumes and processes automatically

**No manual user insertion needed!** All data flows via RabbitMQ events.

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

## Event-Driven Architecture (RabbitMQ)

### How It Works

This service is designed to integrate with a **NestJS backend** via RabbitMQ event streaming:

```
┌─────────────┐                           ┌──────────────┐
│   NestJS    │  Publishes "tender_apply"  │ analysis_ex  │
│  Backend    ├──────────────────────────→ │   (fanout)   │
└─────────────┘                           └──────────────┘
                                                ▼
                                         ┌──────────────┐
                                         │  Python App  │
                                         │  (Gemini AI) │
                                         └──────────────┘
                                                ▼
┌─────────────┐  Consumes "tender_result"  ┌──────────────┐
│   NestJS    │ ← ────────────────────────  │ analysis_res │
│  Backend    │                           │   (fanout)   │
└─────────────┘                           └──────────────┘
```

### RabbitMQ Setup

**For Development:**
```bash
# Using Docker (easiest)
docker run -d --hostname rabbitmq --name rabbitmq -p 5672:27015 -p 15672:15672 rabbitmq:4-management

# Access management UI at: http://localhost:15672
# Default credentials: guest / guest
```

**Or download** [RabbitMQ locally](https://www.rabbitmq.com/download.html)

### Message Contracts

**NestJS → Python** (published to `analysis_exchange`):
```json
{
  "type": "tender_apply",
  "bidNumber": "8481457",
  "bidUrl": "s3://bucket/path/bid.pdf",
  "companyDocuments": [
    {"documentType": "gst", "fileUrl": "s3://bucket/gst.pdf"},
    {"documentType": "pan", "fileUrl": "s3://bucket/pan.pdf"}
  ],
  "timestamp": "2026-03-06T12:00:00Z"
}
```

**Python → NestJS** (published to `analysis_results_exchange`):
```json
{
  "type": "tender_result",
  "bidNumber": "8481457",
  "status": "completed",
  "bid_analysis": { ... },
  "vendor_results": [ ... ],
  "processing_time_seconds": 147.02
}
```

---

## Architecture

```
app/
├── main.py                 # FastAPI app + lifespan (starts RabbitMQ consumer)
├── config.py               # pydantic-settings (env vars)
├── logging_cfg.py          # Structured logging
├── schemas.py              # Pydantic request / response models
├── routers/
│   ├── bid.py              # /analyze-bid endpoint
│   ├── vendor.py           # /evaluate-vendor endpoint
│   └── orchestrator.py     # Gemini orchestration logic
├── services/
│   ├── gemini_client.py    # Gemini Files API upload + generation
│   ├── rabbitmq_consumer.py# Event consumer (listens to NestJS)
│   ├── s3_client.py        # S3 document downloads
│   ├── human_readable.py   # Response formatting
│   └── prompts.py          # Prompt templates
└── worker/
    ├── main.py             # Standalone worker (queue-based)
    ├── consumer.py         # RabbitMQ queue consumer
    └── job_processor.py    # Job execution pipeline
```

### Components

| Component | Purpose |
|-----------|---------|
| `rabbitmq_consumer.py` | Listens to `analysis_exchange` (fanout) from NestJS, publishes results to `analysis_results_exchange` |
| `gemini_client.py` | Calls Gemini Files API for multimodal document analysis |
| `job_processor.py` | Pure business logic—orchestrates bid analysis + vendor evaluation |
| `s3_client.py` | Downloads PDFs from S3 (optional if using S3 for documents) |

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
