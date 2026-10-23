import json
import tempfile
import unittest
import unicodedata
from pathlib import Path

from src.policy_index import build_policy_index, export_policy_index, extract_keywords, normalize_policy_row
from src.central_source import map_central_rows


class PolicyIndexTests(unittest.TestCase):
    def central_row(self, **changes):
        row = {
            "번호": "1", "회계연도": "2026", "소관명": "가상부처",
            "회계명": "일반회계", "프로그램명": "청년 정책",
            "단위사업명": "주거안정", "세부사업명": "청년지원",
            "예산액": "1,000", "사업내용(가능 시)": "월세 지원",
        }
        return {**row, **changes}

    def test_central_hierarchy_and_year_distinguish_businesses(self):
        base = self.central_row()
        variants = [base] + [
            {**base, field: base[field] + "다름"}
            for field in ("소관명", "회계명", "프로그램명", "단위사업명", "세부사업명")
        ] + [{**base, "회계연도": "2025"}]
        policies, skipped = build_policy_index(variants, source="central")
        self.assertEqual((len(policies), skipped), (7, 0))
        self.assertEqual(len({p["id"] for p in policies}), 7)
        self.assertEqual(len({p["legacy_id"] for p in policies[:5]}), 1)
        self.assertTrue(all(p["id_version"] == 2 for p in policies))

    def test_complete_identity_ignores_order_budget_and_enrichment(self):
        rows = [self.central_row(), self.central_row(**{
            "번호": "99", "예산액": "2,000", "사업내용(가능 시)": "추가 설명",
        })]
        policies, skipped = build_policy_index(rows, source="central")
        self.assertEqual((len(policies), skipped), (1, 1))
        self.assertEqual(policies[0]["identity_quality"], "hierarchy")
        self.assertEqual(
            normalize_policy_row(rows[0], source="central")["id"],
            normalize_policy_row(rows[1], source="central")["id"],
        )

    def test_central_identity_normalizes_unicode_whitespace_and_key_order(self):
        first = self.central_row()
        second = {k: unicodedata.normalize("NFD", v) for k, v in reversed(list(first.items()))}
        second["프로그램명"] = "  청년\t 정책\n"
        second["회계연도"] = "2026년"
        policies, skipped = build_policy_index([first, second], source="central")
        self.assertEqual((len(policies), skipped), (1, 1))

    def test_incomplete_hierarchy_keeps_conflicting_rows_and_dedups_repeats(self):
        for field in ("소관명", "회계명", "프로그램명", "단위사업명"):
            with self.subTest(field=field):
                first = self.central_row(**{field: ""})
                second = {**first, "사업내용(가능 시)": "다른 사업"}
                repeat = {**first, "번호": "55"}
                policies, skipped = build_policy_index([first, second, repeat], source="central")
                self.assertEqual((len(policies), skipped), (2, 1))
                self.assertTrue(all(p["identity_quality"] == "row_fallback" for p in policies))
                reversed_policies, _ = build_policy_index([second, first], source="central")
                self.assertEqual({p["id"] for p in policies}, {p["id"] for p in reversed_policies})

    def test_sparse_rows_use_available_evidence_and_effective_defaults(self):
        first = {"사업명": "가상사업", "사업내용": "내용 A"}
        repeat = {**first, "번호": "2", "회계연도": "2026년", "지역": "전국"}
        different = {**first, "사업내용": "내용 B"}
        policies, skipped = build_policy_index(
            [first, repeat, different, {"회계연도": "2026"}],
            source="central", default_year=2026,
        )
        self.assertEqual((len(policies), skipped), (2, 2))

    def test_structured_encoding_avoids_separator_collisions(self):
        first = self.central_row(**{"소관명": "A|B", "회계명": "C"})
        second = self.central_row(**{"소관명": "A", "회계명": "B|C"})
        policies, skipped = build_policy_index([first, second], source="central")
        self.assertEqual((len(policies), skipped), (2, 0))
        self.assertNotEqual(policies[0]["id"], policies[1]["id"])

    def test_collector_mapping_to_index_keeps_ministries_and_dedups_page_overlap(self):
        raw = {
            "acntYr": "2026", "offcNm": "가상부처 A", "acntNm": "일반회계",
            "pgmNm": "프로그램", "actvNm": "단위사업", "sayNm": "같은세부사업",
        }
        rows = map_central_rows([raw, {**raw, "offcNm": "가상부처 B"}, raw], limit=3)
        policies, skipped = build_policy_index(rows, source="central")
        self.assertEqual((len(policies), skipped), (2, 1))

    def test_local_id_compatibility_and_source_separation(self):
        row = {"사업명": "A정책", "지역": "서울", "자치단체": "강동구", "회계연도": "2025"}
        local = normalize_policy_row(row, source="local")
        self.assertEqual(local["id"], "e43a71c1f1f71843")
        self.assertEqual(local["legacy_id"], local["id"])
        self.assertEqual(local["id_version"], 1)
        central = normalize_policy_row(row, source="central")
        self.assertNotEqual(local["id"], central["id"])

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
