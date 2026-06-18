from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from pathlib import Path
from typing import Any


STOPWORDS = {
    "사업",
    "지원",
    "추진",
    "개선",
    "관리",
    "운영",
    "강화",
    "기반",
    "구축",
    "지역",
    "정책",
}


@dataclass(frozen=True)
class PolicyIndexResult:
    indexed: int
    skipped: int


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _parse_int(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    cleaned = re.sub(r"[^0-9\-]", "", text)
    if not cleaned or cleaned == "-":
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _parse_decimal(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    cleaned = re.sub(r"[^0-9\.\-]", "", text)
    if not cleaned or cleaned in {"-", ".", "-."}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_year(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"(19|20)\d{2}", text)
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def extract_keywords(name: str) -> list[str]:
    normalized = re.sub(r"[^0-9A-Za-z가-힣\s]", " ", str(name or ""))
    pieces = [
        piece.strip()
        for piece in re.split(r"\s+", normalized)
        if piece.strip() and len(piece.strip()) >= 2 and piece.strip() not in STOPWORDS
    ]

    unique: list[str] = []
    for piece in pieces:
        if piece not in unique:
            unique.append(piece)
    return unique[:10]


def _build_description(row: dict[str, str]) -> str:
    parts = [
        _text(row.get("사업목적")),
        _text(row.get("사업내용")),
        _text(row.get("추진근거")),
    ]
    parts = [part for part in parts if part]
    if not parts:
        return ""
    return " / ".join(parts[:2])


def _policy_title(row: dict[str, str]) -> str:
    return _text(row.get("사업명")) or _text(row.get("세부사업명"))


def _policy_region(row: dict[str, str], default_region: str | None) -> str:
    return _text(row.get("지역")) or _text(default_region) or "전국"


def _policy_year(row: dict[str, str], default_year: str | int | None) -> int | None:
    return _parse_year(row.get("회계연도")) or _parse_year(default_year)


def _policy_id(source: str, title: str, region: str, municipality: str, year: int | None) -> str:
    raw = "|".join([source, title, region, municipality, str(year or "")])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def normalize_policy_row(
    row: dict[str, str],
    *,
    source: str,
    source_url: str = "",
    default_year: str | int | None = None,
    default_region: str | None = None,
) -> dict[str, object] | None:
    title = _policy_title(row)
    year = _policy_year(row, default_year)
    if not title or year is None:
        return None

    region = _policy_region(row, default_region)
    municipality = _text(row.get("자치단체"))
    source_name = "central" if source == "central" else "local"
    source_label = "중앙정부 열린재정" if source_name == "central" else "지방재정365"
    budget_amount = _parse_int(row.get("총사업비")) or _parse_int(row.get("예산액"))

    return {
        "id": _policy_id(source_name, title, region, municipality, year),
        "source": source_name,
        "source_label": source_label,
        "title": title,
        "region": region,
        "municipality": municipality,
        "year": year,
        "category": _text(row.get("조직")) or _text(row.get("소관명")) or None,
        "budget_amount": budget_amount,
        "execution_amount": _parse_int(row.get("집행액")),
        "execution_rate": _parse_decimal(row.get("집행률")),
        "description": _build_description(row) or _text(row.get("사업내용(가능 시)")) or None,
        "keywords": extract_keywords(title),
        "source_url": source_url or None,
        "raw": {str(key): _text(value) for key, value in row.items()},
    }


def build_policy_index(
    rows: list[dict[str, str]],
    *,
    source: str,
    source_url: str = "",
    default_year: str | int | None = None,
    default_region: str | None = None,
) -> tuple[list[dict[str, object]], int]:
    policies: list[dict[str, object]] = []
    seen: set[str] = set()
    skipped = 0

    for row in rows:
        policy = normalize_policy_row(
            row,
            source=source,
            source_url=source_url,
            default_year=default_year,
            default_region=default_region,
        )
        if policy is None:
            skipped += 1
            continue
        key = str(policy["id"])
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        policies.append(policy)

    return policies, skipped


def export_policy_index(
    rows: list[dict[str, str]],
    output_path: Path,
    *,
    source: str,
    source_url: str = "",
    default_year: str | int | None = None,
    default_region: str | None = None,
    source_file: str = "",
    xlsx_file: str = "",
) -> PolicyIndexResult:
    policies, skipped = build_policy_index(
        rows,
        source=source,
        source_url=source_url,
        default_year=default_year,
        default_region=default_region,
    )
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "central" if source == "central" else "local",
        "source_file": source_file or xlsx_file,
        "xlsx_file": xlsx_file,
        "count": len(policies),
        "skipped": skipped,
        "policies": policies,
    }

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return PolicyIndexResult(indexed=len(policies), skipped=skipped)
