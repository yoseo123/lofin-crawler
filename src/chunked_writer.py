from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path

from src.exporter import export_to_excel, export_to_markdown
from src import policy_index
from src.policy_index import export_policy_index
from src.resume_state import ResumeState, atomic_write, fingerprint


@dataclass
class ChunkedWriteResult:
    count: int
    chunks: int
    manifest_file: str
    files: list[str] = field(default_factory=list)
    json_files: list[str] = field(default_factory=list)
    indexed: int = 0
    index_skipped: int = 0


class ChunkedPolicyWriter:
    def __init__(
        self,
        *,
        output_path: Path,
        output_format: str,
        chunk_size: int,
        source: str,
        source_url: str = "",
        default_year: str = "",
        default_region: str = "",
        title: str = "Lofin 정책 수집 결과",
        search_conditions: dict | None = None,
        resume: bool = False,
    ) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be at least 1")
        if output_format not in {"md", "xlsx", "both"}:
            raise ValueError("출력 형식이 올바르지 않습니다.")
        self.output_path = Path(output_path)
        self.output_format = output_format
        self.chunk_size = int(chunk_size)
        self.source = source
        self.source_url = source_url
        self.default_year = default_year
        self.default_region = default_region
        self.title = title
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.output_path.with_name(
            f"{self.output_path.stem}_manifest.json"
        )
        self.state_path = self.output_path.with_name(
            f"{self.output_path.stem}_resume.sqlite3"
        )
        self.config = {
            "schema": 1,
            "source": source,
            "source_url": source_url,
            "indexer_sha256": hashlib.sha256(
                Path(policy_index.__file__).read_bytes()
            ).hexdigest(),
            "year": default_year,
            "region": default_region,
            "format": output_format,
            "chunk_size": self.chunk_size,
            "title": title,
            "search": search_conditions or {},
        }
        # Never overwrite an older run lacking a trustworthy query/row ledger.
        if not resume and (
            self.manifest_path.exists()
            or any(
                p.name.startswith(f"{self.output_path.stem}_part")
                for p in self.output_path.parent.iterdir()
            )
        ):
            raise ValueError(
                "기존 파트/manifest가 있습니다. 다른 출력 경로를 사용하세요."
            )
        self._state = ResumeState(self.state_path, self.config, resume=resume)
        self._closed = False
        try:
            self._total_count = self._state.count
            self._chunks = self._state.chunks()
            self._started_at = self._state.db.execute(
                "SELECT value FROM meta WHERE key='started_at'"
            ).fetchone()[0]
            last = 0
            for part, chunk in enumerate(self._chunks, 1):
                if (
                    chunk["part"] != part
                    or chunk["first_row"] != last + 1
                    or chunk["last_row"] > self.count
                    or chunk["last_row"] < chunk["first_row"]
                    or chunk["count"] != chunk["last_row"] - last
                ):
                    raise ValueError("파트 기록이 손상되었습니다. 복구를 중단합니다.")
                last = chunk["last_row"]
                # Paths always derived locally, never trusted from a damaged manifest.
                if not self._chunk_valid(chunk):
                    repaired = self._export_chunk(part, chunk["first_row"], last)
                    self._state.save_chunk(repaired)
                    self._chunks[part - 1] = repaired
            # Publish an incomplete snapshot even if the old manifest is missing/bad.
            self._write_manifest(complete=False)
            if resume:
                self.flush()
        except BaseException:
            self.release()
            raise

    @property
    def count(self) -> int:
        return self._total_count

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    def contains(self, key: str) -> bool:
        return self._state.contains(key)

    def bind_conditions(self, conditions: dict) -> None:
        self._state.bind_conditions(conditions)
        self._write_manifest(complete=False)

    @property
    def at_limit(self) -> bool:
        limit = self.config["search"].get("limit")
        return limit is not None and self.count >= int(limit)

    def add_row(self, row: dict[str, str], *, key: str | None = None) -> None:
        if self._closed:
            raise ValueError("writer is closed")
        if self.at_limit:
            return
        normalized = dict(row)
        # Exact snapshot fallback preserves different same-name central hierarchies.
        key = key or fingerprint(
            [self.source, {k: v for k, v in normalized.items() if k != "번호"}]
        )
        normalized["번호"] = str(self.count + 1)
        if not self._state.append(normalized, key, self.count + 1):
            return
        self._total_count += 1
        last = int(self._chunks[-1]["last_row"]) if self._chunks else 0
        if self.count - last >= self.chunk_size:
            self.flush()

    def _paths(self, part: int) -> dict[str, Path]:
        stem = f"{self.output_path.stem}_part{part:03d}"
        paths = {"json_file": self.output_path.with_name(f"{stem}.json")}
        if self.output_format in {"md", "both"}:
            paths["md_file"] = self.output_path.with_name(f"{stem}.md")
        if self.output_format in {"xlsx", "both"}:
            paths["xlsx_file"] = self.output_path.with_name(f"{stem}.xlsx")
        return paths

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for data in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(data)
        return digest.hexdigest()

    def _chunk_valid(self, chunk: dict) -> bool:
        for key, path in self._paths(chunk["part"]).items():
            if chunk.get(key) != path.name or not path.is_file():
                return False
            if self._hash(path) != chunk.get("sha256", {}).get(key):
                return False
        return True

    def _export_chunk(self, part: int, first: int, last: int) -> dict:
        rows = self._state.rows(first, last)
        paths = self._paths(part)
        md = paths.get("md_file")
        xlsx = paths.get("xlsx_file")
        if md:
            atomic_write(
                md, lambda temp: export_to_markdown(rows, temp, title=self.title)
            )
        if xlsx:
            atomic_write(xlsx, lambda temp: export_to_excel(rows, temp))
        result = None

        def write_index(temp):
            nonlocal result
            result = export_policy_index(
                rows,
                temp,
                source=self.source,
                source_url=self.source_url,
                default_year=self.default_year,
                default_region=self.default_region,
                source_file=(md or xlsx).name,
                xlsx_file=xlsx.name if xlsx else "",
            )

        atomic_write(paths["json_file"], write_index)
        return {
            "part": part,
            "count": len(rows),
            "first_row": first,
            "last_row": last,
            "md_file": md.name if md else "",
            "xlsx_file": xlsx.name if xlsx else "",
            "json_file": paths["json_file"].name,
            "indexed": result.indexed,
            "index_skipped": result.skipped,
            "sha256": {key: self._hash(path) for key, path in paths.items()},
        }

    def flush(self) -> None:
        last = int(self._chunks[-1]["last_row"]) if self._chunks else 0
        while last < self.count:
            end = min(last + self.chunk_size, self.count)
            chunk = self._export_chunk(self.chunk_count + 1, last + 1, end)
            # Multi-file publication is committed here, only after all renames.
            self._state.save_chunk(chunk)
            self._chunks.append(chunk)
            last = end
            self._write_manifest(complete=False)

    def _write_manifest(self, *, complete: bool) -> None:
        exported = sum(int(c["count"]) for c in self._chunks)
        payload = {
            "schema_version": 2,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "started_at": self._started_at,
            "source": self.source,
            "year": self.default_year,
            "region": self.default_region,
            "chunk_size": self.chunk_size,
            "total_count": exported,
            "durable_count": self.count,
            "complete": bool(complete),
            "resume_file": self.state_path.name,
            "conditions": self.config,
            "chunks": self._chunks,
        }
        atomic_write(
            self.manifest_path,
            lambda temp: temp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            ),
        )

    def release(self) -> None:
        """Release handles without claiming completion; also used after export errors."""
        self._state.close()
        self._closed = True

    def close(self, *, complete: bool = True) -> ChunkedWriteResult:
        try:
            self.flush()
            self._write_manifest(complete=complete)
            return ChunkedWriteResult(
                count=self.count,
                chunks=self.chunk_count,
                manifest_file=self.manifest_path.name,
                files=[
                    str(c[k])
                    for c in self._chunks
                    for k in ("md_file", "xlsx_file")
                    if c.get(k)
                ],
                json_files=[str(c["json_file"]) for c in self._chunks],
                indexed=sum(int(c["indexed"]) for c in self._chunks),
                index_skipped=sum(int(c["index_skipped"]) for c in self._chunks),
            )
        finally:
            self.release()
