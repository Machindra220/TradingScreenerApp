"""
tests/test_dhan_phase2.py
──────────────────────────
Phase 2 focused tests — reusable client, retry, rate limit, _post.

Covers:
  - Successful auth + request flow
  - Missing configuration
  - Auth failure (401/403)
  - Rate limit (429) → DhanRateLimitError
  - Timeout → DhanAPIError
  - 5xx → DhanAPIError (retry logic engaged)
  - _post() helper correct behaviour
  - _validate_response() error mapping
  - Safe error messages (no secrets)
  - Retry strategy configured on session

Run with:
    python -m pytest tests/test_dhan_phase2.py -v
"""

import os
import unittest
from unittest.mock import patch, MagicMock, call

import requests as req

from app.services.dhan_client import (
    DhanClient,
    DhanConfigError,
    DhanAuthError,
    DhanRateLimitError,
    DhanAPIError,
    validate_dhan_config,
    _RETRY_TOTAL,
    _RETRY_STATUS_CODES,
    _REQUEST_TIMEOUT,
    DHAN_API_BASE,
)


def _make_client(client_id="11050100", token="test_token"):
    """Helper: create a DhanClient without env vars."""
    return DhanClient(client_id=client_id, access_token=token)


def _mock_resp(status_code=200, json_data=None, ok=None):
    """Helper: build a mock requests.Response."""
    m = MagicMock()
    m.status_code = status_code
    m.ok = (status_code < 400) if ok is None else ok
    m.json.return_value = json_data or {}
    return m


# ── Retry strategy ────────────────────────────────────────────────────────────

class TestRetryConfiguration(unittest.TestCase):

    def test_session_has_https_adapter(self):
        client = _make_client()
        adapter = client._session.get_adapter("https://api.dhan.co")
        self.assertIsNotNone(adapter)

    def test_retry_total_matches_constant(self):
        from requests.adapters import HTTPAdapter
        client  = _make_client()
        adapter = client._session.get_adapter("https://api.dhan.co")
        self.assertIsInstance(adapter, HTTPAdapter)
        self.assertEqual(adapter.max_retries.total, _RETRY_TOTAL)

    def test_retry_status_codes(self):
        client  = _make_client()
        adapter = client._session.get_adapter("https://api.dhan.co")
        for code in _RETRY_STATUS_CODES:
            self.assertIn(code, adapter.max_retries.status_forcelist)

    def test_retry_only_on_get(self):
        """POST must NOT be in the retry allowed_methods."""
        client  = _make_client()
        adapter = client._session.get_adapter("https://api.dhan.co")
        allowed = adapter.max_retries.allowed_methods
        self.assertIn("GET",  allowed)
        self.assertNotIn("POST", allowed)


# ── _validate_response ────────────────────────────────────────────────────────

class TestValidateResponse(unittest.TestCase):

    def setUp(self):
        self.client = _make_client()

    def test_200_returns_json(self):
        resp = _mock_resp(200, {"key": "value"})
        result = self.client._validate_response(resp, "/test")
        self.assertEqual(result, {"key": "value"})

    def test_401_raises_auth_error(self):
        with self.assertRaises(DhanAuthError):
            self.client._validate_response(_mock_resp(401), "/test")

    def test_403_raises_auth_error(self):
        with self.assertRaises(DhanAuthError):
            self.client._validate_response(_mock_resp(403), "/test")

    def test_429_raises_rate_limit_error(self):
        with self.assertRaises(DhanRateLimitError):
            self.client._validate_response(_mock_resp(429), "/test")

    def test_500_raises_api_error(self):
        with self.assertRaises(DhanAPIError):
            self.client._validate_response(_mock_resp(500), "/test")

    def test_non_json_raises_api_error(self):
        resp = _mock_resp(200)
        resp.json.side_effect = ValueError("not json")
        with self.assertRaises(DhanAPIError):
            self.client._validate_response(resp, "/test")

    def test_error_messages_contain_no_token(self):
        token = "my_secret_token"
        client = _make_client(token=token)
        for status, exc in [(401, DhanAuthError), (429, DhanRateLimitError), (500, DhanAPIError)]:
            with self.assertRaises(exc) as ctx:
                client._validate_response(_mock_resp(status), "/test")
            self.assertNotIn(token, str(ctx.exception))


# ── _get ──────────────────────────────────────────────────────────────────────

class TestGetMethod(unittest.TestCase):

    def setUp(self):
        self.client = _make_client()

    def test_successful_get(self):
        resp = _mock_resp(200, {"availabelBalance": 5000.0})
        with patch.object(self.client._session, "get", return_value=resp) as mock_get:
            result = self.client._get("/fundlimit")
            self.assertEqual(result["availabelBalance"], 5000.0)
            mock_get.assert_called_once_with(
                f"{DHAN_API_BASE}/fundlimit",
                params=None,
                timeout=_REQUEST_TIMEOUT,
            )

    def test_connection_error_raises_api_error(self):
        with patch.object(self.client._session, "get",
                          side_effect=req.exceptions.ConnectionError()):
            with self.assertRaises(DhanAPIError) as ctx:
                self.client._get("/fundlimit")
            self.assertIn("internet connection", str(ctx.exception))

    def test_timeout_raises_api_error(self):
        with patch.object(self.client._session, "get",
                          side_effect=req.exceptions.Timeout()):
            with self.assertRaises(DhanAPIError) as ctx:
                self.client._get("/fundlimit")
            self.assertIn("timed out", str(ctx.exception))

    def test_rate_limit_raises_correct_type(self):
        with patch.object(self.client._session, "get",
                          return_value=_mock_resp(429)):
            with self.assertRaises(DhanRateLimitError):
                self.client._get("/fundlimit")

    def test_get_passes_params(self):
        resp = _mock_resp(200, [])
        with patch.object(self.client._session, "get", return_value=resp) as mock_get:
            self.client._get("/trades", params={"page": 1})
            _, kwargs = mock_get.call_args
            self.assertEqual(kwargs["params"], {"page": 1})


# ── _post ─────────────────────────────────────────────────────────────────────

class TestPostMethod(unittest.TestCase):

    def setUp(self):
        self.client = _make_client()

    def test_successful_post(self):
        resp = _mock_resp(200, {"result": "ok"})
        payload = {"securityId": "1333", "exchangeSegment": "NSE_EQ"}
        with patch.object(self.client._session, "post", return_value=resp) as mock_post:
            result = self.client._post("/charts/historical", payload)
            self.assertEqual(result, {"result": "ok"})
            mock_post.assert_called_once_with(
                f"{DHAN_API_BASE}/charts/historical",
                json=payload,
                timeout=_REQUEST_TIMEOUT,
            )

    def test_post_connection_error(self):
        with patch.object(self.client._session, "post",
                          side_effect=req.exceptions.ConnectionError()):
            with self.assertRaises(DhanAPIError):
                self.client._post("/charts/historical", {})

    def test_post_timeout(self):
        with patch.object(self.client._session, "post",
                          side_effect=req.exceptions.Timeout()):
            with self.assertRaises(DhanAPIError) as ctx:
                self.client._post("/charts/historical", {})
            self.assertIn("timed out", str(ctx.exception))

    def test_post_auth_failure(self):
        with patch.object(self.client._session, "post",
                          return_value=_mock_resp(401)):
            with self.assertRaises(DhanAuthError):
                self.client._post("/charts/historical", {})

    def test_post_rate_limit(self):
        with patch.object(self.client._session, "post",
                          return_value=_mock_resp(429)):
            with self.assertRaises(DhanRateLimitError):
                self.client._post("/charts/historical", {})


# ── ping() integration path ───────────────────────────────────────────────────

class TestPingIntegration(unittest.TestCase):

    def test_ping_full_success_path(self):
        """Full flow: from_env → session → /fundlimit → safe response."""
        with patch.dict(os.environ, {
            "DHAN_CLIENT_ID":    "11050100",
            "DHAN_ACCESS_TOKEN": "secret_token",
        }):
            client = DhanClient.from_env()
            resp   = _mock_resp(200, {"availabelBalance": 21529.9, "utilizedAmount": 3.0})
            with patch.object(client._session, "get", return_value=resp):
                result = client.ping()

        # Must contain only safe fields
        self.assertTrue(result["status"] == "connected")
        self.assertNotIn("secret_token", str(result))
        self.assertNotIn("11050100",     str(result))
        self.assertIn("****",            result["client_id_masked"])
        self.assertEqual(result["available_balance"], 21529.9)

    def test_ping_propagates_auth_error(self):
        with patch.dict(os.environ, {
            "DHAN_CLIENT_ID":    "11050100",
            "DHAN_ACCESS_TOKEN": "expired_token",
        }):
            client = DhanClient.from_env()
            resp   = _mock_resp(401)
            with patch.object(client._session, "get", return_value=resp):
                with self.assertRaises(DhanAuthError):
                    client.ping()

    def test_ping_propagates_rate_limit(self):
        with patch.dict(os.environ, {
            "DHAN_CLIENT_ID":    "11050100",
            "DHAN_ACCESS_TOKEN": "token",
        }):
            client = DhanClient.from_env()
            resp   = _mock_resp(429)
            with patch.object(client._session, "get", return_value=resp):
                with self.assertRaises(DhanRateLimitError):
                    client.ping()

    def test_missing_config_raises_config_error(self):
        env = {"DHAN_CLIENT_ID": "", "DHAN_ACCESS_TOKEN": ""}
        with patch.dict(os.environ, env):
            with self.assertRaises(DhanConfigError):
                DhanClient.from_env()


if __name__ == "__main__":
    unittest.main()