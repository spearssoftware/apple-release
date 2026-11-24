"""App Store Connect API plumbing shared by the release scripts: ES256 token
minting and a REST client with token refresh and retries.

Standard library only, apart from `cryptography` for signing, so it runs on the
Linux runners that do the waiting as well as on macOS.

Credentials come from ASC_KEY_ID, ASC_ISSUER_ID, and either ASC_PRIVATE_KEY
(the .p8 contents, raw or base64) or ASC_KEY_PATH (the .p8 file).
"""
import base64
import binascii
import http.client
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.appstoreconnect.apple.com"

# Apple rejects a token whose exp is more than 20 minutes past its iat. That is
# shorter than the time a build can spend processing, so tokens are re-minted
# mid-run rather than issued once up front.
TOKEN_LIFETIME = 15 * 60
TOKEN_REFRESH = 12 * 60


def log(msg):
    print(msg, flush=True)


class Transient(Exception):
    """A failure worth retrying: rate limiting, a 5xx, or a dropped connection."""


class Unauthorized(Exception):
    """A 401. Retryable exactly once, to cover a token that expired in flight."""


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def pem_bytes(value):
    """The .p8 PEM from a secret stored raw or base64-encoded."""
    if "-----BEGIN" in value:
        return value.encode()
    try:
        pem = base64.b64decode(value, validate=False)
    except (binascii.Error, ValueError):
        pem = b""
    if b"-----BEGIN PRIVATE KEY-----" not in pem:
        sys.exit("error: ASC_PRIVATE_KEY is neither a .p8 nor a base64-encoded .p8")
    return pem


def load_key(pem):
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
    except ImportError:
        sys.exit(
            "error: the 'cryptography' package is required "
            "(brew install cryptography, or python3 -m pip install cryptography)"
        )
    return load_pem_private_key(pem, password=None)


def mint_token(key_id, issuer_id, key):
    """ES256 JWT for the App Store Connect API, signed with a loaded EC key."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    now = int(time.time())
    header = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    payload = {
        "iss": issuer_id,
        "iat": now,
        "exp": now + TOKEN_LIFETIME,
        "aud": "appstoreconnect-v1",
    }

    signing_input = (
        b64url(json.dumps(header, separators=(",", ":")).encode())
        + b"."
        + b64url(json.dumps(payload, separators=(",", ":")).encode())
    )

    # `cryptography` emits a DER-encoded signature; JOSE wants raw r||s as two
    # fixed-width 32-byte big-endian integers.
    der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")

    return (signing_input + b"." + b64url(signature)).decode()


class Client:
    """App Store Connect client with a self-renewing token and retries.

    A run routinely outlives a single token: waiting for a build to appear and
    finish processing can take longer than the 20 minutes Apple allows on a
    token's exp, so the token is minted lazily and refreshed as it ages.
    """

    def __init__(self, key_id, issuer_id, key, timeout, interval=30):
        self.key_id = key_id
        self.issuer_id = issuer_id
        self.key = key
        self.interval = interval
        self._token = None
        self._minted_at = 0.0
        self.budget(timeout)

    @classmethod
    def from_env(cls, timeout):
        key_id = os.environ.get("ASC_KEY_ID")
        issuer_id = os.environ.get("ASC_ISSUER_ID")
        key_value = os.environ.get("ASC_PRIVATE_KEY")
        key_path = os.path.expanduser(os.environ.get("ASC_KEY_PATH", ""))
        if not key_id or not issuer_id or not (key_value or key_path):
            sys.exit("error: set ASC_KEY_ID, ASC_ISSUER_ID, and ASC_PRIVATE_KEY or ASC_KEY_PATH")
        if key_value:
            pem = pem_bytes(key_value)
        elif Path(key_path).is_file():
            pem = Path(key_path).read_bytes()
        else:
            sys.exit(f"error: private key not found at {key_path}")
        return cls(key_id, issuer_id, load_key(pem), timeout=timeout)

    def budget(self, seconds):
        """Give the calls that follow their own window to retry within.

        Polling and the write/verify calls get separate budgets: polling can
        legitimately consume its whole deadline waiting on Apple, which would
        otherwise leave the writes with no retries at all.
        """
        self.deadline = time.monotonic() + seconds

    def token(self):
        age = time.monotonic() - self._minted_at
        if self._token is None or age > TOKEN_REFRESH:
            self._token = mint_token(self.key_id, self.issuer_id, self.key)
            self._minted_at = time.monotonic()
        return self._token

    def _send(self, method, path_or_url, params, body):
        url = path_or_url if path_or_url.startswith("https://") else API + path_or_url
        if params:
            url += "?" + urllib.parse.urlencode(params)
        path = urllib.parse.urlparse(url).path

        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": "Bearer " + self.token()}
        if data:
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")
            if err.code == 401:
                # Usually an expired token; drop it so the retry re-mints.
                self._token = None
                raise Unauthorized(f"HTTP 401 on {method} {path}") from None
            if err.code == 429 or err.code >= 500:
                raise Transient(f"HTTP {err.code} on {method} {path}") from None
            raise RuntimeError(
                f"{method} {path} failed with HTTP {err.code}: {detail}"
            ) from None
        except (urllib.error.URLError, socket.timeout, http.client.HTTPException) as err:
            raise Transient(f"{type(err).__name__} on {method} {path}: {err}") from None

    def request(self, method, path_or_url, params=None, body=None):
        reauthed = False
        while True:
            try:
                return self._send(method, path_or_url, params, body)
            except Unauthorized as err:
                # One immediate retry covers a token that expired in flight. A
                # second 401 means the credentials themselves are wrong, and
                # retrying those to the deadline just burns runner time.
                if reauthed:
                    raise RuntimeError(
                        f"{err}; check ASC_KEY_ID, ASC_ISSUER_ID and the .p8 key"
                    ) from None
                reauthed = True
                log(f"  {err}; re-minting the token and retrying")
            except Transient as err:
                if time.monotonic() >= self.deadline:
                    raise RuntimeError(f"{err} (giving up at deadline)") from None
                log(f"  {err}; retrying in {self.interval}s")
                time.sleep(self.interval)

    def all(self, path, params=None):
        """Every item of a collection, following links.next across pages."""
        page = self.request("GET", path, params)
        items = list(page.get("data", []))
        while next_url := page.get("links", {}).get("next"):
            page = self.request("GET", next_url)
            items += page.get("data", [])
        return items


def resolve_app_id(client, bundle_id):
    # filter[bundleId] can also match other bundle ids, so require an exact match.
    for app in client.all("/v1/apps", {"filter[bundleId]": bundle_id}):
        if app["attributes"].get("bundleId") == bundle_id:
            return app["id"]
    raise RuntimeError(f"no App Store Connect app with bundle id {bundle_id}")


def build_number(build):
    """A build's CFBundleVersion as an int, so "10" sorts above "9"."""
    try:
        return int(build["attributes"]["version"])
    except (KeyError, ValueError):
        return 0
