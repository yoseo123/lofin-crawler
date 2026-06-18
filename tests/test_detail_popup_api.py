import unittest

from src.detail_popup_api import build_popup_detail_form


class DetailPopupApiTests(unittest.TestCase):
    def test_build_popup_detail_form_keeps_required_fields(self):
        entry = {
            "dbizCd": "3240000201430008",
            "lafCd": "1135000",
            "fyr": "2026",
            "inqYmd": "2026-01-01",
            "name": "인구정책위원회 구성·운영",
        }

        got = build_popup_detail_form(entry)

        self.assertEqual(got["dbizCd"], "3240000201430008")
        self.assertEqual(got["lafCd"], "1135000")
        self.assertEqual(got["fyr"], "2026")
        self.assertEqual(got["inqYmd"], "20260101")
        self.assertEqual(got["dbizNm"], "인구정책위원회 구성·운영")

    def test_build_popup_detail_form_omits_empty_values(self):
        entry = {"dbizCd": "A", "inqYmd": "", "name": ""}
        got = build_popup_detail_form(entry)

        self.assertEqual(got, {"dbizCd": "A"})


if __name__ == "__main__":
    unittest.main()
