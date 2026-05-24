"""
API Key Guardian — Minimal Auto-Block Proxy
============================================
Drop this in front of any Gemini API call.
Zero config needed — set 3 env vars and deploy.

ENV VARS:
  GEMINI_API_KEY     → your real key (stored in Secret Manager)
  BLOCK_BUDGET_USD   → hard spend limit before key is revoked (default: $10)
  RATE_LIMIT_RPM     → max requests per IP per minute (default: 60)

Deploy:
  gcloud run deploy api-guardian \
    --source . \
    --set-env-vars GEMINI_API_KEY=<key>,BLOCK_BUDGET_USD=10,RATE_LIMIT_RPM=60 \
    --region us-central1 \
    --allow-unauthenticated
"""

import os, time, logging
from collections import defaultdict
from flask import Flask, request, jsonify, abort
import httpx
from google.cloud import apikeys_v2

# ──────────────────────────────────────────────
# Config — all from env vars, safe defaults
# ──────────────────────────────────────────────
RATE_LIMIT_RPM   = int(os.environ.get("RATE_LIMIT_RPM",   "60"))
BLOCK_BUDGET_USD = float(os.environ.get("BLOCK_BUDGET_USD", "10"))
WARN_BUDGET_USD  = BLOCK_BUDGET_USD * 0.8          # warn at 80%
PROJECT_ID       = os.environ.get("PROJECT_ID",    "")
KEY_ID           = os.environ.get("API_KEY_ID",    "")
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY","")
GEMINI_URL       = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

# Cost per 1K tokens (Gemini 2.0 Flash pricing)
COST_PER_1K_INPUT  = 0.000075  # $0.075 per 1M input tokens
COST_PER_1K_OUTPUT = 0.0003    # $0.30 per 1M output tokens

# ──────────────────────────────────────────────
# In-memory state  (swap for Redis in production)
# ──────────────────────────────────────────────
ip_request_log  = defaultdict(list)   # ip → [unix timestamps]
blocked_ips     = set()               # permanently blocked this session
daily_spend_usd = 0.0
key_revoked     = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("guardian")

app = Flask(__name__)

# ──────────────────────────────────────────────
# Core blocking logic
# ──────────────────────────────────────────────

def check_rate_limit(ip: str) -> bool:
    """Returns True (and auto-blocks IP) if over the rate limit."""
    now = time.time()
    # Slide the window — keep only last 60 seconds
    ip_request_log[ip] = [t for t in ip_request_log[ip] if now - t < 60]

    if len(ip_request_log[ip]) >= RATE_LIMIT_RPM:
        blocked_ips.add(ip)
        log.warning(f"🚫 RATE LIMIT: IP {ip} auto-blocked ({len(ip_request_log[ip])} req/min)")
        return True

    ip_request_log[ip].append(now)
    return False


def check_budget() -> bool:
    """Returns True if we've hit the hard spend cap."""
    global key_revoked
    if daily_spend_usd >= BLOCK_BUDGET_USD and not key_revoked:
        log.critical(f"💸 BUDGET EXCEEDED: ${daily_spend_usd:.4f} >= ${BLOCK_BUDGET_USD} → revoking key")
        revoke_api_key()
        key_revoked = True
        return True
    if daily_spend_usd >= WARN_BUDGET_USD:
        log.warning(f"⚠️  Budget warning: ${daily_spend_usd:.4f} of ${BLOCK_BUDGET_USD} used")
    return key_revoked


def revoke_api_key():
    """Permanently revoke the managed API key via GCP API Keys API."""
    if not PROJECT_ID or not KEY_ID:
        log.error("Cannot revoke key — PROJECT_ID or API_KEY_ID not set")
        return
    try:
        client = apikeys_v2.ApiKeysClient()
        name   = f"projects/{PROJECT_ID}/locations/global/keys/{KEY_ID}"
        client.delete_key(name=name)           # permanent delete
        log.critical(f"✅ API key {KEY_ID} REVOKED (budget exceeded)")
    except Exception as e:
        log.error(f"Key revocation failed: {e}")


def track_cost(usage_metadata: dict):
    """Estimate and accumulate USD cost from Gemini token usage."""
    global daily_spend_usd
    input_tokens  = usage_metadata.get("promptTokenCount",     0)
    output_tokens = usage_metadata.get("candidatesTokenCount", 0)
    cost = (input_tokens / 1000 * COST_PER_1K_INPUT) + \
           (output_tokens / 1000 * COST_PER_1K_OUTPUT)
    daily_spend_usd += cost
    log.info(f"💰 Cost this request: ${cost:.6f} | Daily total: ${daily_spend_usd:.4f}")

# ──────────────────────────────────────────────
# Proxy endpoint — replaces direct Gemini calls
# ──────────────────────────────────────────────

@app.route("/generate", methods=["POST"])
def proxy():
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()

    # ── Block 1: Already blocked IP ──
    if client_ip in blocked_ips:
        log.info(f"🛑 Blocked IP attempted request: {client_ip}")
        abort(429, description="Your IP has been blocked due to abuse.")

    # ── Block 2: Rate limit check ──
    if check_rate_limit(client_ip):
        abort(429, description=f"Rate limit exceeded: max {RATE_LIMIT_RPM} requests/min.")

    # ── Block 3: Budget cap check ──
    if check_budget():
        abort(402, description="Service suspended: daily spend limit reached.")

    # ── Forward to Gemini ──
    try:
        response = httpx.post(
            GEMINI_URL,
            params={"key": GEMINI_API_KEY},
            json=request.get_json(force=True),
            timeout=30,
        )
    except httpx.TimeoutException:
        abort(504, description="Upstream Gemini API timed out.")

    # ── Track cost from response ──
    if response.status_code == 200:
        body = response.json()
        track_cost(body.get("usageMetadata", {}))
        return jsonify(body), 200

    # Pass through Gemini errors transparently
    return response.text, response.status_code, {"Content-Type": "application/json"}


@app.route("/status")
def status():
    """Health + current guardian state."""
    return jsonify({
        "status":          "revoked" if key_revoked else "active",
        "daily_spend_usd": round(daily_spend_usd, 4),
        "budget_limit":    BLOCK_BUDGET_USD,
        "budget_pct":      round(daily_spend_usd / BLOCK_BUDGET_USD * 100, 1),
        "blocked_ips":     list(blocked_ips),
        "rate_limit_rpm":  RATE_LIMIT_RPM,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    log.info(f"🛡️  Guardian starting on :{port} | Budget: ${BLOCK_BUDGET_USD} | Rate: {RATE_LIMIT_RPM} rpm")
    app.run(host="0.0.0.0", port=port)
