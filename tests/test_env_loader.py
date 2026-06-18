import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.env_loader import load_env_file


class EnvLoaderTests(unittest.TestCase):
    def test_load_env_file_sets_values(self):
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "LOFIN_WEB_SECRET_KEY=test-secret",
                        "LOFIN_ALLOWED_ORIGINS=http://127.0.0.1:8787",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                loaded = load_env_file(env_path)
                self.assertTrue(loaded)
                self.assertEqual(os.environ.get("LOFIN_WEB_SECRET_KEY"), "test-secret")
                self.assertEqual(os.environ.get("LOFIN_ALLOWED_ORIGINS"), "http://127.0.0.1:8787")

    def test_load_env_file_does_not_override_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            env_path.write_text("LOFIN_WEB_SECRET_KEY=from-file\n", encoding="utf-8")

            with patch.dict(os.environ, {"LOFIN_WEB_SECRET_KEY": "from-env"}, clear=True):
                load_env_file(env_path)
                self.assertEqual(os.environ.get("LOFIN_WEB_SECRET_KEY"), "from-env")

    def test_load_env_file_can_override(self):
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            env_path.write_text("LOFIN_WEB_SECRET_KEY=from-file\n", encoding="utf-8")

            with patch.dict(os.environ, {"LOFIN_WEB_SECRET_KEY": "from-env"}, clear=True):
                load_env_file(env_path, override=True)
                self.assertEqual(os.environ.get("LOFIN_WEB_SECRET_KEY"), "from-file")


if __name__ == "__main__":
    unittest.main()
