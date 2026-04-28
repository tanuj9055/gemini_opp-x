# 🎯 GeM Multi-Agent Audit Service

**Automated auditing of Government e-Marketplace (GeM) procurement documents** powered by a modular **Gemini 1.5 Pro** multi-agent architecture.

This service provides an intelligent pipeline to analyze tender documents, evaluate vendor eligibility, and classify items using advanced multimodal AI. It is designed to integrate seamlessly with a NestJS backend via RabbitMQ for asynchronous processing.

---

## 📋 Table of Contents
1. [Architecture Overview](#-architecture-overview)
2. [The Multi-Agent Pipeline](#-the-multi-agent-pipeline)
3. [Technology Stack](#-technology-stack)
4. [Project Structure](#-project-structure)
5. [Getting Started](#-getting-started)
6. [API Endpoints](#-api-endpoints)
7. [RabbitMQ Configuration](#-rabbitmq-configuration)

---

## 🏗️ Architecture Overview

The system follows a separate-of-concerns pattern, isolating AI-driven domain logic from infrastructure utilities.

- **FastAPI Layer**: Provides synchronous endpoints for testing and direct integration.
- **Agent Layer (`app/agents/`)**: Contains specialized AI agents for extraction, analysis, classification, and evaluation.
- **Service Layer (`app/services/`)**: Provides shared infrastructure utilities (Gemini API wrapper, prompt templates).
- **Worker Layer (`app/worker/`)**: Background consumers that process RabbitMQ jobs asynchronously.

---

## 🤖 The Multi-Agent Pipeline

The audit process is broken down into specialized agents that work together to ensure high accuracy and structured outputs.

| Agent | Module | Description |
| :--- | :--- | :--- |
| **Agent 1** | `rule_extractor.py` | Extracts structured eligibility criteria from complex bid PDFs. |
| **Agent 1b** | `filter_agent.py` | (Verifiable Filter) Separates checkable rules from narrative/non-verifiable ones. |
| **Agent 2** | `bid_analyzer.py` | Generates summaries, highlights, and insights for vendors. |
| **Agent 3** | `classification_agent.py` | Maps extracted criteria to specific fields in a customer's profile. |
| **Agent 4** | `evaluation_agent.py` | Performs pass/fail assessment based on provided customer data. |
| **HSN Agent** | `hsn_generator.py` | Classifies items into precise HSN product codes. |

---

## 🛠️ Technology Stack

| Layer | Technology |
| :--- | :--- |
| **Framework** | FastAPI (Python 3.10+) |
| **AI Model** | Google Gemini 1.5 Pro / 2.5 Pro |
| **Message Queue** | RabbitMQ (via `aio-pika`) |
| **PDF Processing** | `pypdf`, `reportlab` |
| **Data Validation** | Pydantic v2 |
| **AI Reliability** | `json-repair`, `tenacity` |

---

## 📁 Project Structure

```text
app/
├── agents/             # AI-driven domain logic (Agents 1-5)
│   ├── rule_extractor.py
│   ├── bid_analyzer.py
│   ├── hsn_generator.py
│   ├── filter_agent.py
│   └── ...
├── services/           # Infrastructure & Utilities
│   ├── gemini_client.py # Gemini Files API & Generation
│   └── prompts.py       # Centralized prompt templates
├── worker/             # RabbitMQ Consumers
│   ├── extraction_consumer.py
│   ├── analysis_consumer.py
│   └── ...
├── routers/            # HTTP Endpoints
│   ├── test_routes.py   # Development & Validation UI
│   └── hsn.py
├── config.py           # Configuration (Pydantic Settings)
├── schemas.py          # Unified Pydantic models
└── main.py             # Entry point & Lifespan management
```

---

## 🚀 Getting Started

### Prerequisites
- Python 3.9+
- RabbitMQ server
- Google Gemini API Key

### Installation

1. **Clone & Setup Environment**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # macOS/Linux
   .venv\Scripts\activate     # Windows
   pip install -r requirements.txt
   ```

2. **Configuration**
   Create a `.env` file in the root:
   ```env
   GOOGLE_API_KEY=your_key_here
   GEMINI_MODEL=gemini-1.5-pro
   RABBITMQ_URL=amqp://guest:guest@localhost
   ```

3. **Run the Service**
   ```bash
   uvicorn app.main:app --reload
   ```
   *The background RabbitMQ consumers will start automatically as part of the FastAPI lifespan.*

---

## 📡 API Endpoints

- **Health Check**: `GET /health`
- **Documentation**: `/docs` (Swagger UI)
- **HSN Generation**: `POST /generate-hsn`
- **Test Endpoints**: `/test/extract-rules`, `/test/analyze-bid`, `/test/full-eligibility`, etc.

---

## 🔄 RabbitMQ Configuration

The service consumes from several specialized queues and publishes results back to the NestJS orchestration layer.

- **Extraction**: `tender_extraction_jobs` -> `tender_extraction_results`
- **Analysis**: `tender_analysis_jobs` -> `tender_analysis_results`
- **HSN**: `hsn_requests_queue` -> `hsn_generation_results`
- **Classification**: `rule_classification_jobs` -> `rule_classification_results`
- **Evaluation**: `rule_evaluation_jobs` -> `rule_evaluation_results`

---

**Last Updated**: April 2026
**Architecture**: Agent 1-5 Modular Design
