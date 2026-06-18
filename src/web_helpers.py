from __future__ import annotations

from pathlib import Path

from src.central_source import CENTRAL_SOURCE, normalize_source, validate_central_year

MAX_CRAWL_LIMIT = 500000
DEFAULT_CHUNK_SIZE = 10000
OUTPUT_FORMATS = {"md", "xlsx", "both"}
ALL_REGION_VALUES = {"전국", "전체", "all", "nationwide"}


def _normalize_year(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if len(value) == 4 and value.isdigit():
        return f"{value}-01-01"
    return value


def _normalize_limit(raw: str) -> int:
    try:
        value = int((raw or "").strip())
    except ValueError:
        return 30
    if value < 1:
        return 1
    if value > MAX_CRAWL_LIMIT:
        return MAX_CRAWL_LIMIT
    return value


def _normalize_chunk_size(raw: str) -> int:
    try:
        value = int((raw or "").strip())
    except ValueError:
        return DEFAULT_CHUNK_SIZE
    if value < 0:
        return 0
    if value > MAX_CRAWL_LIMIT:
        return MAX_CRAWL_LIMIT
    return value


def _normalize_output_format(raw: str) -> str:
    value = (raw or "").strip().lower()
    if value in OUTPUT_FORMATS:
        return value
    return "md"


def normalize_region_filter(raw: str) -> str:
    value = (raw or "").strip()
    if value.lower() in ALL_REGION_VALUES:
        return "전국"
    return value


def _normalize_output_name(raw: str, output_format: str = "md") -> str:
    value = (raw or "").strip()
    if not value:
        return "web_out.md" if output_format != "xlsx" else "web_out.xlsx"

    # keep only filename part for both Unix/Windows style separators
    filename = Path(value.replace("\\", "/")).name.strip()
    suffix = ".xlsx" if output_format == "xlsx" else ".md"
    if not filename.lower().endswith((".xlsx", ".md")):
        filename = f"{filename}{suffix}"
    elif output_format == "xlsx" and not filename.lower().endswith(".xlsx"):
        filename = str(Path(filename).with_suffix(".xlsx"))
    elif output_format in {"md", "both"} and not filename.lower().endswith(".md"):
        filename = str(Path(filename).with_suffix(".md"))
    return filename


def json_output_name_for(output_name: str, output_format: str = "md") -> str:
    filename = _normalize_output_name(output_name, output_format)
    return str(Path(filename).with_suffix(".json"))


def is_allowed_download_filename(raw: str) -> bool:
    name = (raw or "").strip()
    if not name:
        return False
    if Path(name).name != name:
        return False
    if "/" in name or "\\" in name:
        return False
    if name.startswith("."):
        return False
    if not (name.lower().endswith(".xlsx") or name.lower().endswith(".md") or name.lower().endswith(".json")):
        return False
    return True


def user_facing_error_message(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        msg = str(exc).strip()
        if msg:
            return msg
    if isinstance(exc, RuntimeError):
        msg = str(exc).strip()
        if msg.startswith("수집 결과가 없습니다"):
            return msg
    return "실행 중 오류가 발생했습니다. 입력값을 확인하고 다시 시도하세요."


def build_run_params(form: dict[str, str]) -> dict[str, object]:
    keyword = (form.get("keyword") or "").strip()

    source = normalize_source(form.get("source") or "")
    year = _normalize_year(form.get("year") or "")
    region = normalize_region_filter(form.get("region") or "")
    fiscal_year = (form.get("fiscal_year") or "").strip()

    if source == CENTRAL_SOURCE:
        year = validate_central_year(fiscal_year or year)
        region = ""
    output_format = _normalize_output_format(form.get("output_format") or "")
    output = _normalize_output_name(form.get("output") or "", output_format)

    return {
        "keyword": keyword,
        "source": source,
        "year": year,
        "region": region,
        "limit": _normalize_limit(form.get("limit") or ""),
        "chunk_size": _normalize_chunk_size(form.get("chunk_size") or ""),
        "output_format": output_format,
        "output": output,
        "json_output": json_output_name_for(output, output_format),
        "headed": (form.get("headed") or "") == "on",
    }
