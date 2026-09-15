"""
app/services/dhan_client.py
────────────────────────────
Phase 1 — Secure Dhan API configuration foundation.
Phase 2 — Reusable API client with retry, rate-limit handling, _post().

SCOPE:
  Phase 1  ✅  Credential loading, validation, ping()
  Phase 2  ✅  Retry strategy, rate-limit (429), _post(), response validation
  Phase 3+ ❌  Trade history, holdings, pagination, analytics

SECURITY RULES — enforced here, not optional:
  - Credentials are read from os.getenv() ONLY.
  - The access token is NEVER logged, even partially in error messages.
  - The client_id is masked in all outputs (first 4 + last 2 chars only).
  - This module is server-side only — never imported in templates or JS.
  - ping() returns only a safe summary dict — no raw token, no full client_id.
"""

import os
import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
DHAN_API_BASE    = "https://api.dhan.co/v2"
_REQUEST_TIMEOUT = 15   # seconds per request

# Retry strategy — applied to idempotent requests only (GET).
# Retries on connection errors and 5xx server errors.
# Does NOT retry on 401/403/429 — those require human action.
_RETRY_TOTAL        = 3
_RETRY_BACKOFF      = 0.5   # exponential: 0.5s, 1s, 2s
_RETRY_STATUS_CODES = (500, 502, 503, 504)  # 5xx transient server errors only


# ── Exceptions ────────────────────────────────────────────────────────────────

class DhanConfigError(Exception):
    """Credentials missing or malformed in .env. Safe to surface to UI."""

class DhanAuthError(Exception):
    """Dhan API rejected credentials (401/403). Message contains no token."""

class DhanRateLimitError(Exception):
    """Dhan API returned 429 Too Many Requests. Caller should back off."""

class DhanAPIError(Exception):
    """Non-auth API failure (network, server error). Message contains no secrets."""


# ── Configuration validation ──────────────────────────────────────────────────

def validate_dhan_config() -> dict:
    """
    Reads and validates Dhan credentials from environment variables.

    Returns a dict with:
        {
            "configured": bool,       # True only if both vars are non-empty
            "has_client_id": bool,
            "has_access_token": bool,
            "error": str | None,      # human-readable problem description, no secrets
        }

    Never raises — always returns a safe status dict.
    Credentials are never included in the return value.
    """
    client_id    = os.getenv("DHAN_CLIENT_ID", "").strip()
    access_token = os.getenv("DHAN_ACCESS_TOKEN", "").strip()

    has_client_id    = bool(client_id)
    has_access_token = bool(access_token)

    if not has_client_id and not has_access_token:
        return {
            "configured":      False,
            "has_client_id":   False,
            "has_access_token":False,
            "error": (
                "DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are not set in .env. "
                "Add both variables and restart Flask."
            ),
        }
    if not has_client_id:
        return {
            "configured":      False,
            "has_client_id":   False,
            "has_access_token":True,
            "error": "DHAN_CLIENT_ID is not set in .env.",
        }
    if not has_access_token:
        return {
            "configured":      False,
            "has_client_id":   True,
            "has_access_token":False,
            "error": "DHAN_ACCESS_TOKEN is not set in .env.",
        }

    return {
        "configured":      True,
        "has_client_id":   True,
        "has_access_token":True,
        "error":           None,
    }


# ── Client ────────────────────────────────────────────────────────────────────

class DhanClient:
    """
    Authenticated Dhan API client.

    Phase 1 exposes only ping() for connection verification.
    Data-fetching methods (trades, holdings, etc.) are added in Phase 2+.

    Always instantiate via DhanClient.from_env() — do not construct directly
    in route handlers (use the factory so credentials come from one place).
    """

    def __init__(self, client_id: str, access_token: str):
        if not client_id or not access_token:
            raise DhanConfigError(
                "DhanClient requires non-empty client_id and access_token. "
                "Use DhanClient.from_env() to load from environment."
            )
        self._client_id    = client_id
        self._access_token = access_token

        # Build a persistent session with retry strategy.
        # Retries are applied only to GET (idempotent) on transient 5xx errors.
        # Auth errors (401/403) and rate limits (429) are NOT retried —
        # they require human action (regenerate token, back off).
        retry = Retry(
            total            = _RETRY_TOTAL,
            backoff_factor   = _RETRY_BACKOFF,
            status_forcelist = _RETRY_STATUS_CODES,
            allowed_methods  = {"GET"},          # POST never retried (non-idempotent)
            raise_on_status  = False,            # we handle status ourselves
        )
        adapter = HTTPAdapter(max_retries=retry)

        self._session = requests.Session()
        self._session.mount("https://", adapter)
        self._session.mount("http://",  adapter)
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept":       "application/json",
            "access-token": self._access_token,
            "client-id":    self._client_id,
        })

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "DhanClient":
        """
        Create a DhanClient from environment variables.

        Raises DhanConfigError (not DhanAuthError) if env vars are absent —
        a missing env var is a config problem, not an auth rejection.
        """
        cfg = validate_dhan_config()
        if not cfg["configured"]:
            raise DhanConfigError(cfg["error"])

        # Read again here — validate_dhan_config() confirmed they exist
        client_id    = os.getenv("DHAN_CLIENT_ID", "").strip()
        access_token = os.getenv("DHAN_ACCESS_TOKEN", "").strip()
        return cls(client_id, access_token)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _validate_response(self, resp: requests.Response, path: str) -> dict | list:
        """
        Central response validation — maps HTTP status codes to typed exceptions.
        Called by both _get() and _post().
        Never logs response body (may contain account data).
        """
        if resp.status_code in (401, 403):
            raise DhanAuthError(
                "Dhan API rejected the request. "
                "The access token may have expired — "
                "generate a new one from the Dhan portal and update "
                "DHAN_ACCESS_TOKEN in .env."
            )
        if resp.status_code == 429:
            raise DhanRateLimitError(
                "Dhan API rate limit reached (HTTP 429). "
                "Wait a few seconds before retrying."
            )
        if not resp.ok:
            raise DhanAPIError(
                f"Dhan API returned HTTP {resp.status_code} for {path}."
            )
        try:
            return resp.json()
        except ValueError:
            raise DhanAPIError(
                f"Dhan API returned a non-JSON response for {path}."
            )

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        """
        Authenticated GET. Retries automatically on transient 5xx errors
        (via HTTPAdapter Retry). Never retries on 401/403/429.
        """
        url = f"{DHAN_API_BASE}{path}"
        log.debug("[DhanClient] GET %s", path)
        try:
            resp = self._session.get(url, params=params, timeout=_REQUEST_TIMEOUT)
        except requests.exceptions.ConnectionError:
            raise DhanAPIError(
                "Cannot reach Dhan API (api.dhan.co). "
                "Check your internet connection."
            )
        except requests.exceptions.Timeout:
            raise DhanAPIError(
                f"Dhan API GET {path} timed out after {_REQUEST_TIMEOUT}s."
            )
        return self._validate_response(resp, path)

    def _post(self, path: str, payload: dict) -> dict | list:
        """
        Authenticated POST. NOT retried (non-idempotent).
        Used for Dhan endpoints that require a request body.
        """
        url = f"{DHAN_API_BASE}{path}"
        log.debug("[DhanClient] POST %s", path)
        try:
            resp = self._session.post(url, json=payload, timeout=_REQUEST_TIMEOUT)
        except requests.exceptions.ConnectionError:
            raise DhanAPIError(
                "Cannot reach Dhan API (api.dhan.co). "
                "Check your internet connection."
            )
        except requests.exceptions.Timeout:
            raise DhanAPIError(
                f"Dhan API POST {path} timed out after {_REQUEST_TIMEOUT}s."
            )
        return self._validate_response(resp, path)

    def _mask_client_id(self) -> str:
        """Returns a masked version of client_id safe for logs and UI."""
        cid = self._client_id
        if len(cid) <= 6:
            return "****"
        # Always show at least 4 asterisks in the middle
        n_stars = max(4, len(cid) - 6)
        return cid[:4] + "*" * n_stars + cid[-2:]

    # ── Phase 1 public method: connection health check ────────────────────────

    def ping(self) -> dict:
        """
        Verify the connection and token are valid by calling /fundlimit.

        Returns a safe summary dict — no access token, no full client_id:
            {
                "status":            "connected",
                "client_id_masked":  "1105****00",
                "available_balance": 21529.9,
                "used_margin":       0.0,
                "currency":          "INR",
            }

        Raises DhanAuthError or DhanAPIError on failure.
        """
        log.info("[DhanClient] ping() — checking connection")
        data = self._get("/fundlimit")

        return {
            "status":            "connected",
            "client_id_masked":  self._mask_client_id(),
            "available_balance": data.get("availabelBalance"),   # Dhan typo in API
            "used_margin":       data.get("utilizedAmount"),
            "currency":          "INR",
        }