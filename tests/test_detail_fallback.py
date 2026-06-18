import unittest

from src.detail_fallback import should_open_detail_page


class DetailFallbackTests(unittest.TestCase):
    def test_should_open_when_selected_fields_are_too_few(self):
        selected = {"회계연도": "2026"}
        self.assertTrue(should_open_detail_page(selected))

    def test_should_open_when_priority_fields_are_all_missing(self):
        selected = {
            "회계연도": "2026",
            "조직": "기획조정실",
            "사업목적": "인구구조 대응",
            "사업기간": "2026-01-01 ~ 2030-01-31",
            "총사업비": "50000000",
        }
        self.assertTrue(should_open_detail_page(selected))

    def test_should_not_open_when_any_priority_field_exists(self):
        selected = {
            "회계연도": "2026",
            "조직": "기획조정실",
            "사업목적": "인구구조 대응",
            "사업기간": "2026-01-01 ~ 2030-01-31",
            "총사업비": "50000000",
            "시행주체": "서울시",
        }
        self.assertFalse(should_open_detail_page(selected))


if __name__ == "__main__":
    unittest.main()
