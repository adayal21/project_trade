"""
coinswitch_auth.py — CoinSwitch PRO API authentication (Ed25519 signing).

CoinSwitch Spot endpoints require:
  - GET requests with signed headers
  - X-AUTH-APIKEY  : your API public key (hex)
  - X-AUTH-SIGNATURE : Ed25519 signature of (METHOD + decoded_path + epoch)
  - X-AUTH-EPOCH   : current Unix time in milliseconds

Credentials are loaded from environment variables:
    COINSWITCH_API_KEY    — hex public key (from CoinSwitch PRO profile)
    COINSWITCH_SECRET_KEY — hex private key (from CoinSwitch PRO profile)

Usage:
    from coinswitch_auth import signed_get
    response = signed_get("/trade/api/v2/coins", params={"exchange": "coinswitchx"})
"""

import os
import time
import urllib.parse
import requests
from cryptography.hazmat.primitives.asymmetric import ed25519

COINSWITCH_BASE_URL = "https://coinswitch.co"


def _get_credentials() -> tuple[str, str]:
    key    = os.environ.get("COINSWITCH_API_KEY", "")
    secret = os.environ.get("COINSWITCH_API_SECRET", "")
    if not key or not secret:
        raise EnvironmentError(
            "COINSWITCH_API_KEY and COINSWITCH_API_SECRET must be set as "
            "environment variables before using the CoinSwitch API."
        )
    return key, secret


def sign_request(method: str, path: str, params: dict = None) -> tuple[dict, str]:
    """
    Build signed headers + final path for a CoinSwitch authenticated request.

    Parameters
    ----------
    method : str   "GET" | "POST" | "DELETE"
    path   : str   endpoint path, e.g. "/trade/api/v2/candles"
    params : dict  query parameters (GET requests)

    Returns
    -------
    (headers dict, decoded_path str)
    """
    api_key, secret_key = _get_credentials()
    method = method.upper()

    if params:
        sep  = "&" if "?" in path else "?"
        path = path + sep + urllib.parse.urlencode(params)

    decoded_path = urllib.parse.unquote_plus(path)
    epoch        = str(int(time.time() * 1000))
    message      = method + decoded_path + epoch

    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(secret_key)
    )
    signature = private_key.sign(message.encode("utf-8")).hex()

    headers = {
        "Content-Type":     "application/json",
        "X-AUTH-APIKEY":    api_key,
        "X-AUTH-SIGNATURE": signature,
        "X-AUTH-EPOCH":     epoch,
    }
    return headers, decoded_path


def signed_get(endpoint: str, params: dict = None, timeout: int = 20) -> requests.Response:
    """
    Make a signed GET request to the CoinSwitch Spot API.

    Parameters
    ----------
    endpoint : str   e.g. "/trade/api/v2/candles"
    params   : dict  query parameters
    timeout  : int   request timeout in seconds

    Returns
    -------
    requests.Response — caller is responsible for checking status_code
    """
    last_exc = None
    for attempt in range(1, 4):
        try:
            # Re-sign every attempt — epoch must be fresh
            headers, path = sign_request("GET", endpoint, params)
            response = requests.get(
                COINSWITCH_BASE_URL + path,
                headers=headers,
                timeout=timeout,
            )
            return response
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < 3:
                backoff = 2 ** attempt
                print(f"  [coinswitch] GET {endpoint} failed (attempt {attempt}/3): {e}. Retrying in {backoff}s...")
                time.sleep(backoff)

    raise ConnectionError(f"CoinSwitch API unreachable after 3 attempts: {last_exc}")