from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Mapping


LOCAL_SOURCE = "local"
CENTRAL_SOURCE = "central"
SUPPORTED_SOURCES: tuple[str, str] = (LOCAL_SOURCE, CENTRAL_SOURCE)
CENTRAL_YEAR_MIN = 2018
CENTRAL_YEAR_MAX = 2026

CENTRAL_LIST_URL = "https://www.openfiscaldata.go.kr/op/ko/bs/UOPKOBSA02"


def normalize_source(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if value in SUPPORTED_SOURCES:
        return value
    return LOCAL_SOURCE


def normalize_central_year(raw: str) -> str:
    digits = re.sub(r"\D+", "", str(raw or "").strip())
    if len(digits) >= 4:
        return digits[:4]
    return str(datetime.now().year)


def validate_central_year(raw: str) -> str:
    year = normalize_central_year(raw)
    try:
        value = int(year)
    except ValueError as exc:
        raise ValueError("중앙정부 회계년은 2018~2026 범위만 가능합니다.") from exc

    if not (CENTRAL_YEAR_MIN <= value <= CENTRAL_YEAR_MAX):
        raise ValueError("중앙정부 회계년은 2018~2026 범위만 가능합니다.")
    return str(value)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _format_budget(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    numeric = text.replace(",", "")
    if re.fullmatch(r"-?\d+", numeric):
        return format(int(numeric), ",")
    return text


def map_central_row(raw: Mapping[str, Any], row_no: int) -> dict[str, str]:
    return {
        "번호": str(row_no),
        "회계연도": _text(raw.get("acntYr")),
        "소관명": _text(raw.get("offcNm")),
        "회계명": _text(raw.get("acntNm")),
        "분야명": _text(raw.get("fldNm")),
        "부문명": _text(raw.get("sectNm")),
        "프로그램명": _text(raw.get("pgmNm")),
        "단위사업명": _text(raw.get("actvNm")),
        "세부사업명": _text(raw.get("sayNm")),
        "예산액": _format_budget(raw.get("thyCfmtnMediAmt")),
    }


def map_central_rows(rows: list[Mapping[str, Any]], limit: int) -> list[dict[str, str]]:
    safe_limit = max(1, int(limit))
    out: list[dict[str, str]] = []
    for row_no, raw in enumerate(rows[:safe_limit], start=1):
        out.append(map_central_row(raw, row_no=row_no))
    return out
