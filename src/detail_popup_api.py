from __future__ import annotations

import re
from typing import Mapping


def _normalize_inq_ymd(raw: str) -> str:
    digits = re.sub(r"\D+", "", str(raw or "").strip())
    if len(digits) >= 8:
        return digits[:8]
    if len(digits) == 6:
        return f"{digits}01"
    if len(digits) == 4:
        return f"{digits}0101"
    return ""


def build_popup_detail_form(entry: Mapping[str, str]) -> dict[str, str]:
    payload = {
        "dbizCd": str(entry.get("dbizCd", "")).strip(),
        "lafCd": str(entry.get("lafCd", "")).strip(),
        "fyr": str(entry.get("fyr", "")).strip(),
        "dbizNm": str(entry.get("name", "")).strip(),
        "inqYmd": _normalize_inq_ymd(str(entry.get("inqYmd", ""))),
    }
    return {key: value for key, value in payload.items() if value}
