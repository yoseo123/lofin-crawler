from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path

from src.exporter import export_to_excel, export_to_markdown
from src.policy_index import export_policy_index


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
    ) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be at least 1")
        self.output_path = Path(output_path)
        self.output_format = output_format
        self.chunk_size = int(chunk_size)
        self.source = source
        self.source_url = source_url
        self.default_year = default_year
        self.default_region = default_region
        self.title = title
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

        self._buffer: list[dict[str, str]] = []
        self._total_count = 0
        self._chunk_no = 0
        self._indexed = 0
        self._index_skipped = 0
        self._chunks: list[dict[str, object]] = []
        self._started_at = datetime.now().isoformat(timespec="seconds")

    @property
    def count(self) -> int:
        return self._total_count

    @property
    def chunk_count(self) -> int:
        return self._chunk_no

    def add_row(self, row: dict[str, str]) -> None:
        self._total_count += 1
        normalized = dict(row)
        normalized["번호"] = str(self._total_count)
        self._buffer.append(normalized)
        if len(self._buffer) >= self.chunk_size:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return

        self._chunk_no += 1
        rows = self._buffer
        self._buffer = []

        part_stem = f"{self.output_path.stem}_part{self._chunk_no:03d}"
        md_path = self.output_path.with_name(f"{part_stem}.md") if self.output_format in {"md", "both"} else None
        xlsx_path = self.output_path.with_name(f"{part_stem}.xlsx") if self.output_format in {"xlsx", "both"} else None

        if md_path is not None:
            export_to_markdown(rows, md_path, title=self.title)
        if xlsx_path is not None:
            export_to_excel(rows, xlsx_path)

        primary_path = md_path or xlsx_path
        if primary_path is None:
            raise ValueError("출력 형식이 올바르지 않습니다.")

        json_path = primary_path.with_suffix(".json")
        index_result = export_policy_index(
            rows,
            json_path,
            source=self.source,
            source_url=self.source_url,
            default_year=self.default_year,
            default_region=self.default_region,
            source_file=primary_path.name,
            xlsx_file=xlsx_path.name if xlsx_path is not None else "",
        )
        self._indexed += index_result.indexed
        self._index_skipped += index_result.skipped

        self._chunks.append(
            {
                "part": self._chunk_no,
                "count": len(rows),
                "first_row": self._total_count - len(rows) + 1,
                "last_row": self._total_count,
                "md_file": md_path.name if md_path is not None else "",
                "xlsx_file": xlsx_path.name if xlsx_path is not None else "",
                "json_file": json_path.name,
                "indexed": index_result.indexed,
                "index_skipped": index_result.skipped,
            }
        )

    def close(self, *, complete: bool = True) -> ChunkedWriteResult:
        self.flush()
        manifest_path = self.output_path.with_name(f"{self.output_path.stem}_manifest.json")
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "started_at": self._started_at,
            "source": "central" if self.source == "central" else "local",
            "year": self.default_year,
            "region": self.default_region,
            "chunk_size": self.chunk_size,
            "total_count": self._total_count,
            "complete": bool(complete),
            "chunks": self._chunks,
        }
        manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        files: list[str] = []
        json_files: list[str] = []
        for chunk in self._chunks:
            for key in ("md_file", "xlsx_file"):
                value = str(chunk.get(key, ""))
                if value:
                    files.append(value)
            json_file = str(chunk.get("json_file", ""))
            if json_file:
                json_files.append(json_file)

        return ChunkedWriteResult(
            count=self._total_count,
            chunks=self._chunk_no,
            manifest_file=manifest_path.name,
            files=files,
            json_files=json_files,
            indexed=self._indexed,
            index_skipped=self._index_skipped,
        )
