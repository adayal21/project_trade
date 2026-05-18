import time
import urllib

from urllib.parse import urlencode, urlparse
from cryptography.hazmat.primitives.asymmetric import ed25519

from config import API_SECRET


def make_signature(method, endpoint, params=None):

    if params is None:
        params = {}

    epoch_time = str(int(time.time() * 1000))

    if method == "GET" and params:
        endpoint += (
            ("&" if urlparse(endpoint).query else "?")
            + urlencode(params)
        )

    unquoted = urllib.parse.unquote_plus(endpoint)

    signature_message = (
        method.upper()
        + unquoted
        + epoch_time
    )

    request_bytes = bytes(signature_message, "utf-8")

    secret_bytes = bytes.fromhex(API_SECRET)

    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
        secret_bytes
    )

    signature = private_key.sign(
        request_bytes
    ).hex()

    return endpoint, epoch_time, signature