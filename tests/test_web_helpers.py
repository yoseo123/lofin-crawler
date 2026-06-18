import unittest

from src.web_helpers import (
    build_run_params,
    is_allowed_download_filename,
    user_facing_error_message,
)


class WebHelpersTests(unittest.TestCase):
    def test_build_run_params_basic(self):
        form = {
            "keyword": " 인구 ",
            "year": "2025-01-01",
            "region": "서울",
            "source": "local",
            "limit": "10",
            "output": "out.xlsx",
            "output_format": "xlsx",
            "headed": "on",
        }

        got = build_run_params(form)

        self.assertEqual(got["keyword"], "인구")
        self.assertEqual(got["year"], "2025-01-01")
        self.assertEqual(got["region"], "서울")
        self.assertEqual(got["source"], "local")
        self.assertEqual(got["limit"], 10)
        self.assertEqual(got["chunk_size"], 10000)
        self.assertEqual(got["output_format"], "xlsx")
        self.assertEqual(got["output"], "out.xlsx")
        self.assertEqual(got["json_output"], "out.json")
        self.assertTrue(got["headed"])

    def test_build_run_params_limit_and_output_normalize(self):
        form = {
            "keyword": "인구",
            "year": "",
            "region": "",
            "source": "local",
            "limit": "9999",
            "output": "",
        }

        got = build_run_params(form)

        self.assertEqual(got["limit"], 9999)
        self.assertEqual(got["output_format"], "md")
        self.assertEqual(got["output"], "web_out.md")
        self.assertEqual(got["source"], "local")
        self.assertFalse(got["headed"])

    def test_build_run_params_allows_empty_keyword(self):
        form = {
            "keyword": "",
            "year": "2026",
            "region": "",
            "source": "local",
            "limit": "5",
            "output": "all",
        }

        got = build_run_params(form)
        self.assertEqual(got["keyword"], "")
        self.assertEqual(got["year"], "2026-01-01")

    def test_build_run_params_limit_upper_bound_is_500000(self):
        form = {
            "keyword": "인구",
            "year": "",
            "region": "",
            "source": "local",
            "limit": "500001",
            "output": "out.xlsx",
        }

        got = build_run_params(form)
        self.assertEqual(got["limit"], 500000)

    def test_build_run_params_chunk_size(self):
        form = {
            "keyword": "인구",
            "year": "",
            "region": "",
            "source": "local",
            "limit": "500000",
            "chunk_size": "999999",
            "output": "out.md",
        }

        got = build_run_params(form)
        self.assertEqual(got["chunk_size"], 500000)

    def test_build_run_params_year_short_form(self):
        form = {
            "keyword": "인구",
            "year": "2025",
            "region": "전국",
            "source": "local",
            "limit": "5",
            "output": "custom",
        }

        got = build_run_params(form)

        self.assertEqual(got["year"], "2025-01-01")
        self.assertEqual(got["region"], "전국")
        self.assertEqual(got["output"], "custom.md")

    def test_build_run_params_normalizes_all_region_aliases(self):
        for region in ("전국", "전체", "all", "Nationwide"):
            with self.subTest(region=region):
                form = {
                    "keyword": "청년",
                    "year": "2026",
                    "region": region,
                    "source": "local",
                    "limit": "5",
                    "output": "custom",
                }

                got = build_run_params(form)

                self.assertEqual(got["region"], "전국")

    def test_build_run_params_markdown_output(self):
        form = {
            "keyword": "인구",
            "year": "",
            "region": "",
            "source": "local",
            "limit": "5",
            "output": "custom.xlsx",
            "output_format": "md",
        }

        got = build_run_params(form)
        self.assertEqual(got["output_format"], "md")
        self.assertEqual(got["output"], "custom.md")
        self.assertEqual(got["json_output"], "custom.json")

    def test_build_run_params_output_path_is_basename_only(self):
        form = {
            "keyword": "인구",
            "year": "",
            "region": "",
            "source": "local",
            "limit": "5",
            "output": "..\\..\\secret-report",
        }

        got = build_run_params(form)
        self.assertEqual(got["output"], "secret-report.md")

    def test_build_run_params_source_defaults_to_local(self):
        form = {
            "keyword": "인구",
            "year": "",
            "region": "",
            "limit": "5",
            "output": "custom",
        }

        got = build_run_params(form)
        self.assertEqual(got["source"], "local")

    def test_build_run_params_central_uses_fiscal_year(self):
        form = {
            "keyword": "",
            "year": "2026-01-01",
            "region": "",
            "source": "central",
            "fiscal_year": "2026",
            "limit": "5",
            "output": "central.xlsx",
        }

        got = build_run_params(form)
        self.assertEqual(got["source"], "central")
        self.assertEqual(got["year"], "2026")
        self.assertEqual(got["region"], "")

    def test_build_run_params_central_requires_year_range(self):
        form = {
            "keyword": "인구",
            "year": "",
            "region": "",
            "source": "central",
            "fiscal_year": "2017",
            "limit": "5",
            "output": "central.xlsx",
        }

        with self.assertRaises(ValueError):
            build_run_params(form)

    def test_user_facing_error_message_hides_internal_exception(self):
        msg = user_facing_error_message(RuntimeError("Traceback: DB connection refused"))
        self.assertEqual(msg, "실행 중 오류가 발생했습니다. 입력값을 확인하고 다시 시도하세요.")

    def test_user_facing_error_message_keeps_validation_message(self):
        msg = user_facing_error_message(ValueError("중앙정부 회계년은 2018~2026 범위만 가능합니다."))
        self.assertEqual(msg, "중앙정부 회계년은 2018~2026 범위만 가능합니다.")

    def test_is_allowed_download_filename(self):
        self.assertTrue(is_allowed_download_filename("전국 인구정책 현황_20260214_190412.xlsx"))
        self.assertTrue(is_allowed_download_filename("out_20260214_173029.xlsx"))
        self.assertFalse(is_allowed_download_filename("../webapp.py"))
        self.assertFalse(is_allowed_download_filename("debug/1771060396.html"))
        self.assertFalse(is_allowed_download_filename("requirements.txt"))
        self.assertFalse(is_allowed_download_filename(".env.xlsx"))
        self.assertTrue(is_allowed_download_filename("out_20260214_173029.json"))
        self.assertTrue(is_allowed_download_filename("out_20260214_173029.md"))


if __name__ == "__main__":
    unittest.main()
