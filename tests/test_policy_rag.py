import json
import tempfile
import unittest
from pathlib import Path

from src.policy_rag import (
    build_embedding_batch_jsonl,
    build_embedding_batch_jsonl_files,
    build_policy_embedding_text,
    format_search_result,
    load_policies_from_files,
    parse_embedding_batch_result,
    policy_payload,
    qdrant_point_id,
    rerank_results,
    text_hash,
)


class PolicyRagTests(unittest.TestCase):
    def sample_policy(self):
        return {
            "id": "p1",
            "title": "청년 주거 지원",
            "year": 2026,
            "region": "서울",
            "municipality": "강동구",
            "category": "복지국",
            "budget_amount": 1000,
            "keywords": ["청년", "주거"],
            "description": "청년 월세 부담 완화",
            "raw": {
                "사업명": "청년 주거 지원",
                "사업목적": "청년 월세 부담 완화",
                "사업내용": "월세 지원",
                "추진근거": "조례",
            },
        }

    def test_build_policy_embedding_text_prefers_structured_fields(self):
        text = build_policy_embedding_text(self.sample_policy())

        self.assertIn("사업명: 청년 주거 지원", text)
        self.assertIn("사업목적: 청년 월세 부담 완화", text)
        self.assertIn("추진근거: 조례", text)

    def test_policy_payload_keeps_filter_fields_and_hash(self):
        policy = self.sample_policy()
        text = build_policy_embedding_text(policy)
        payload = policy_payload(policy, embedding_text=text, source_file="part001.json")

        self.assertEqual(payload["policy_id"], "p1")
        self.assertEqual(payload["year"], 2026)
        self.assertEqual(payload["region"], "서울")
        self.assertEqual(payload["text_hash"], text_hash(text))
        self.assertEqual(payload["raw"]["사업내용"], "월세 지원")

    def test_build_embedding_batch_jsonl(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "batch.jsonl"
            result = build_embedding_batch_jsonl([self.sample_policy()], output, model="m", dimensions=8)
            line = json.loads(output.read_text(encoding="utf-8").strip())

        self.assertEqual(result.requests, 1)
        self.assertEqual(line["method"], "POST")
        self.assertEqual(line["url"], "/v1/embeddings")
        self.assertEqual(line["body"]["dimensions"], 8)
        self.assertIn("청년 주거 지원", line["body"]["input"])

    def test_build_embedding_batch_jsonl_files_splits_large_inputs(self):
        policies = []
        for idx in range(3):
            policy = self.sample_policy()
            policy["id"] = f"p{idx}"
            policies.append(policy)

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "batch.jsonl"
            result = build_embedding_batch_jsonl_files(
                policies,
                output,
                model="m",
                dimensions=8,
                max_requests_per_file=2,
            )

        self.assertEqual(result.requests, 3)
        self.assertEqual([item.requests for item in result.files], [2, 1])
        self.assertTrue(result.files[0].path.endswith("batch_001.jsonl"))

    def test_qdrant_point_id_is_deterministic_uuid(self):
        point_id = qdrant_point_id("p1")

        self.assertEqual(point_id, qdrant_point_id("p1"))
        self.assertRegex(point_id, r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

    def test_parse_embedding_batch_result(self):
        payload = {
            "custom_id": "p1:abc",
            "response": {"body": {"data": [{"embedding": [0.1, 0.2]}]}},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "result.jsonl"
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            got = parse_embedding_batch_result(path)

        self.assertEqual(got, {"p1": [0.1, 0.2]})

    def test_load_policies_from_files_deduplicates(self):
        payload = {"source_file": "a.md", "policies": [self.sample_policy(), self.sample_policy()]}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "part.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            policies, skipped = load_policies_from_files([path])

        self.assertEqual(len(policies), 1)
        self.assertEqual(skipped, 1)
        self.assertEqual(policies[0]["source_file"], "a.md")

    def test_rerank_and_format_search_result(self):
        results = [
            {"score": 0.7, "payload": {"title": "기타", "description": "", "keywords": []}},
            {"score": 0.69, "payload": {"title": "청년 주거 지원", "description": "월세", "keywords": ["청년"]}},
        ]
        ranked = rerank_results("청년 주거", results)
        formatted = format_search_result(ranked[0])

        self.assertEqual(formatted["title"], "청년 주거 지원")
        self.assertGreater(formatted["score"], 0.69)


if __name__ == "__main__":
    unittest.main()
