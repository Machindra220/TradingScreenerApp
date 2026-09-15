"""
tests/test_dhan_phase1.py
──────────────────────────
Phase 1 focused tests — credential validation and DhanClient security.

Tests cover ONLY Phase 1 scope:
  - validate_dhan_config() with all env var combinations
  - DhanClient.from_env() raises correct exception when config missing
  - _mask_client_id() never exposes full client_id
  - ping() response contains no token
  - Error messages contain no secret values

Run with:
    pytest tests/test_dhan_phase1.py -v
"""

import os
import unittest
from unittest.mock import patch, MagicMock

from app.services.dhan_client import (
    DhanClient,
    DhanConfigError,
    DhanAuthError,
    DhanAPIError,
    validate_dhan_config,
)


class TestValidateDhanConfig(unittest.TestCase):
    """Tests for validate_dhan_config() — all env var combinations."""

    def test_both_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("DHAN_CLIENT_ID",    None)
            os.environ.pop("DHAN_ACCESS_TOKEN", None)
            result = validate_dhan_config()
            self.assertFalse(result["configured"])
            self.assertFalse(result["has_client_id"])
            self.assertFalse(result["has_access_token"])
            self.assertIsNotNone(result["error"])

    def test_only_client_id_set(self):
        with patch.dict(os.environ, {"DHAN_CLIENT_ID": "12345"}, clear=False):
            os.environ.pop("DHAN_ACCESS_TOKEN", None)
            result = validate_dhan_config()
            self.assertFalse(result["configured"])
            self.assertTrue(result["has_client_id"])
            self.assertFalse(result["has_access_token"])
            self.assertIn("DHAN_ACCESS_TOKEN", result["error"])

    def test_only_access_token_set(self):
        with patch.dict(os.environ, {"DHAN_ACCESS_TOKEN": "tok123"}, clear=False):
            os.environ.pop("DHAN_CLIENT_ID", None)
            result = validate_dhan_config()
            self.assertFalse(result["configured"])
            self.assertFalse(result["has_client_id"])
            self.assertTrue(result["has_access_token"])
            self.assertIn("DHAN_CLIENT_ID", result["error"])

    def test_both_set(self):
        with patch.dict(os.environ, {
            "DHAN_CLIENT_ID":    "11050100",
            "DHAN_ACCESS_TOKEN": "secure_token_value",
        }):
            result = validate_dhan_config()
            self.assertTrue(result["configured"])
            self.assertTrue(result["has_client_id"])
            self.assertTrue(result["has_access_token"])
            self.assertIsNone(result["error"])

    def test_empty_string_treated_as_missing(self):
        with patch.dict(os.environ, {
            "DHAN_CLIENT_ID":    "",
            "DHAN_ACCESS_TOKEN": "  ",   # whitespace only
        }):
            result = validate_dhan_config()
            self.assertFalse(result["configured"])

    def test_error_message_contains_no_token(self):
        """Security: error messages must never echo credential values."""
        token = "super_secret_dhan_token_12345"
        with patch.dict(os.environ, {
            "DHAN_CLIENT_ID":    "",
            "DHAN_ACCESS_TOKEN": token,
        }):
            result = validate_dhan_config()
            self.assertNotIn(token, result.get("error", ""))


class TestDhanClientFromEnv(unittest.TestCase):
    """Tests for DhanClient.from_env() config validation."""

    def test_raises_config_error_when_missing(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DHAN_CLIENT_ID",    None)
            os.environ.pop("DHAN_ACCESS_TOKEN", None)
            with self.assertRaises(DhanConfigError):
                DhanClient.from_env()

    def test_raises_config_error_message_has_no_secrets(self):
        token = "my_real_token_xyz"
        with patch.dict(os.environ, {
            "DHAN_CLIENT_ID":    "12345",
            "DHAN_ACCESS_TOKEN": "",
        }):
            try:
                DhanClient.from_env()
            except DhanConfigError as e:
                self.assertNotIn(token, str(e))

    def test_creates_client_when_configured(self):
        with patch.dict(os.environ, {
            "DHAN_CLIENT_ID":    "11050100",
            "DHAN_ACCESS_TOKEN": "valid_token",
        }):
            client = DhanClient.from_env()
            self.assertIsInstance(client, DhanClient)


class TestDhanClientMasking(unittest.TestCase):
    """Tests that client_id masking works and never exposes full value."""

    def _make_client(self, client_id="11050100", token="tok"):
        return DhanClient(client_id=client_id, access_token=token)

    def test_mask_hides_middle(self):
        client = self._make_client("11050100")
        masked = client._mask_client_id()
        self.assertNotEqual(masked, "11050100")
        self.assertIn("*", masked)

    def test_mask_shows_first_4_and_last_2(self):
        client = self._make_client("11050100")
        masked = client._mask_client_id()
        self.assertTrue(masked.startswith("1105"))
        self.assertTrue(masked.endswith("00"))

    def test_mask_short_client_id(self):
        client = self._make_client("123")
        masked = client._mask_client_id()
        self.assertEqual(masked, "****")

    def test_ping_response_contains_no_token(self):
        """Security: ping() must never return the access token."""
        token = "real_secret_token_value"
        client = DhanClient(client_id="11050100", access_token=token)

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "availabelBalance": 21529.9,
            "utilizedAmount":   0.0,
        }

        with patch.object(client._session, "get", return_value=mock_resp):
            result = client.ping()

        # Token must not appear anywhere in the result
        result_str = str(result)
        self.assertNotIn(token, result_str)

        # Full client_id must not appear
        self.assertNotIn("11050100", result_str)

        # Masked client_id should appear
        self.assertIn("1105****00", result_str)

    def test_ping_returns_safe_fields_only(self):
        """ping() response schema must contain only safe fields."""
        client = DhanClient(client_id="11050100", access_token="tok")

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "availabelBalance": 5000.0,
            "utilizedAmount":   100.0,
        }

        with patch.object(client._session, "get", return_value=mock_resp):
            result = client.ping()

        allowed_keys = {"status", "client_id_masked", "available_balance",
                        "used_margin", "currency"}
        self.assertEqual(set(result.keys()), allowed_keys)


class TestDhanClientAuthErrors(unittest.TestCase):
    """Tests for correct exception types on API error responses."""

    def _make_client(self):
        return DhanClient(client_id="11050100", access_token="tok")

    def test_401_raises_auth_error(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 401
        with patch.object(client._session, "get", return_value=mock_resp):
            with self.assertRaises(DhanAuthError):
                client._get("/fundlimit")

    def test_403_raises_auth_error(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 403
        with patch.object(client._session, "get", return_value=mock_resp):
            with self.assertRaises(DhanAuthError):
                client._get("/fundlimit")

    def test_500_raises_api_error(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 500
        with patch.object(client._session, "get", return_value=mock_resp):
            with self.assertRaises(DhanAPIError):
                client._get("/fundlimit")

    def test_auth_error_message_has_no_token(self):
        """Security: DhanAuthError message must never contain the token."""
        token = "my_token_should_not_appear"
        client = DhanClient(client_id="11050100", access_token=token)
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 401
        with patch.object(client._session, "get", return_value=mock_resp):
            try:
                client._get("/fundlimit")
            except DhanAuthError as e:
                self.assertNotIn(token, str(e))

    def test_network_error_raises_api_error(self):
        import requests as req
        client = self._make_client()
        with patch.object(
            client._session, "get",
            side_effect=req.exceptions.ConnectionError("unreachable")
        ):
            with self.assertRaises(DhanAPIError):
                client._get("/fundlimit")


if __name__ == "__main__":
    unittest.main()