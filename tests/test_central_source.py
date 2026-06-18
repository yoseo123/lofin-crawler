import unittest

from src.central_source import map_central_rows, normalize_central_year, validate_central_year


class CentralSourceTests(unittest.TestCase):
    def test_normalize_central_year_from_date_string(self):
        self.assertEqual(normalize_central_year("2026-01-01"), "2026")

    def test_normalize_central_year_from_year_string(self):
        self.assertEqual(normalize_central_year("2027"), "2027")

    def test_validate_central_year_range(self):
        self.assertEqual(validate_central_year("2018"), "2018")
        self.assertEqual(validate_central_year("2026"), "2026")
        with self.assertRaises(ValueError):
            validate_central_year("2017")
        with self.assertRaises(ValueError):
            validate_central_year("2027")

    def test_map_central_rows_keeps_required_columns(self):
        rows = [
            {
                "acntYr": "2026",
                "offcNm": "기획재정부",
                "acntNm": "일반회계",
                "fldNm": "사회복지",
                "sectNm": "노동",
                "pgmNm": "청년일자리",
                "actvNm": "청년채용지원",
                "sayNm": "청년채용 연계사업",
                "thyCfmtnMediAmt": 125000000,
            }
        ]

        got = map_central_rows(rows, limit=10)

        self.assertEqual(len(got), 1)
        row = got[0]
        self.assertEqual(row["번호"], "1")
        self.assertEqual(row["회계연도"], "2026")
        self.assertEqual(row["소관명"], "기획재정부")
        self.assertEqual(row["회계명"], "일반회계")
        self.assertEqual(row["분야명"], "사회복지")
        self.assertEqual(row["부문명"], "노동")
        self.assertEqual(row["프로그램명"], "청년일자리")
        self.assertEqual(row["단위사업명"], "청년채용지원")
        self.assertEqual(row["세부사업명"], "청년채용 연계사업")
        self.assertEqual(row["예산액"], "125,000,000")

    def test_map_central_rows_respects_limit(self):
        rows = [
            {"sayNm": "a"},
            {"sayNm": "b"},
            {"sayNm": "c"},
        ]
        got = map_central_rows(rows, limit=2)
        self.assertEqual(len(got), 2)


if __name__ == "__main__":
    unittest.main()
