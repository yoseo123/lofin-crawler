import tempfile
import unittest
from pathlib import Path

from src.parser import (
    TARGET_FIELDS,
    parse_policy_detail,
    parse_policy_detail_from_script,
    parse_policy_detail_from_script_object,
    select_target_fields,
)
from src.exporter import export_to_excel, export_to_markdown


class ParserTests(unittest.TestCase):
    def test_parse_policy_detail_from_two_column_table(self):
        html = """
        <div>
          <h3>세부사업 내용</h3>
          <table>
            <tr><th>정책명</th><td>인구활력 지원사업</td></tr>
            <tr><th>소관부처</th><td>행정안전부</td></tr>
            <tr><th>지원대상</th><td>청년, 신혼부부</td></tr>
          </table>
        </div>
        """

        got = parse_policy_detail(html)

        self.assertEqual(got["정책명"], "인구활력 지원사업")
        self.assertEqual(got["소관부처"], "행정안전부")
        self.assertEqual(got["지원대상"], "청년, 신혼부부")

    def test_parse_policy_detail_from_multi_pair_row(self):
        html = """
        <table>
          <tr>
            <th>회계연도</th><td>2026</td>
            <th>조직</th><td>기획재정부</td>
          </tr>
          <tr>
            <th>사업목적</th><td>인구구조 대응</td>
            <th>사업기간</th><td>2026~2030</td>
          </tr>
        </table>
        """
        got = parse_policy_detail(html)

        self.assertEqual(got["회계연도"], "2026")
        self.assertEqual(got["조직"], "기획재정부")
        self.assertEqual(got["사업목적"], "인구구조 대응")
        self.assertEqual(got["사업기간"], "2026~2030")

    def test_parse_policy_detail_from_div_and_list_layout(self):
        html = """
        <div class="view_content">
          <div class="row">
            <div class="item1">회계연도</div>
            <div class="item2">2026</div>
            <div class="item1">조직</div>
            <div class="item2">기획조정실</div>
          </div>
        </div>
        <ul class="blit">
          <li>사업목적 : 서울시 인구정책 대응력 제고</li>
          <li>사업기간 : 2026-01-01 ~ 2030-01-31</li>
          <li>총사업비 : 50,000,000</li>
          <li>사업내용 : 위원회 운영</li>
          <li>지원형태 : 10 (직접수행)</li>
          <li>지원조건 : </li>
        </ul>
        """
        got = parse_policy_detail(html)

        self.assertEqual(got["회계연도"], "2026")
        self.assertEqual(got["조직"], "기획조정실")
        self.assertEqual(got["사업목적"], "서울시 인구정책 대응력 제고")
        self.assertEqual(got["사업기간"], "2026-01-01 ~ 2030-01-31")
        self.assertEqual(got["총사업비"], "50,000,000")
        self.assertEqual(got["사업내용"], "위원회 운영")
        self.assertEqual(got["지원형태"], "10 (직접수행)")


class ExportTests(unittest.TestCase):
    def test_export_to_excel_creates_xlsx(self):
        rows = [
            {"검색어": "인구", "정책명": "인구활력 지원사업", "소관부처": "행정안전부"},
            {"검색어": "인구", "정책명": "저출생 대응 패키지", "소관부처": "보건복지부"},
        ]

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "policies.xlsx"
            export_to_excel(rows, out)
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 0)

    def test_export_to_markdown_creates_readable_md(self):
        rows = [
            {"번호": "1", "사업명": "인구활력 지원사업", "지역": "서울", "회계연도": "2026", "사업내용": "지원 내용"},
        ]

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "policies.md"
            export_to_markdown(rows, out)
            self.assertTrue(out.exists())
            text = out.read_text(encoding="utf-8")
            self.assertIn("# Lofin 정책 수집 결과", text)
            self.assertIn("### 1. 인구활력 지원사업", text)
            self.assertIn("- **사업내용**: 지원 내용", text)


class FieldSelectionTests(unittest.TestCase):
    def test_select_target_fields_keeps_only_requested_columns(self):
        parsed = {
            "회계 연도": "2026",
            "조직": "행정안전부",
            "사업목적:": "인구 활력 제고",
            "사업기간": "2026-2028",
            "총사업비": "100억원",
            "사업내용": "지원 내용",
            "지원형태": "현금",
            "지원조건": "조건 없음",
            "사업위치": "전국",
            "시행주체": "지자체",
            "추진근거": "법령",
            "추진경위": "정부 계획",
            "추진계획": "연차 추진",
            "불필요컬럼": "제외",
        }

        got = select_target_fields(parsed)

        self.assertEqual(list(got.keys()), TARGET_FIELDS)
        self.assertEqual(got["회계연도"], "2026")
        self.assertEqual(got["사업목적"], "인구 활력 제고")
        self.assertNotIn("불필요컬럼", got)

    def test_select_target_fields_with_variant_labels(self):
        parsed = {
            "회계연도(당해)": "2026",
            "조직명": "행정안전부",
            "사업내용(요약)": "세부 내용",
            "추진 계획": "상반기 집행",
        }

        got = select_target_fields(parsed)

        self.assertEqual(got["회계연도"], "2026")
        self.assertEqual(got["조직"], "행정안전부")
        self.assertEqual(got["사업내용"], "세부 내용")
        self.assertEqual(got["추진계획"], "상반기 집행")

    def test_select_target_fields_with_extended_policy_labels(self):
        parsed = {
            "사업위치(소재지)": "서울특별시",
            "시행주체(부서)": "서울시 인구정책과",
            "추진근거 및 필요성": "인구감소지역 지원 특별법",
            "추진경위(배경)": "청년 인구 유출 심화",
            "추진계획(향후)": "연차별 시행계획 추진",
        }

        got = select_target_fields(parsed)

        self.assertEqual(got["사업위치"], "서울특별시")
        self.assertEqual(got["시행주체"], "서울시 인구정책과")
        self.assertEqual(got["추진근거"], "인구감소지역 지원 특별법")
        self.assertEqual(got["추진경위"], "청년 인구 유출 심화")
        self.assertEqual(got["추진계획"], "연차별 시행계획 추진")


class ScriptFallbackTests(unittest.TestCase):
    def test_parse_policy_detail_from_script_list3(self):
        html = """
        <script>
        var list3 = {"fyr":"2026","deptNm":"기획조정실",
        "bizPurpCn":"인구구조 변화 대응",
        "bizBgngYmd":"20260101","bizEndYmd":"20301231",
        "tbc":50000000,"bizCn":"사업 내용\\n상세",
        "spoFomNm":"직접수행"};
        </script>
        """
        got = parse_policy_detail_from_script(html)

        self.assertEqual(got["회계연도"], "2026")
        self.assertEqual(got["조직"], "기획조정실")
        self.assertEqual(got["사업목적"], "인구구조 변화 대응")
        self.assertEqual(got["사업기간"], "2026-01-01 ~ 2030-12-31")
        self.assertEqual(got["총사업비"], "50000000")
        self.assertIn("사업 내용", got["사업내용"])
        self.assertEqual(got["지원형태"], "직접수행")

    def test_parse_policy_detail_from_script_object(self):
        obj = {
            "fyr": "2026",
            "deptNm": "기획조정실_조직담당관",
            "bizPurpCn": "총괄 조정",
            "bizBgngYmd": "20260101",
            "bizEndYmd": "20300131",
            "tbc": 50000000,
            "bizCn": "위원회 운영",
            "spoFomNm": "직접수행",
        }
        got = parse_policy_detail_from_script_object(obj)

        self.assertEqual(got["회계연도"], "2026")
        self.assertEqual(got["조직"], "기획조정실_조직담당관")
        self.assertEqual(got["사업목적"], "총괄 조정")
        self.assertEqual(got["사업기간"], "2026-01-01 ~ 2030-01-31")
        self.assertEqual(got["총사업비"], "50000000")
        self.assertEqual(got["사업내용"], "위원회 운영")

    def test_parse_policy_detail_from_script_supports_const_assignment(self):
        html = """
        <script>
        const list3 = {"bizLocCn":"전국","impSubjNm":"지자체",
        "bizBssCn":"법령","bizPrgsCn":"정부계획","bizPlanCn":"연차 추진"};
        </script>
        """
        got = parse_policy_detail_from_script(html)

        self.assertEqual(got["사업위치"], "전국")
        self.assertEqual(got["시행주체"], "지자체")
        self.assertEqual(got["추진근거"], "법령")
        self.assertEqual(got["추진경위"], "정부계획")
        self.assertEqual(got["추진계획"], "연차 추진")

    def test_parse_policy_detail_from_script_object_with_variant_keys(self):
        obj = {
            "bizLoc": "서울 전역",
            "impSubj": "서울시",
            "bizBasisCn": "서울시 인구정책 기본조례",
            "bizProgCn": "인구구조 변화 대응 필요",
            "bizPlan": "2026년 단계별 추진",
        }
        got = parse_policy_detail_from_script_object(obj)

        self.assertEqual(got["사업위치"], "서울 전역")
        self.assertEqual(got["시행주체"], "서울시")
        self.assertEqual(got["추진근거"], "서울시 인구정책 기본조례")
        self.assertEqual(got["추진경위"], "인구구조 변화 대응 필요")
        self.assertEqual(got["추진계획"], "2026년 단계별 추진")


if __name__ == "__main__":
    unittest.main()
