import json
import tempfile
import unittest
from pathlib import Path

from src.policy_index import build_policy_index, export_policy_index, extract_keywords, normalize_policy_row


class PolicyIndexTests(unittest.TestCase):
    def test_extract_keywords(self):
        got = extract_keywords("청년 주거 지원 사업")
        self.assertEqual(got, ["청년", "주거"])

    def test_normalize_local_policy_row(self):
        row = {
            "사업명": "청년주거지원",
            "지역": "서울특별시",
            "자치단체": "강동구",
            "회계연도": "2025",
            "조직": "복지국",
            "총사업비": "5,000,000,000원",
            "사업목적": "청년 주거비 부담 완화",
            "사업내용": "월세 지원",
        }
        got = normalize_policy_row(row, source="local", source_url="https://example.com")
        assert got is not None

        self.assertEqual(got["title"], "청년주거지원")
        self.assertEqual(got["source"], "local")
        self.assertEqual(got["region"], "서울특별시")
        self.assertEqual(got["municipality"], "강동구")
        self.assertEqual(got["year"], 2025)
        self.assertEqual(got["budget_amount"], 5000000000)
        self.assertEqual(got["category"], "복지국")
        self.assertEqual(got["source_url"], "https://example.com")
        self.assertIn("청년주거지원", got["keywords"])

    def test_build_policy_index_dedup_and_skip(self):
        rows = [
            {"사업명": "A정책", "지역": "서울", "자치단체": "강동구", "회계연도": "2025"},
            {"사업명": "A정책", "지역": "서울", "자치단체": "강동구", "회계연도": "2025"},
            {"사업명": "A정책", "지역": "서울", "자치단체": "종로구", "회계연도": "2025"},
            {"사업명": "", "지역": "서울", "자치단체": "강동구", "회계연도": "2025"},
        ]

        policies, skipped = build_policy_index(rows, source="local", source_url="")
        self.assertEqual(len(policies), 2)
        self.assertEqual(skipped, 2)

    def test_build_policy_index_uses_default_year_region(self):
        rows = [
            {"사업명": "B정책", "지역": "", "회계연도": ""},
        ]

        policies, skipped = build_policy_index(
            rows,
            source="local",
            source_url="",
            default_year="2026-01-01",
            default_region="서울특별시",
        )
        self.assertEqual(skipped, 0)
        self.assertEqual(len(policies), 1)
        self.assertEqual(policies[0]["year"], 2026)
        self.assertEqual(policies[0]["region"], "서울특별시")

    def test_export_policy_index_writes_search_payload(self):
        rows = [{"사업명": "A정책", "지역": "서울", "회계연도": "2025"}]
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "policies.json"
            result = export_policy_index(rows, output, source="local", source_file="policies.md")
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result.indexed, 1)
        self.assertEqual(payload["source_file"], "policies.md")
        self.assertEqual(payload["xlsx_file"], "")
        self.assertEqual(payload["policies"][0]["title"], "A정책")


if __name__ == "__main__":
    unittest.main()
