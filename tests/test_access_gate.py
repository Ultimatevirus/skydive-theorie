import importlib
import os
import time
import unittest
from unittest.mock import patch

import app as app_module
from app import app

PROTECTED_PATHS = ["/", "/practice", "/leren", "/metar", "/contact", "/exam"]
GATE_ENV = {"ACCESS_GATE_ENABLED": "1", "ACCESS_CODE": "s3cret-test-code"}


class AccessGateTests(unittest.TestCase):
    def test_protected_pages_redirect_to_gate_when_enabled(self):
        client = app.test_client()
        with patch.dict(os.environ, GATE_ENV):
            for path in PROTECTED_PATHS:
                response = client.get(path)
                self.assertEqual(response.status_code, 302, path)
                self.assertIn("/access-gate", response.headers["Location"], path)

    def test_exempt_routes_bypass_gate_when_enabled(self):
        client = app.test_client()
        with patch.dict(os.environ, GATE_ENV):
            self.assertEqual(client.get("/healthz").status_code, 200)
            self.assertEqual(client.get("/favicon.ico").status_code, 200)
            self.assertEqual(client.get("/favicon.svg").status_code, 200)
            self.assertEqual(
                client.get("/static/style.css").status_code, 200
            )

    def test_correct_code_grants_access_and_redirects_to_next(self):
        client = app.test_client()
        with patch.dict(os.environ, GATE_ENV):
            gate_response = client.get("/practice")
            next_target = gate_response.headers["Location"]

            unlock_response = client.post(
                next_target,
                data={"code": GATE_ENV["ACCESS_CODE"], "next": "/practice"},
                follow_redirects=False,
            )
            self.assertEqual(unlock_response.status_code, 302)
            self.assertTrue(unlock_response.headers["Location"].endswith("/practice"))

            follow_up = client.get("/practice")
            self.assertEqual(follow_up.status_code, 200)

    def test_incorrect_code_is_rejected_and_shows_error(self):
        client = app.test_client()
        with patch.dict(os.environ, GATE_ENV):
            response = client.post(
                "/access-gate",
                data={"code": "wrong-code", "next": "/"},
            )
            self.assertEqual(response.status_code, 401)
            html = response.get_data(as_text=True)
            self.assertIn("onjuist", html)

            with client.session_transaction() as session:
                self.assertNotIn("access_granted_at", session)

    def test_next_parameter_is_restricted_to_same_origin_paths(self):
        client = app.test_client()
        with patch.dict(os.environ, GATE_ENV):
            response = client.post(
                "/access-gate",
                data={"code": GATE_ENV["ACCESS_CODE"], "next": "https://evil.example.com/phish"},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers["Location"].endswith("/"))
            self.assertNotIn("evil.example.com", response.headers["Location"])

    def test_gate_bypassed_entirely_when_flag_disabled(self):
        client = app.test_client()
        with patch.dict(os.environ, {"ACCESS_GATE_ENABLED": "0", "ACCESS_CODE": "s3cret-test-code"}):
            for path in PROTECTED_PATHS:
                response = client.get(path)
                if response.status_code == 302:
                    self.assertNotIn("/access-gate", response.headers["Location"], path)
                else:
                    self.assertEqual(response.status_code, 200, path)

    def test_gate_no_ops_when_code_not_configured(self):
        client = app.test_client()
        env = dict(os.environ)
        env.pop("ACCESS_CODE", None)
        env["ACCESS_GATE_ENABLED"] = "1"
        with patch.dict(os.environ, env, clear=True):
            for path in PROTECTED_PATHS:
                response = client.get(path)
                if response.status_code == 302:
                    self.assertNotIn("/access-gate", response.headers["Location"], path)
                else:
                    self.assertEqual(response.status_code, 200, path)

    def test_expired_token_forces_reauthentication(self):
        client = app.test_client()
        with patch.dict(os.environ, GATE_ENV):
            with client.session_transaction() as session:
                session["access_granted_at"] = time.time() - (8 * 24 * 3600)

            response = client.get("/practice")
            self.assertEqual(response.status_code, 302)
            self.assertIn("/access-gate", response.headers["Location"])

    def test_production_import_requires_access_code(self):
        env_overrides = {
            "APP_ENV": "production",
            "SECRET_KEY": "some-production-secret",
            "ACCESS_GATE_ENABLED": "1",
        }
        env = dict(os.environ)
        env.update(env_overrides)
        env.pop("ACCESS_CODE", None)
        try:
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaises(RuntimeError):
                    importlib.reload(app_module)
        finally:
            importlib.reload(app_module)


if __name__ == "__main__":
    unittest.main()
