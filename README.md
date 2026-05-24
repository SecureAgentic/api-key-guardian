# 🛡️ API Key Guardian

> **Automated protection proxy for Google Gemini API keys** — stops abuse, rate-limit attacks, and runaway spend before they cost you money.

Casual developers often accidentally expose API keys in public repos or client-side code. Within minutes, bots scrape GitHub and start abusing those keys. API Key Guardian sits as a lightweight proxy between your app and the Gemini API, **automatically blocking threats with zero manual intervention**.

---

## How It Works

```
Your App  →  API Key Guardian (Cloud Run)  →  Gemini API
                      │
           ┌──────────▼──────────┐
           │  Block 1: IP on     │  → 429 Blocked
           │  blocklist?         │
           └──────────▼──────────┘
           ┌──────────▼──────────┐
           │  Block 2: Rate      │  → 429 + IP auto-blocked
           │  > 60 req/min?      │     permanently
           └──────────▼──────────┘
           ┌──────────▼──────────┐
           │  Block 3: Spend     │  → 402 + API key
           │  ≥ $10 today?       │     auto-revoked in GCP
           └──────────▼──────────┘
                      │
               Forward to Gemini ✅
```

Your real API key **never leaves the proxy** — it's stored in GCP Secret Manager and injected at runtime.

---

## Features

| Protection | Mechanism | Default |
|---|---|---|
| **Rate limiting** | Sliding window per IP | 60 req/min |
| **IP auto-block** | Permanent ban on breach | Instant |
| **Spend warning** | Log alert at 80% of budget | $8 (of $10) |
| **Spend hard cap** | Auto-revokes GCP API key | $10/day |
| **Status endpoint** | Live health + spend state | `/status` |
| **Secret storage** | Key never in env plain text | Secret Manager |

---

## Prerequisites

- [GCP project](https://console.cloud.google.com) with billing enabled
- [`gcloud` CLI](https://cloud.google.com/sdk/docs/install) authenticated
- Gemini API key stored in **Secret Manager** (see setup below)
- Cloud Run, Cloud API Keys, Secret Manager APIs enabled

```bash
gcloud services enable \
  run.googleapis.com \
  apikeys.googleapis.com \
  secretmanager.googleapis.com
```

---

## Quick Start

### 1. Store your API key in Secret Manager

```bash
# Never pass secrets as plain env vars
echo -n "AIza..." | gcloud secrets create gemini-api-key \
  --data-file=- \
  --project=YOUR_PROJECT_ID
```

### 2. Get your API Key resource ID

```bash
# List keys and grab the KEY_ID (not the key value)
gcloud services api-keys list --project=YOUR_PROJECT_ID
# Output: NAME: projects/123/locations/global/keys/abc-123-KEY_ID
```

### 3. Deploy to Cloud Run

```bash
gcloud run deploy api-key-guardian \
  --source . \
  --region us-central1 \
  --project YOUR_PROJECT_ID \
  --set-env-vars \
    PROJECT_ID=YOUR_PROJECT_ID,\
    API_KEY_ID=YOUR_KEY_ID,\
    RATE_LIMIT_RPM=60,\
    BLOCK_BUDGET_USD=10 \
  --set-secrets GEMINI_API_KEY=gemini-api-key:latest \
  --allow-unauthenticated \
  --min-instances 1 \
  --max-instances 10
```

> ⚠️ `--set-secrets` mounts the key securely from Secret Manager — **do not use `--set-env-vars` for `GEMINI_API_KEY`**.

### 4. Update your app to use the proxy

```python
# Before (vulnerable — key in code):
import google.generativeai as genai
genai.configure(api_key="AIza...")

# After (protected — key never touches your app):
import httpx

GUARDIAN_URL = "https://YOUR-GUARDIAN-URL.run.app"

response = httpx.post(
    f"{GUARDIAN_URL}/generate",
    json={
        "contents": [{"parts": [{"text": "Hello!"}]}]
    }
)
print(response.json())
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GEMINI_API_KEY` | ✅ | — | Your Gemini API key (inject via `--set-secrets`) |
| `PROJECT_ID` | ✅ | — | GCP project ID |
| `API_KEY_ID` | ✅ | — | GCP API Key resource ID (for auto-revocation) |
| `RATE_LIMIT_RPM` | ❌ | `60` | Max requests per IP per minute |
| `BLOCK_BUDGET_USD` | ❌ | `10` | Daily spend hard cap in USD |
| `PORT` | ❌ | `8080` | HTTP port (Cloud Run sets this automatically) |

---

## API Reference

### `POST /generate`

Proxies your request to the Gemini API with full protection applied.

**Request body** — identical to the [Gemini generateContent API](https://ai.google.dev/api/generate-content):

```json
{
  "contents": [
    { "parts": [{ "text": "Your prompt here" }] }
  ]
}
```

**Response** — identical to Gemini API response, passed through transparently.

**Error responses:**

| Status | Reason |
|---|---|
| `429` | IP rate-limited or permanently blocked |
| `402` | Daily budget exceeded — key revoked |
| `504` | Upstream Gemini API timeout |

---

### `GET /status`

Returns current guardian state. Useful for monitoring and alerting.

```bash
curl https://YOUR-GUARDIAN-URL.run.app/status
```

```json
{
  "status": "active",
  "daily_spend_usd": 3.2041,
  "budget_limit": 10.0,
  "budget_pct": 32.0,
  "blocked_ips": ["1.2.3.4"],
  "rate_limit_rpm": 60
}
```

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    GCP Project                      │
│                                                     │
│  ┌──────────────┐     ┌─────────────────────────┐  │
│  │  Cloud Run   │────▶│   Secret Manager        │  │
│  │  (Guardian)  │     │   gemini-api-key        │  │
│  └──────┬───────┘     └─────────────────────────┘  │
│         │                                           │
│         │ auto-revoke on budget breach              │
│         ▼                                           │
│  ┌──────────────┐                                   │
│  │  GCP API     │                                   │
│  │  Keys API    │                                   │
│  └──────────────┘                                   │
└─────────────────────────────────────────────────────┘
         │
         ▼ (only after all checks pass)
  Gemini API (generativelanguage.googleapis.com)
```

---

## Production Recommendations

> The defaults are intentionally simple for easy onboarding. For production traffic, apply these hardening steps.

### Replace in-memory state with Redis

The current rate-limit and blocked-IP tracking is **per-instance in memory**. With multiple Cloud Run instances, each instance has its own state — meaning a single attacker could bypass rate limits by hitting different instances.

**Fix:** Use [Cloud Memorystore (Redis)](https://cloud.google.com/memorystore/docs/redis/redis-overview) for shared state:

```python
import redis
r = redis.Redis(host=os.environ["REDIS_HOST"], port=6379)

def check_rate_limit(ip: str) -> bool:
    key = f"rpm:{ip}"
    count = r.incr(key)
    if count == 1:
        r.expire(key, 60)      # 60-second sliding window
    if count >= RATE_LIMIT_RPM:
        r.sadd("blocked_ips", ip)
        return True
    return False
```

### Add Cloud Armor in front of Cloud Run

For DDoS-level protection (volumetric attacks that overwhelm the proxy itself), put [Cloud Armor](https://cloud.google.com/armor/docs) behind a GCP Load Balancer in front of the Cloud Run service.

```bash
# Create security policy with rate limiting
gcloud compute security-policies create guardian-armor
gcloud compute security-policies rules create 1000 \
  --security-policy guardian-armor \
  --action rate-based-ban \
  --rate-limit-threshold-count 200 \
  --rate-limit-threshold-interval-sec 60 \
  --ban-duration-sec 3600 \
  --src-ip-ranges "*"
```

### Enable GCP Budget Alerts as a second layer

The in-proxy spend tracking resets on service restart. Add a [GCP Billing Budget](https://cloud.google.com/billing/docs/how-to/budgets) as a persistent, independent kill switch:

```bash
gcloud billing budgets create \
  --billing-account=YOUR_BILLING_ACCOUNT \
  --display-name="Guardian-Backup-Budget" \
  --budget-amount=10USD \
  --threshold-rule=percent=1.0,basis=CURRENT_SPEND
```

---

## Known Limitations

| Limitation | Impact | Workaround |
|---|---|---|
| In-memory state | Rate limits don't persist across Cloud Run instances or restarts | Use Redis (see above) |
| Token-based cost estimate | Actual GCP billing may differ slightly from proxy estimate | Use GCP Budget as second layer |
| No authentication on `/generate` | Any caller can use your proxy | Add API Gateway or IAP in front |
| Single Gemini model hardcoded | `gemini-2.0-flash` only | Make `GEMINI_MODEL` an env var |

---

## Project Structure

```
api-key-guardian/
├── guardian.py        # Main proxy application
├── requirements.txt   # Python dependencies
├── Dockerfile         # Container definition (non-root, gunicorn)
├── .dockerignore      # Keeps image lean, prevents secret leakage
└── README.md          # This file
```

---

## License

This project is part of the [gemini-live-api-examples](https://github.com/google-gemini/gemini-live-api-examples) repository and is subject to its license terms.
