import unittest

from src.exporter import _ordered_headers


class ExporterHeaderTests(unittest.TestCase):
    def test_ordered_headers_places_municipality_after_region(self):
        rows = [
            {
                "번호": "1",
                "사업명": "인구정책",
                "지역": "서울",
                "회계연도": "2025",
                "자치단체": "강동구",
            }
        ]

        headers = _ordered_headers(rows)

        self.assertIn("자치단체", headers)
        self.assertEqual(headers.index("자치단체"), headers.index("지역") + 1)


if __name__ == "__main__":
    unittest.main()
