"""
CoinDCX API authentication — HMAC-SHA256 signing.

CoinDCX trading endpoints require:
  - POST requests with JSON body
  - Body must include a 'timestamp' field (epoch ms)
  - X-AUTH-APIKEY header: your API key
  - X-AUTH-SIGNATURE header: HMAC-SHA256(secret, json_body)

Usage:
    from coindcx_auth import signed_post
    response = signed_post("/exchange/v1/users/balances", {})
"""

import os
import hmac
import hashlib
import json
import time
import requests

# ---------------------------------------------------------------------------
# Credentials — loaded from environment variables.
# Set these in your .env file or shell before running:
#   COINDCX_API_KEY=your_key
#   COINDCX_API_SECRET=your_secret
# ---------------------------------------------------------------------------

COINDCX_BASE_URL = "https://api.coindcx.com"


def _get_credentials() -> tuple[str, str]:
    """Load API key and secret from environment. Raises clearly if missing."""
    key    = os.environ.get("COINDCX_API_KEY", "")
    secret = os.environ.get("COINDCX_API_SECRET", "")
    if not key or not secret:
        raise EnvironmentError(
            "COINDCX_API_KEY and COINDCX_API_SECRET must be set as environment "
            "variables (or in your .env file via python-dotenv) before using "
            "live trading mode."
        )
    return key, secret


def _sign(secret: str, body: dict) -> tuple[str, str]:
    """
    Add timestamp to body, sign with HMAC-SHA256, return (json_payload, signature).
    Body is modified in-place to include the timestamp.
    """
    body["timestamp"] = int(time.time() * 1000)
    payload = json.dumps(body, separators=(',', ':'), sort_keys=True)
    signature = hmac.new(
        secret.encode('utf-8'),
        payload.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return payload, signature


def signed_post(endpoint: str, body: dict, timeout: int = 10) -> requests.Response:
    """
    Make a signed POST request to the CoinDCX trading API.

    Parameters
    ----------
    endpoint : str   e.g. "/exchange/v1/users/balances"
    body     : dict  request body (timestamp will be added automatically)
    timeout  : int   request timeout in seconds

    Returns
    -------
    requests.Response — caller is responsible for checking status_code
    """
    key, secret = _get_credentials()
    payload, signature = _sign(secret, body)

    headers = {
        "Content-Type":    "application/json",
        "X-AUTH-APIKEY":   key,
        "X-AUTH-SIGNATURE": signature,
    }

    url = COINDCX_BASE_URL + endpoint

    last_exc = None
    for attempt in range(1, 4):
        try:
            response = requests.post(url, data=payload, headers=headers, timeout=timeout)
            return response
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < 3:
                import time as _t
                print(f"  [exchange] Request failed (attempt {attempt}/3): {e}. Retrying...")
                _t.sleep(2 ** attempt)

    raise ConnectionError(f"CoinDCX API unreachable after 3 attempts: {last_exc}")