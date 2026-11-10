from __future__ import annotations

from dataclasses import dataclass
import glob
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable
import uuid


DEFAULT_COLLECTION = "policy_cases_v1"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-large"
DEFAULT_EMBEDDING_DIMENSIONS = 1536
DEFAULT_TOP_K = 10

TEXT_FIELDS: tuple[str, ...] = (
    "사업명",
    "세부사업명",
    "지역",
    "자치단체",
    "회계연도",
    "조직",
    "소관명",
    "사업목적",
    "사업내용",
    "사업내용(가능 시)",
    "지원조건",
    "사업위치",
    "시행주체",
    "추진근거",
    "추진계획",
)


@dataclass(frozen=True)
class BatchBuildResult:
    requests: int
    skipped: int


@dataclass(frozen=True)
class BatchFileResult:
    path: str
    requests: int


@dataclass(frozen=True)
class BatchBuildFilesResult:
    files: list[BatchFileResult]
    requests: int
    skipped: int


def load_policy_index_file(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    policies = payload.get("policies", [])
    if not isinstance(policies, list):
        return []
    source_file = str(payload.get("source_file") or Path(path).name)
    out: list[dict[str, Any]] = []
    for item in policies:
        if not isinstance(item, dict):
            continue
        policy = dict(item)
        policy.setdefault("source_file", source_file)
        out.append(policy)
    return out


def iter_policy_index_files(patterns: Iterable[str], *, base_dir: Path | None = None) -> list[Path]:
    base = Path(base_dir or ".")
    files: list[Path] = []
    for pattern in patterns:
        matches = glob.glob(str(base / pattern)) if not Path(pattern).is_absolute() else glob.glob(pattern)
        for match in matches:
            path = Path(match)
            if path.name.endswith("_manifest.json"):
                continue
            if path.is_file() and path.suffix.lower() == ".json" and path not in files:
                files.append(path)
    return sorted(files)


def load_policies_from_files(paths: Iterable[Path]) -> tuple[list[dict[str, Any]], int]:
    policies: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped = 0
    for path in paths:
        for policy in load_policy_index_file(path):
            policy_id = str(policy.get("id") or "").strip()
            if not policy_id or policy_id in seen:
                skipped += 1
                continue
            seen.add(policy_id)
            policies.append(policy)
    return policies, skipped


def build_policy_embedding_text(policy: dict[str, Any]) -> str:
    raw = policy.get("raw") if isinstance(policy.get("raw"), dict) else {}
    parts: list[str] = []
    for field in TEXT_FIELDS:
        value = raw.get(field) if isinstance(raw, dict) else None
        if value is None:
            key = {
                "사업명": "title",
                "세부사업명": "title",
                "지역": "region",
                "자치단체": "municipality",
                "회계연도": "year",
                "조직": "category",
                "소관명": "category",
                "사업내용": "description",
                "사업내용(가능 시)": "description",
            }.get(field)
            value = policy.get(key, "") if key else ""
        text = str(value or "").strip()
        if text:
            parts.append(f"{field}: {text}")
    return "\n".join(parts)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def qdrant_point_id(policy_id: str) -> str:
    digest = hashlib.md5(str(policy_id).encode("utf-8"), usedforsecurity=False).hexdigest()
    return str(uuid.UUID(digest))


def policy_payload(policy: dict[str, Any], *, embedding_text: str, source_file: str = "") -> dict[str, Any]:
    raw = policy.get("raw") if isinstance(policy.get("raw"), dict) else {}
    return {
        "policy_id": str(policy.get("id") or ""),
        "title": str(policy.get("title") or ""),
        "year": policy.get("year"),
        "region": str(policy.get("region") or ""),
        "municipality": str(policy.get("municipality") or ""),
        "category": str(policy.get("category") or ""),
        "budget_amount": policy.get("budget_amount"),
        "keywords": list(policy.get("keywords") or []),
        "source": str(policy.get("source") or ""),
        "source_file": str(policy.get("source_file") or source_file or ""),
        "description": str(policy.get("description") or ""),
        "raw": {str(k): str(v or "") for k, v in raw.items()},
        "text_hash": text_hash(embedding_text),
    }


def build_embedding_batch_jsonl(
    policies: Iterable[dict[str, Any]],
    output_path: Path,
    *,
    model: str = DEFAULT_EMBEDDING_MODEL,
    dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
) -> BatchBuildResult:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    requests = 0
    skipped = 0
    with output.open("w", encoding="utf-8") as handle:
        for policy in policies:
            policy_id = str(policy.get("id") or "").strip()
            embedding_text = build_policy_embedding_text(policy)
            if not policy_id or not embedding_text:
                skipped += 1
                continue
            custom_id = f"{policy_id}:{text_hash(embedding_text)[:16]}"
            body = {"model": model, "input": embedding_text, "dimensions": int(dimensions)}
            request = {"custom_id": custom_id, "method": "POST", "url": "/v1/embeddings", "body": body}
            handle.write(json.dumps(request, ensure_ascii=False) + "\n")
            requests += 1
    return BatchBuildResult(requests=requests, skipped=skipped)


def _embedding_batch_requests(
    policies: Iterable[dict[str, Any]],
    *,
    model: str,
    dimensions: int,
) -> Iterable[tuple[dict[str, Any] | None, bool]]:
    for policy in policies:
        policy_id = str(policy.get("id") or "").strip()
        embedding_text = build_policy_embedding_text(policy)
        if not policy_id or not embedding_text:
            yield None, True
            continue
        custom_id = f"{policy_id}:{text_hash(embedding_text)[:16]}"
        body = {"model": model, "input": embedding_text, "dimensions": int(dimensions)}
        yield {"custom_id": custom_id, "method": "POST", "url": "/v1/embeddings", "body": body}, False


def build_embedding_batch_jsonl_files(
    policies: Iterable[dict[str, Any]],
    output_path: Path,
    *,
    model: str = DEFAULT_EMBEDDING_MODEL,
    dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
    max_requests_per_file: int = 50_000,
) -> BatchBuildFilesResult:
    if max_requests_per_file < 1:
        raise ValueError("max_requests_per_file must be at least 1.")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_paths: list[Path] = []
    files: list[BatchFileResult] = []
    handle = None
    current_requests = 0
    total_requests = 0
    skipped = 0

    def next_path(index: int) -> Path:
        return output.with_name(f"{output.stem}_{index:03d}{output.suffix or '.jsonl'}")

    try:
        for request, is_skipped in _embedding_batch_requests(policies, model=model, dimensions=dimensions):
            if is_skipped:
                skipped += 1
                continue
            if handle is None or current_requests >= max_requests_per_file:
                if handle is not None:
                    handle.close()
                    files.append(BatchFileResult(path=str(temp_paths[-1]), requests=current_requests))
                temp_paths.append(next_path(len(temp_paths) + 1))
                handle = temp_paths[-1].open("w", encoding="utf-8")
                current_requests = 0
            handle.write(json.dumps(request, ensure_ascii=False) + "\n")
            current_requests += 1
            total_requests += 1
    finally:
        if handle is not None:
            handle.close()

    if current_requests:
        files.append(BatchFileResult(path=str(temp_paths[-1]), requests=current_requests))

    if len(files) == 1:
        temp_path = Path(files[0].path)
        temp_path.replace(output)
        files = [BatchFileResult(path=str(output), requests=files[0].requests)]

    return BatchBuildFilesResult(files=files, requests=total_requests, skipped=skipped)


def parse_embedding_batch_result(path: Path) -> dict[str, list[float]]:
    embeddings: dict[str, list[float]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        custom_id = str(item.get("custom_id") or "")
        policy_id = custom_id.split(":", 1)[0]
        response = item.get("response") or {}
        body = response.get("body") or {}
        data = body.get("data") or []
        if not policy_id or not data:
            continue
        embedding = data[0].get("embedding") if isinstance(data[0], dict) else None
        if isinstance(embedding, list):
            embeddings[policy_id] = [float(value) for value in embedding]
    return embeddings


class OpenAIEmbeddingClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = DEFAULT_EMBEDDING_MODEL,
        dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
    ) -> None:
        try:
            from openai import OpenAI
        except Exception as exc:  # pragma: no cover - depends on optional package
            raise RuntimeError("openai 패키지가 필요합니다. requirements.txt를 설치하세요.") from exc
        self.model = model
        self.dimensions = int(dimensions)
        self.client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))

    def embed(self, text: str) -> list[float]:
        response = self.client.embeddings.create(
            model=self.model,
            input=text,
            dimensions=self.dimensions,
        )
        return [float(value) for value in response.data[0].embedding]


class QdrantPolicyStore:
    def __init__(
        self,
        *,
        url: str = "http://127.0.0.1:6333",
        collection: str = DEFAULT_COLLECTION,
        vector_size: int = DEFAULT_EMBEDDING_DIMENSIONS,
    ) -> None:
        try:
            from qdrant_client import QdrantClient
        except Exception as exc:  # pragma: no cover - depends on optional package
            raise RuntimeError("qdrant-client 패키지가 필요합니다. requirements.txt를 설치하세요.") from exc
        self.collection = collection
        self.vector_size = int(vector_size)
        self.client = QdrantClient(url=url)

    def ensure_collection(self) -> None:
        from qdrant_client.models import Distance, VectorParams

        if self.client.collection_exists(self.collection):
            return
        self.client.create_collection(
            collection_name=self.collection,
            vectors_config=VectorParams(size=self.vector_size, distance=Distance.COSINE),
        )
        for field, schema in (
            ("year", "integer"),
            ("region", "keyword"),
            ("municipality", "keyword"),
            ("category", "keyword"),
            ("keywords", "keyword"),
        ):
            try:
                self.client.create_payload_index(self.collection, field_name=field, field_schema=schema)
            except Exception:
                pass

    def upsert_policies(self, policies: Iterable[dict[str, Any]], embeddings: dict[str, list[float]]) -> int:
        from qdrant_client.models import PointStruct

        points: list[Any] = []
        for policy in policies:
            policy_id = str(policy.get("id") or "")
            vector = embeddings.get(policy_id)
            if not policy_id or not vector:
                continue
            embedding_text = build_policy_embedding_text(policy)
            points.append(
                PointStruct(
                    id=qdrant_point_id(policy_id),
                    vector=vector,
                    payload=policy_payload(policy, embedding_text=embedding_text),
                )
            )
        if not points:
            return 0
        self.ensure_collection()
        self.client.upsert(collection_name=self.collection, points=points)
        return len(points)

    def search(self, vector: list[float], *, top_k: int, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        query_filter = _qdrant_filter(filters or {})
        try:
            result = self.client.search(
                collection_name=self.collection,
                query_vector=vector,
                query_filter=query_filter,
                limit=int(top_k),
                with_payload=True,
            )
        except AttributeError:  # pragma: no cover - qdrant-client version compatibility
            result = self.client.query_points(
                collection_name=self.collection,
                query=vector,
                query_filter=query_filter,
                limit=int(top_k),
                with_payload=True,
            ).points
        return [
            {
                "score": float(getattr(point, "score", 0.0) or 0.0),
                "payload": dict(getattr(point, "payload", {}) or {}),
            }
            for point in result
        ]


def _qdrant_filter(filters: dict[str, Any]):
    if not filters:
        return None
    from qdrant_client.models import FieldCondition, Filter, MatchValue, Range

    must: list[Any] = []
    for field in ("region", "municipality", "category"):
        value = str(filters.get(field) or "").strip()
        if value:
            must.append(FieldCondition(key=field, match=MatchValue(value=value)))
    year_from = filters.get("year_from")
    year_to = filters.get("year_to")
    if year_from or year_to:
        must.append(
            FieldCondition(
                key="year",
                range=Range(
                    gte=int(year_from) if year_from else None,
                    lte=int(year_to) if year_to else None,
                ),
            )
        )
    return Filter(must=must) if must else None


def rerank_results(query: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    query_terms = {piece for piece in str(query).split() if len(piece) >= 2}
    reranked: list[dict[str, Any]] = []
    for item in results:
        payload = dict(item.get("payload") or {})
        text = " ".join(
            [
                str(payload.get("title") or ""),
                str(payload.get("description") or ""),
                " ".join(str(k) for k in payload.get("keywords") or []),
            ]
        )
        overlap = sum(1 for term in query_terms if term in text)
        score = float(item.get("score") or 0.0) + overlap * 0.02
        reranked.append({"score": score, "payload": payload})
    return sorted(reranked, key=lambda item: item["score"], reverse=True)


def format_search_result(item: dict[str, Any]) -> dict[str, Any]:
    payload = dict(item.get("payload") or {})
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    return {
        "score": round(float(item.get("score") or 0.0), 6),
        "jev_relevant": item.get("jev_relevant"),
        "policy_id": payload.get("policy_id"),
        "title": payload.get("title"),
        "year": payload.get("year"),
        "region": payload.get("region"),
        "municipality": payload.get("municipality"),
        "category": payload.get("category"),
        "budget_amount": payload.get("budget_amount"),
        "description": payload.get("description"),
        "source_file": payload.get("source_file"),
        "raw": raw,
    }


class SimilarPolicySearch:
    def __init__(self, *, embedding_client: Any, store: Any, reranker: Any = None) -> None:
        self.embedding_client = embedding_client
        self.store = store
        self.reranker = reranker

    def search(self, query: str, *, top_k: int = DEFAULT_TOP_K, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        query_text = str(query or "").strip()
        if not query_text:
            raise ValueError("검색어를 입력하세요.")
        vector = self.embedding_client.embed(query_text)
        candidate_limit = 24 if self.reranker is not None else max(int(top_k) * 3, int(top_k))
        candidates = self.store.search(vector, top_k=candidate_limit, filters=filters)
        ranked = rerank_results(query_text, candidates)
        if self.reranker is not None:
            ranked = self.reranker.rerank(query_text, ranked)
        return [format_search_result(item) for item in ranked[: int(top_k)]]
