import unittest

from crawl_lofin import _parse_post_form_data


class CrawlLofinRequestTests(unittest.TestCase):
    def test_parse_post_form_data_keeps_blank_nationwide_region(self):
        got = _parse_post_form_data("inqYr=2026&inqCap=&inqSgg=&srchCrstCn=%EC%B2%AD%EB%85%84")

        self.assertEqual(got["inqYr"], "2026")
        self.assertEqual(got["inqCap"], "")
        self.assertEqual(got["inqSgg"], "")
        self.assertEqual(got["srchCrstCn"], "청년")


if __name__ == "__main__":
    unittest.main()
