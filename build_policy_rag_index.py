#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.policy_rag import (
    DEFAULT_COLLECTION,
    DEFAULT_EMBEDDING_DIMENSIONS,
    DEFAULT_EMBEDDING_MODEL,
    OpenAIEmbeddingClient,
    QdrantPolicyStore,
    SimilarPolicySearch,
    build_embedding_batch_jsonl_files,
    load_policies_from_files,
    iter_policy_index_files,
    parse_embedding_batch_result,
)


def _patterns(args) -> list[str]:
    return list(args.input_glob or ["*_part*.json"])


def _load_policies(patterns: list[str]) -> tuple[list[dict[str, object]], int, list[Path]]:
    files = iter_policy_index_files(patterns, base_dir=Path("."))
    policies, skipped = load_policies_from_files(files)
    return policies, skipped, files


def prepare_batch(args) -> int:
    policies, skipped, files = _load_policies(_patterns(args))
    result = build_embedding_batch_jsonl_files(
        policies,
        Path(args.output_jsonl),
        model=args.model,
        dimensions=args.dimensions,
        max_requests_per_file=args.max_requests_per_file,
    )
    manifest = {
        "input_files": [str(path) for path in files],
        "batch_files": [{"path": item.path, "requests": item.requests} for item in result.files],
        "policies": len(policies),
        "dedup_skipped": skipped,
        "requests": result.requests,
        "request_skipped": result.skipped,
        "model": args.model,
        "dimensions": args.dimensions,
        "max_requests_per_file": args.max_requests_per_file,
    }
    Path(args.manifest_json).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if result.files:
        print(f"Batch JSONL: {result.requests} requests -> {len(result.files)} file(s)")
        for item in result.files:
            print(f"- {item.path}: {item.requests} requests")
    else:
        print("Batch JSONL: 0 requests")
    print(f"Manifest: {args.manifest_json}")
    return 0


def submit_batch(args) -> int:
    try:
        from openai import OpenAI
    except Exception as exc:
        raise RuntimeError("openai 패키지가 필요합니다. requirements.txt를 설치하세요.") from exc

    client = OpenAI()
    with Path(args.input_jsonl).open("rb") as handle:
        uploaded = client.files.create(file=handle, purpose="batch")
    batch = client.batches.create(
        input_file_id=uploaded.id,
        endpoint="/v1/embeddings",
        completion_window=args.completion_window,
    )
    payload = {
        "file_id": uploaded.id,
        "batch_id": batch.id,
        "status": batch.status,
        "endpoint": "/v1/embeddings",
    }
    Path(args.output_json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def check_batch(args) -> int:
    try:
        from openai import OpenAI
    except Exception as exc:
        raise RuntimeError("openai 패키지가 필요합니다. requirements.txt를 설치하세요.") from exc

    batch = OpenAI().batches.retrieve(args.batch_id)
    payload = {
        "id": batch.id,
        "status": batch.status,
        "request_counts": getattr(batch, "request_counts", None).model_dump()
        if getattr(batch, "request_counts", None) is not None
        else None,
        "output_file_id": getattr(batch, "output_file_id", None),
        "error_file_id": getattr(batch, "error_file_id", None),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def download_batch_output(args) -> int:
    try:
        from openai import OpenAI
    except Exception as exc:
        raise RuntimeError("openai 패키지가 필요합니다. requirements.txt를 설치하세요.") from exc

    content = OpenAI().files.content(args.file_id)
    data = content.read()
    Path(args.output_jsonl).write_bytes(data)
    print(f"Downloaded batch output -> {args.output_jsonl}")
    return 0


def upsert_batch(args) -> int:
    policies, skipped, files = _load_policies(_patterns(args))
    embeddings = parse_embedding_batch_result(Path(args.batch_output_jsonl))
    store = QdrantPolicyStore(
        url=args.qdrant_url,
        collection=args.collection,
        vector_size=args.dimensions,
    )
    count = store.upsert_policies(policies, embeddings)
    print(f"Upserted: {count} policies into {args.collection}")
    print(f"Loaded policies: {len(policies)} / skipped duplicates: {skipped} / files: {len(files)}")
    return 0


def search(args) -> int:
    service = SimilarPolicySearch(
        embedding_client=OpenAIEmbeddingClient(model=args.model, dimensions=args.dimensions),
        store=QdrantPolicyStore(
            url=args.qdrant_url,
            collection=args.collection,
            vector_size=args.dimensions,
        ),
    )
    filters = {
        "year_from": args.year_from,
        "year_to": args.year_to,
        "region": args.region,
        "municipality": args.municipality,
        "category": args.category,
    }
    results = service.search(args.query, top_k=args.top_k, filters=filters)
    print(json.dumps({"count": len(results), "results": results}, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Build and query a RAG vector index for Lofin policy cases.")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare-batch", help="Create OpenAI Batch API JSONL requests for embeddings.")
    prepare.add_argument("--input-glob", action="append", default=None, help="Policy index JSON glob.")
    prepare.add_argument("--output-jsonl", default="policy_embedding_batch.jsonl")
    prepare.add_argument("--manifest-json", default="policy_embedding_batch_manifest.json")
    prepare.add_argument("--model", default=DEFAULT_EMBEDDING_MODEL)
    prepare.add_argument("--dimensions", type=int, default=DEFAULT_EMBEDDING_DIMENSIONS)
    prepare.add_argument("--max-requests-per-file", type=int, default=50_000)
    prepare.set_defaults(func=prepare_batch)

    submit = sub.add_parser("submit-batch", help="Upload embedding JSONL and create an OpenAI Batch job.")
    submit.add_argument("--input-jsonl", default="policy_embedding_batch.jsonl")
    submit.add_argument("--output-json", default="policy_embedding_batch_openai.json")
    submit.add_argument("--completion-window", default="24h")
    submit.set_defaults(func=submit_batch)

    check = sub.add_parser("check-batch", help="Check an OpenAI Batch job status.")
    check.add_argument("batch_id")
    check.set_defaults(func=check_batch)

    download = sub.add_parser("download-batch-output", help="Download an OpenAI Batch output file.")
    download.add_argument("file_id")
    download.add_argument("--output-jsonl", default="policy_embedding_batch_output.jsonl")
    download.set_defaults(func=download_batch_output)

    upsert = sub.add_parser("upsert-batch", help="Read OpenAI Batch output JSONL and upsert embeddings into Qdrant.")
    upsert.add_argument("--input-glob", action="append", default=None, help="Policy index JSON glob.")
    upsert.add_argument("--batch-output-jsonl", required=True)
    upsert.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    upsert.add_argument("--collection", default=DEFAULT_COLLECTION)
    upsert.add_argument("--dimensions", type=int, default=DEFAULT_EMBEDDING_DIMENSIONS)
    upsert.set_defaults(func=upsert_batch)

    query = sub.add_parser("search", help="Search similar policies using OpenAI embeddings and Qdrant.")
    query.add_argument("query")
    query.add_argument("--top-k", type=int, default=10)
    query.add_argument("--year-from", type=int, default=None)
    query.add_argument("--year-to", type=int, default=None)
    query.add_argument("--region", default="")
    query.add_argument("--municipality", default="")
    query.add_argument("--category", default="")
    query.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    query.add_argument("--collection", default=DEFAULT_COLLECTION)
    query.add_argument("--model", default=DEFAULT_EMBEDDING_MODEL)
    query.add_argument("--dimensions", type=int, default=DEFAULT_EMBEDDING_DIMENSIONS)
    query.set_defaults(func=search)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
