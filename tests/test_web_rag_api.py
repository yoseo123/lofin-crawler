import unittest

import webapp


class FakeSearchService:
    def search(self, query, *, top_k, filters):
        return [
            {
                "score": 0.9,
                "policy_id": "p1",
                "title": query,
                "year": filters.get("year_from"),
                "region": filters.get("region"),
            }
        ][:top_k]


class WebRagApiTests(unittest.TestCase):
    def setUp(self):
        self.original_service = webapp._similar_search_service
        webapp._similar_search_service = FakeSearchService()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()
        with self.client.session_transaction() as session:
            session["_csrf_token"] = "token"

    def tearDown(self):
        webapp._similar_search_service = self.original_service

    def test_similar_policies_requires_query(self):
        response = self.client.post(
            "/api/similar-policies",
            json={"query": ""},
            headers={"X-CSRF-Token": "token"},
        )

        self.assertEqual(response.status_code, 400)

    def test_similar_policies_returns_results(self):
        response = self.client.post(
            "/api/similar-policies",
            json={"query": "청년 주거", "top_k": 1, "year_from": 2020, "region": "서울"},
            headers={"X-CSRF-Token": "token"},
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["title"], "청년 주거")
        self.assertEqual(payload["results"][0]["region"], "서울")


if __name__ == "__main__":
    unittest.main()
