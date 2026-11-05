import base64
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import webapp


class WebSecurityTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "LOFIN_ALLOW_REMOTE": "false", "LOFIN_WEB_PASSWORD": "",
        })
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.outputs = Path(self.tmp.name) / "outputs"
        self.outputs.mkdir()
        self.output_patch = patch.object(webapp, "OUTPUT_DIR", self.outputs)
        self.output_patch.start()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def tearDown(self):
        self.output_patch.stop()
        self.tmp.cleanup()
        self.env.stop()

    def auth(self, password="a" * 40):
        return {"Authorization": "Basic " + base64.b64encode(
            ("lofin:" + password).encode()).decode()}

    def test_local_use_and_remote_denial_cover_all_routes(self):
        for route in ("/", "/api/jobs/active", "/static/app.css"):
            with self.client.get(route) as response:
                self.assertEqual(response.status_code, 200)
            self.assertEqual(self.client.get(route, environ_overrides={
                "REMOTE_ADDR": "192.0.2.1"}).status_code, 403)

    def test_dns_rebinding_and_forwarded_headers_do_not_grant_access(self):
        self.assertEqual(self.client.get("/", base_url="http://evil.example").status_code, 403)
        for header in ("X-Forwarded-For", "X-Forwarded-Host", "Forwarded", "X-Real-IP"):
            self.assertEqual(self.client.get("/", headers={header: "127.0.0.1"}).status_code, 403)
        self.assertEqual(self.client.get("/", environ_overrides={
            "REMOTE_ADDR": "192.0.2.1"}, headers={"X-Forwarded-For": "127.0.0.1"}).status_code, 403)

    def test_remote_flag_alone_fails_closed(self):
        with patch.dict(os.environ, {"LOFIN_ALLOW_REMOTE": "true"}):
            for route in ("/", "/api/jobs/active", "/download/result.md", "/static/app.css"):
                self.assertEqual(self.client.get(route).status_code, 503)

    def test_authentication_required_even_for_loopback_proxy(self):
        with patch.dict(os.environ, {"LOFIN_ALLOW_REMOTE": "true", "LOFIN_WEB_PASSWORD": "a" * 40}):
            for route in ("/", "/api/jobs/active", "/static/app.css"):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 401)
                self.assertIn("Basic", response.headers["WWW-Authenticate"])
                self.assertEqual(self.client.get(route, headers=self.auth("wrong")).status_code, 401)
                with self.client.get(route, headers=self.auth()) as response:
                    self.assertEqual(response.status_code, 200)
            self.assertEqual(self.client.get("/", headers=self.auth(), environ_overrides={
                "REMOTE_ADDR": "192.0.2.1"}).status_code, 200)

    def test_authentication_does_not_replace_csrf_or_origin_checks(self):
        with patch.dict(os.environ, {"LOFIN_ALLOW_REMOTE": "true", "LOFIN_WEB_PASSWORD": "a" * 40}):
            self.assertEqual(self.client.post("/api/run", json={}, headers=self.auth()).status_code, 403)
            with self.client.session_transaction() as session:
                session["_csrf_token"] = "test-token"
            headers = {**self.auth(), "X-CSRF-Token": "test-token", "Origin": "https://evil.example"}
            self.assertEqual(self.client.post("/api/run", json={}, headers=headers).status_code, 403)

    def test_access_check_precedes_body_and_csrf_parsing(self):
        with patch.dict(os.environ, {"LOFIN_ALLOW_REMOTE": "true", "LOFIN_WEB_PASSWORD": "a" * 40}):
            with patch.object(webapp, "_submitted_csrf_token",
                              side_effect=AssertionError("must authenticate first")):
                self.assertEqual(self.client.post("/api/run", json={}).status_code, 401)
        with patch.object(webapp, "_submitted_csrf_token",
                          side_effect=AssertionError("must check peer first")):
            self.assertEqual(self.client.post("/api/run", json={}, environ_overrides={
                "REMOTE_ADDR": "192.0.2.1"}).status_code, 403)

    def test_https_proxy_origin_and_valid_csrf_reach_route(self):
        with patch.dict(os.environ, {"LOFIN_ALLOW_REMOTE": "true", "LOFIN_WEB_PASSWORD": "a" * 40,
                                    "LOFIN_ALLOWED_ORIGINS": "https://crawler.example.com"}):
            with self.client.session_transaction() as session:
                session["_csrf_token"] = "test-token"
            headers = {**self.auth(), "X-CSRF-Token": "test-token",
                       "Origin": "https://crawler.example.com", "X-Forwarded-For": "192.0.2.1"}
            response = self.client.post("/api/similar-policies", json={"query": ""}, headers=headers)
            self.assertEqual(response.status_code, 400)
            self.assertIn("error", response.get_json())

    def test_downloads_only_export_types_in_output_directory(self):
        for name in ("result.md", "result.xlsx", "result.json", "result_manifest.json"):
            (self.outputs / name).write_text("test")
            response = self.client.get("/download/" + name)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            response.close()
        for name in ("README.md", "missing.json", "../README.md", "out_resume.sqlite3",
                     "out_resume.sqlite3-wal", "out_resume.sqlite3.lock", ".result.md.123.tmp", "data.jsonl"):
            self.assertIn(self.client.get("/download/" + name).status_code, (400, 404))

    def test_symlinks_and_directories_are_not_downloaded_or_listed(self):
        outside = Path(self.tmp.name) / "secret.md"
        outside.write_text("secret")
        (self.outputs / "linked.md").symlink_to(outside)
        (self.outputs / "directory.md").mkdir()
        (self.outputs / "ok.md").write_text("ok")
        self.assertEqual(webapp._recent_outputs(), ["ok.md"])
        self.assertEqual(self.client.get("/download/linked.md").status_code, 404)
        self.assertEqual(self.client.get("/download/directory.md").status_code, 404)

    def test_single_and_chunked_writers_use_output_directory(self):
        params = {"output": "sample.md", "output_format": "md", "chunk_size": 2,
                  "source": "local", "year": "2026"}
        result = webapp._write_output_files([{"사업명": "test"}], params)
        self.assertTrue((self.outputs / result["file"]).is_file())
        writer = webapp._create_chunked_writer({**params, "output": "chunk.md"})
        writer.add_row({"사업명": "test"})
        result = writer.close()
        for filename in result.files + result.json_files:
            self.assertTrue((self.outputs / filename).is_file())

    def test_legacy_post_shares_queue_and_rate_limit(self):
        with self.client.session_transaction() as session:
            session["_csrf_token"] = "test-token"
        headers = {"X-CSRF-Token": "test-token"}
        with patch.object(webapp, "_is_run_rate_limited", return_value=True):
            for route in ("/", "/api/run"):
                self.assertEqual(self.client.post(route, data={}, headers=headers).status_code, 429)
        job = {"id": "existing", "status": "running"}
        with patch.object(webapp, "_jobs", {"existing": job}), \
                patch.object(webapp, "_active_job_id", "existing"), \
                patch.object(webapp, "_is_run_rate_limited", return_value=False), \
                patch.object(webapp, "crawl", side_effect=AssertionError("must not crawl")):
            for route in ("/", "/api/run"):
                self.assertEqual(self.client.post(route, data={}, headers=headers).status_code, 409)
        with patch.object(webapp, "_jobs", {}), \
                patch.object(webapp, "_active_job_id", None), \
                patch.object(webapp, "_is_run_rate_limited", return_value=False), \
                patch.object(webapp.threading, "Thread") as thread:
            response = self.client.post("/", data={}, headers=headers)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.get_json()["job"]["status"], "queued")
            thread.return_value.start.assert_called_once()
            self.assertEqual(self.client.post("/api/run", data={}, headers=headers).status_code, 409)

    def test_rate_key_does_not_trust_forwarded_for(self):
        with webapp.app.test_request_context(headers={"X-Forwarded-For": "attacker"},
                                            environ_base={"REMOTE_ADDR": "192.0.2.7"}):
            self.assertEqual(webapp._client_key(), "192.0.2.7")


if __name__ == "__main__":
    unittest.main()
