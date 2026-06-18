import unittest

from src.parser import parse_policy_detail


class ParserFailureContractTests(unittest.TestCase):
    def test_parse_policy_detail_returns_empty_when_no_table(self):
        html = "<div><p>데이터 없음</p></div>"
        got = parse_policy_detail(html)
        self.assertEqual(got, {})


if __name__ == "__main__":
    unittest.main()
