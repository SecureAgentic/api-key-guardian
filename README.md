# 🛡️ API Key Guardian — Gemini Safety Net

> **A real-time safety net for Google Gemini API keys** — automatically deletes compromised keys before runaway spend drains your wallet.

Casual developers often accidentally leak API keys in public repos or client-side code. Within minutes, bots scrape GitHub and abuse those keys. GCP budget alerts can take **24 to 48 hours** to notify you, meaning you could be hit with a **$7,000+ surprise bill** before you realize it.

API Key Guardian is a lightweight, thread-safe proxy that sits in front of the Gemini API. It tracks token usage and USD spend in real-time, **permanently deleting the API key from GCP** the moment a threshold is crossed. It limits your worst-case exposure to a few single-digit dollars.

---

## How It Works

```
Your App  →  API Key Guardian (Cloud Run)  →  Gemini API
                       │
            ┌──────────▼──────────┐
            │  Enforce Dual-Guard:│
            │  Spend ≥ $1.00 USD  │  → 402 + API key
            │         OR          │     auto-deleted in GCP
            │  Tokens ≥ 10M today │
            └──────────▼──────────┘
                       │
                Forward to Gemini ✅
```

Your real API key **never leaves the proxy** — it is stored securely in GCP Secret Manager and injected at runtime.

---

## Features

*   **USD Spend Limit**: Shuts down the key if estimated USD spend exceeds your cap (Default: `$1.00`).
*   **Token Count Limit**: A backup ceiling (Default: `10,000,000` tokens) that protects you even if pricing rates change or you switch models.
*   **UTC Date Reset**: Automatically resets spend and token counters at UTC midnight, allowing the proxy to act as a rolling daily budget.
*   **Thread-Safe State**: Uses locking mechanisms combined with Gunicorn thread pools to ensure accurate and secure memory state.
*   **Secured Status Endpoint**: Securely query `/status` using an optional access token to view daily metrics.

---

## Prerequisites

- [GCP project](https://console.cloud.google.com) with billing enabled.
- [`gcloud` CLI](https://cloud.google.com/sdk/docs/install) installed and authenticated.
- Gemini API key stored in **Secret Manager** (see below).
- APIs enabled:
  ```bash
  gcloud services enable \
    run.googleapis.com \
    apikeys.googleapis.com \
    secretmanager.googleapis.com
  ```
- **IAM Permission**: The Cloud Run service account must have the **API Keys Admin** role (`roles/apikeys.admin`) or a custom role with `apikeys.keys.delete` to authorize it to delete the key on breach.

---

## Quick Start

### 1. Store your API key in Secret Manager

```bash
echo -n "AIza..." | gcloud secrets create gemini-api-key \
  --data-file=- \
  --project=YOUR_PROJECT_ID
```

### 2. Get your API Key resource ID

```bash
# List keys and grab the ID (e.g., projects/123/locations/global/keys/<API_KEY_ID>)
gcloud services api-keys list --project=YOUR_PROJECT_ID
```

### 3. Deploy to Cloud Run

Deploy with `--min-instances 0` to enable **scale-to-zero** (you pay nothing when the proxy is idle).

```bash
gcloud run deploy api-key-guardian \
  --source . \
  --region us-central1 \
  --project YOUR_PROJECT_ID \
  --set-env-vars \
    PROJECT_ID=YOUR_PROJECT_ID,\
    API_KEY_ID=YOUR_KEY_ID,\
    BLOCK_BUDGET_USD=1.0,\
    BLOCK_BUDGET_TOKENS=10000000,\
    GEMINI_MODEL=gemini-2.0-flash \
  --set-secrets GEMINI_API_KEY=gemini-api-key:latest \
  --allow-unauthenticated \
  --memory 256Mi \
  --cpu 1 \
  --concurrency 10 \
  --min-instances 0 \
  --max-instances 3 \
  --timeout 35
```

### 4. Update your App Code

Configure your application to direct Gemini API requests to the proxy URL instead of hitting Google directly.

```python
import httpx

GUARDIAN_URL = "https://YOUR-GUARDIAN-URL.run.app"

response = httpx.post(
    f"{GUARDIAN_URL}/generate",
    json={
        "contents": [{"parts": [{"text": "Write a haiku about security."}]}]
    }
)
print(response.json())
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GEMINI_API_KEY` | ✅ | — | Real Gemini API key (inject via `--set-secrets`). |
| `PROJECT_ID` | ✅ | — | GCP project ID where the key resides. |
| `API_KEY_ID` | ✅ | — | GCP API key resource ID (UUID string) to delete. |
| `BLOCK_BUDGET_USD` | ❌ | `1.0` | Daily USD spend limit. |
| `BLOCK_BUDGET_TOKENS` | ❌ | `10000000` | Daily token safety cap limit. |
| `GEMINI_MODEL` | ❌ | `gemini-2.0-flash` | Downstream Gemini model. |
| `COST_PER_1M_INPUT_USD` | ❌ | `0.075` | Input price per 1M tokens. |
| `COST_PER_1M_OUTPUT_USD` | ❌ | `0.30` | Output price per 1M tokens. |
| `STATUS_TOKEN` | ❌ | — | If provided, secures `/status` behind a header/param check. |

---

## API Reference

### `POST /generate`

Proxies request to the Gemini API, tracking tokens and enforcing safety thresholds.

*   **Request body**: Identical to standard [Gemini generateContent API](https://ai.google.dev/api/generate-content).
*   **Response**: Passed through directly from Gemini.
*   **Errors**:
    *   `402 Payment Required`: Daily safety limit reached. Key has been deleted in GCP.
    *   `504 Gateway Timeout`: Gemini upstream timeout.

### `GET /status`

Queries current usage metrics.

*   **Authentication**: If `STATUS_TOKEN` env var is configured, you must supply it in the header `X-Status-Token: <token>` or parameter `?token=<token>`.

```json
{
  "status": "active",
  "model": "gemini-2.0-flash",
  "daily_spend_usd": 0.052,
  "daily_tokens": 42000,
  "budget_limit_usd": 1.0,
  "budget_limit_tokens": 10000000
}
```

---

## Cost to Run (GCP Pricing)

Because Cloud Run supports **scaling to zero**, running this proxy will cost you **$0.00** per month under normal test usage, fitting comfortably within the GCP Free Tier.

---

## License

Licensed under the **Apache License 2.0** — see [`LICENSE`](./LICENSE) for full terms.
