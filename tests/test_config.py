from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from expense_tracker.config import AppConfig, load_app_config


class ConfigTests(unittest.TestCase):
    def test_load_app_config_reads_secret_file_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            auth_path = Path(temp_dir) / "auth.token"
            openai_path = Path(temp_dir) / "openai.key"
            auth_path.write_text("auth-secret\n", encoding="utf-8")
            openai_path.write_text("openai-secret\n", encoding="utf-8")

            config = load_app_config(
                {
                    "EXPENSE_TRACKER_AUTH_TOKEN_FILE": str(auth_path),
                    "EXPENSE_TRACKER_OPENAI_API_KEY_FILE": str(openai_path),
                    "EXPENSE_TRACKER_ALLOWED_HOSTS": "tracker.example.com,10.0.0.5",
                    "EXPENSE_TRACKER_SECURE_COOKIES": "true",
                }
            )
            self.assertTrue(config.auth_enabled)
            self.assertEqual(config.resolved_auth_token, "auth-secret")
            self.assertEqual(config.resolved_openai_api_key, "openai-secret")
            self.assertEqual(config.allowed_hosts, ("tracker.example.com", "10.0.0.5"))
            self.assertTrue(config.secure_cookies)

    def test_explicit_app_config_values_override_files(self) -> None:
        config = AppConfig(
            auth_token="inline-auth",
            openai_api_key="inline-openai",
        )

        self.assertEqual(config.resolved_auth_token, "inline-auth")
        self.assertEqual(config.resolved_openai_api_key, "inline-openai")


if __name__ == "__main__":
    unittest.main()
