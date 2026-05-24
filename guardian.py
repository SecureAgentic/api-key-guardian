"""
API Key Guardian — Dual-Guard Cost Safety Net
=============================================
Intercepts Gemini API calls, tracks USD spend and token usage in real-time,
and permanently deletes your GCP API key if budget thresholds are exceeded.

Required GCP IAM Permission:
  apikeys.keys.delete (e.g., API Keys Admin role) on the Cloud Run service account.

ENV VARS:
  GEMINI_API_KEY         → your real Gemini API key
  PROJECT_ID             → GCP project ID
  API_KEY_ID             → GCP API key resource ID (UUID)
  BLOCK_BUDGET_USD       → Max daily spend in USD before key deletion (default: 1.0)
  BLOCK_BUDGET_TOKENS    → Max daily tokens before key deletion (default: 10,000,000)
  GEMINI_MODEL           → Gemini model name (default: gemini-2.0-flash)
  COST_PER_1M_INPUT_USD  → Custom input cost per 1M tokens (default: 0.075)
  COST_PER_1M_OUTPUT_USD → Custom output cost per 1M tokens (default: 0.30)
  STATUS_TOKEN           → Optional query/header token to secure `/status`
"""

import os
import logging
import threading
import time
from datetime import datetime, timezone
from flask import Flask, request, jsonify, abort
import httpx
from google.cloud import api_keys_v2

# ──────────────────────────────────────────────
# Config — from env vars with safe defaults
# ──────────────────────────────────────────────
PROJECT_ID      = os.environ.get("PROJECT_ID", "")
KEY_ID          = os.environ.get("API_KEY_ID", "")
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL    = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

BLOCK_BUDGET_USD    = float(os.environ.get("BLOCK_BUDGET_USD", "1.0"))
BLOCK_BUDGET_TOKENS = int(os.environ.get("BLOCK_BUDGET_TOKENS", "10000000"))

# Configurable price per 1 million tokens (default: Gemini 2.0 Flash pricing)
COST_PER_1M_INPUT  = float(os.environ.get("COST_PER_1M_INPUT_USD", "0.075"))
COST_PER_1M_OUTPUT = float(os.environ.get("COST_PER_1M_OUTPUT_USD", "0.30"))

# Convert to individual token costs
COST_PER_INPUT_TOKEN  = COST_PER_1M_INPUT / 1000000.0
COST_PER_OUTPUT_TOKEN = COST_PER_1M_OUTPUT / 1000000.0

GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")
STATUS_TOKEN = os.environ.get("STATUS_TOKEN", "")

# ──────────────────────────────────────────────
# Thread-Safe Shared Global State
# ──────────────────────────────────────────────
daily_spend_usd = 0.0
daily_tokens    = 0
current_date    = datetime.now(timezone.utc).date()
key_revoked     = False

state_lock = threading.Lock()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("guardian")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1MB limit to prevent memory exhaustion

# ──────────────────────────────────────────────
# Core safety-net logic
# ──────────────────────────────────────────────

def revoke_api_key():
    """Permanently delete the API key via GCP API Keys API with retries and exponential backoff."""
    if not PROJECT_ID or not KEY_ID:
        log.error("Cannot revoke key — PROJECT_ID or API_KEY_ID not set")
        return
    name = f"projects/{PROJECT_ID}/locations/global/keys/{KEY_ID}"
    log.critical(f"🔥 Deleting compromised API key: {name}")
    for attempt in range(3):
        try:
            client = api_keys_v2.ApiKeysClient()
            client.delete_key(name=name)
            log.critical(f"✅ API key {KEY_ID} permanently deleted.")
            return
        except Exception as e:
            log.error(f"❌ Key deletion attempt {attempt+1}/3 failed: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    log.critical("🚨 ALL DELETION ATTEMPTS FAILED — manual intervention required!")


def track_usage_and_check(usage_metadata: dict) -> bool:
    """Updates global counters and checks budget thresholds. Triggers deletion on breach."""
    global daily_spend_usd, daily_tokens, current_date, key_revoked

    input_tokens  = usage_metadata.get("promptTokenCount", 0)
    output_tokens = usage_metadata.get("candidatesTokenCount", 0)
    total_tokens  = usage_metadata.get("totalTokenCount", 0)

    if total_tokens == 0:
        total_tokens = input_tokens + output_tokens

    cost = (input_tokens * COST_PER_INPUT_TOKEN) + (output_tokens * COST_PER_OUTPUT_TOKEN)

    with state_lock:
        # UTC Date Rollover Check
        today = datetime.now(timezone.utc).date()
        if today != current_date:
            log.info(f"📆 Date rollover detected ({current_date} → {today}). Resetting daily spend counters.")
            current_date = today
            daily_spend_usd = 0.0
            daily_tokens = 0

        daily_spend_usd += cost
        daily_tokens    += total_tokens

        log.info(
            f"💰 Request Cost: ${cost:.6f} | "
            f"Daily Spend: ${daily_spend_usd:.4f}/${BLOCK_BUDGET_USD:.2f} USD | "
            f"Daily Tokens: {daily_tokens:,}/{BLOCK_BUDGET_TOKENS:,}"
        )

        usd_breached    = daily_spend_usd >= BLOCK_BUDGET_USD
        tokens_breached = daily_tokens >= BLOCK_BUDGET_TOKENS

        if (usd_breached or tokens_breached) and not key_revoked:
            reason = "USD budget limit" if usd_breached else "token limit"
            limit_val = f"${BLOCK_BUDGET_USD:.2f}" if usd_breached else f"{BLOCK_BUDGET_TOKENS:,} tokens"
            curr_val = f"${daily_spend_usd:.4f}" if usd_breached else f"{daily_tokens:,} tokens"

            log.critical(
                f"🚨 SAFETY LIMIT TRIGGERED: Current {reason} ({curr_val}) >= Limit ({limit_val}). "
                f"Deleting key to prevent runaway spend!"
            )
            key_revoked = True

    # Revoke key outside the critical lock section to avoid holding threads during GCP API call
    if usd_breached or tokens_breached:
        revoke_api_key()

    return key_revoked

# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────

@app.route("/generate", methods=["POST"])
def proxy():
    if PROXY_API_KEY:
        caller_key = request.headers.get("X-API-Key", "")
        if caller_key != PROXY_API_KEY:
            abort(401, description="Unauthorized.")

    if key_revoked:
        return jsonify({"error": "Service suspended: API key safety budget exceeded."}), 402

    # Validate JSON payload
    req_json = request.get_json(silent=True)
    if req_json is None:
        abort(400, description="Invalid JSON payload.")

    # Forward to Gemini API
    try:
        response = httpx.post(
            GEMINI_URL,
            params={"key": GEMINI_API_KEY},
            json=req_json,
            timeout=30.0,
        )
    except httpx.TimeoutException:
        abort(504, description="Upstream Gemini API timed out.")
    except Exception as e:
        log.error(f"Error forwarding request to Gemini: {e}")
        abort(500, description="Failed to reach upstream Gemini API.")

    # Process successful responses
    if response.status_code == 200:
        try:
            body = response.json()
        except Exception as e:
            log.error(f"Failed to parse Gemini response JSON: {e}")
            # Fallback to serving raw response if parsing fails to avoid blocking the user
            return response.text, response.status_code, {"Content-Type": "application/json"}

        # Track usage and check budget
        usage_metadata = body.get("usageMetadata", {})
        if usage_metadata:
            if track_usage_and_check(usage_metadata):
                return jsonify({"error": "Service suspended: budget limit reached on this request."}), 402

        return jsonify(body), 200

    # Pass through Gemini API errors transparently
    return response.text, response.status_code, {"Content-Type": "application/json"}


@app.route("/status")
def status():
    """Health + current guardian state."""
    if STATUS_TOKEN:
        provided_token = request.headers.get("X-Status-Token") or request.args.get("token")
        if provided_token != STATUS_TOKEN:
            abort(401, description="Unauthorized.")

    return jsonify({
        "status": "revoked" if key_revoked else "active",
        "model": GEMINI_MODEL,
        "daily_spend_usd": round(daily_spend_usd, 4),
        "daily_tokens": daily_tokens,
        "budget_limit_usd": BLOCK_BUDGET_USD,
        "budget_limit_tokens": BLOCK_BUDGET_TOKENS,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    log.info(
        f"🛡️ Guardian proxy active on :{port} | Model: {GEMINI_MODEL} | "
        f"Budget: ${BLOCK_BUDGET_USD} USD | Limit: {BLOCK_BUDGET_TOKENS:,} tokens"
    )
    app.run(host="0.0.0.0", port=port)
