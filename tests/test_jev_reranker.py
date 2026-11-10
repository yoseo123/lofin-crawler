import copy
import os
import threading
import unittest
from unittest.mock import Mock, patch

from src.jev_reranker import JevReranker
from src.policy_rag import SimilarPolicySearch, rerank_results
import webapp


def answer(value):
    return {"answers": {"relevant": {"type": "noul", "noul": value}}}


def candidates(count=4):
    return [{"score": 1 - i / 100, "payload": {"title": str(i), "policy_id": str(i)}}
            for i in range(count)]


class JevRerankerTests(unittest.TestCase):
    def make(self, values, **kwargs):
        reranker = JevReranker(api_key="test-only", **kwargs)
        def post(body):
            i = int(body["state"]["policy"].splitlines()[0].split(": ")[1])
            value = values[i]
            if isinstance(value, Exception):
                raise value
            return answer(value)
        reranker._post = Mock(side_effect=post)
        return reranker

    def test_threshold_sort_partial_failure_and_immutability(self):
        original = candidates(); saved = copy.deepcopy(original)
        reranker = self.make([0.7, 0.59, TimeoutError(), 0.9])
        ranked = reranker.rerank("가상 정책", original)
        self.assertEqual([x["payload"]["title"] for x in ranked], ["3", "0", "2"])
        self.assertEqual([x["jev_relevant"] for x in ranked], [0.9, 0.7, None])
        self.assertEqual(ranked[0]["score"], original[3]["score"])
        self.assertEqual(original, saved)

    def test_all_failed_or_below_fallback_original_order(self):
        for values in [[TimeoutError()] * 4, [0.1, 0.5, 0.59, 0], [0.1, TimeoutError(), 0.2, 0.3]]:
            ranked = self.make(values).rerank("query", candidates())
            self.assertEqual([x["payload"]["title"] for x in ranked], ["0", "1", "2", "3"])

    def test_threshold_equality_and_stable_ties(self):
        ranked = self.make([0.6, 0.6, 0.2, 0.6]).rerank("query", candidates())
        self.assertEqual([x["payload"]["title"] for x in ranked], ["0", "1", "3"])

    def test_invalid_responses_are_failures(self):
        responses = [None, {}, {"answers": {}}, answer(True), answer("0.9"), answer(float("nan")),
                     answer(float("inf")), answer(-0.1), answer(1.1),
                     {"answers": {"relevant": {"type": "choice", "noul": 0.8}}}]
        for response in responses:
            reranker = JevReranker(api_key="test")
            with patch.object(reranker, "_post", return_value=response):
                self.assertIsNone(reranker.rerank("query", candidates(1))[0]["jev_relevant"])

    def test_transport_uses_fixed_endpoint_and_timeout(self):
        from src.jev_reranker import ENDPOINT, _NoRedirect
        reranker = JevReranker(api_key="test", timeout=2.5)
        with patch("src.jev_reranker.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read1.side_effect = [b'{"answers": {}}', b""]
            self.assertEqual(reranker._post({"state": "synthetic"}), {"answers": {}})
            args = opener.return_value.open.call_args
            self.assertEqual(args.args[0].full_url, ENDPOINT)
            self.assertEqual(args.args[0].get_header("Authorization"), "Bearer test")
            self.assertEqual(args.kwargs["timeout"], 2.5)
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid"))

    def test_transport_response_size_and_read_deadline(self):
        reranker = JevReranker(api_key="test", timeout=1)
        with patch("src.jev_reranker.build_opener") as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.read1.return_value = b"x" * 8192
            with self.assertRaises(ValueError):
                reranker._post({})
            self.assertLessEqual(response.read1.call_count, 9)
        with patch("src.jev_reranker.build_opener"), patch("src.jev_reranker.time.monotonic", side_effect=[0, 2]):
            with self.assertRaises(TimeoutError):
                reranker._post({})

    def test_request_contract(self):
        reranker = self.make([0.8])
        reranker.rerank("청년 주거", candidates(1))
        body = reranker._post.call_args.args[0]
        self.assertEqual(body["model"], "jev-1.14")
        self.assertEqual(body["state"]["query"], "청년 주거")
        self.assertEqual(body["questions"]["relevant"]["type"], "noul")
        self.assertEqual(set(body["questions"]["relevant"]["criteria"]), {"true", "false"})

    def test_parallel_24_candidates_and_bound(self):
        reranker = JevReranker(api_key="test")
        barrier = threading.Barrier(24, timeout=5)
        def post(body):
            barrier.wait()
            return answer(0.8)
        with patch.object(reranker, "_post", side_effect=post) as post_mock:
            ranked = reranker.rerank("query", candidates(26))
        self.assertEqual(post_mock.call_count, 24)
        self.assertEqual(len(ranked), 26)
        self.assertEqual([x["jev_relevant"] for x in ranked[-2:]], [None, None])

    def test_empty_no_calls(self):
        reranker = self.make([])
        self.assertEqual(reranker.rerank("query", []), [])
        reranker._post.assert_not_called()

    def test_configuration(self):
        with patch.dict(os.environ, {"BEATAPI_API_KEY": "fallback", "LOFIN_JEV_THRESHOLD": "0.7"}, clear=True):
            self.assertEqual(JevReranker().threshold, 0.7)
            self.assertEqual(JevReranker().api_key, "fallback")
            with patch.dict(os.environ, {"LOFIN_JEV_API_KEY": "primary"}):
                self.assertEqual(JevReranker().api_key, "primary")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError): JevReranker()
        for value in [-1, 2, float("nan"), float("inf"), "invalid"]:
            with self.assertRaises(ValueError): JevReranker(api_key="test", threshold=value)
        for value in [0, -1, float("nan"), float("inf")]:
            with self.assertRaises(ValueError): JevReranker(api_key="test", timeout=value)

    def test_search_optional_reranker_and_limits(self):
        for enabled in [False, True]:
            store = Mock(); store.search.return_value = candidates()
            embedder = Mock(); embedder.embed.return_value = [0.1]
            reranker = self.make([0.7, 0.9, 0.2, 0.3]) if enabled else None
            service = SimilarPolicySearch(embedding_client=embedder, store=store, reranker=reranker)
            results = service.search("query", top_k=2, filters={"region": "서울"})
            store.search.assert_called_once_with([0.1], top_k=24 if enabled else 6, filters={"region": "서울"})
            self.assertEqual(results[0]["policy_id"], "1" if enabled else "0")
            self.assertEqual(results[0]["jev_relevant"], 0.9 if enabled else None)

    def test_search_fallback_preserves_keyword_ranking(self):
        items = candidates(); items[3]["payload"]["title"] = "query query"
        store = Mock(); store.search.return_value = items
        reranker = JevReranker(api_key="test")
        with patch.object(reranker, "_post", side_effect=TimeoutError()):
            results = SimilarPolicySearch(embedding_client=Mock(), store=store, reranker=reranker).search("query", top_k=4)
        self.assertEqual([x["policy_id"] for x in results],
                         [x["payload"]["policy_id"] for x in rerank_results("query", items)])

    def test_webapp_configuration_and_initialization_failure(self):
        for key in [None, "LOFIN_JEV_API_KEY", "BEATAPI_API_KEY"]:
            for fails in [False, True]:
                env = {key: "test"} if key else {}
                with patch.dict(os.environ, env, clear=True), patch.object(webapp, "_similar_search_service", None), \
                     patch.object(webapp, "OpenAIEmbeddingClient"), patch.object(webapp, "QdrantPolicyStore"), \
                     patch.object(webapp, "JevReranker", side_effect=ValueError("secret") if fails else None) as factory, \
                     patch.object(webapp.app.logger, "warning") as warning:
                    service = webapp._similar_policy_service()
                    if key:
                        factory.assert_called_once_with()
                        self.assertEqual(service.reranker, None if fails else factory.return_value)
                        self.assertEqual(warning.call_count, int(fails))
                    else:
                        factory.assert_not_called(); self.assertIsNone(service.reranker)
