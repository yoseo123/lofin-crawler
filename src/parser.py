from __future__ import annotations

import re
from html import unescape
from typing import Dict


_TAG_RE = re.compile(r"<[^>]+>")
_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<(th|td)\b[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
_ITEM_RE = re.compile(
    r'<div\b[^>]*class="[^"]*item([12])[^"]*"[^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_LI_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)
_LABEL_NORMALIZE_RE = re.compile(r"[\s:\-\(\)\[\]\/]+")
_NON_ALNUM_RE = re.compile(r"[^0-9a-z가-힣]+", re.IGNORECASE)
_LIST3_PATTERNS = (
    re.compile(r"(?:var|let|const)\s+list3\s*=\s*\{(.*?)\}\s*;?", re.IGNORECASE | re.DOTALL),
    re.compile(r"window\.list3\s*=\s*\{(.*?)\}\s*;?", re.IGNORECASE | re.DOTALL),
    re.compile(r"list3\s*=\s*\{(.*?)\}\s*;?", re.IGNORECASE | re.DOTALL),
)


def _normalize_key(value: str) -> str:
    text = _LABEL_NORMALIZE_RE.sub("", str(value or "").strip().lower())
    return _NON_ALNUM_RE.sub("", text)

TARGET_FIELDS = [
    "회계연도",
    "조직",
    "사업목적",
    "사업기간",
    "총사업비",
    "사업내용",
    "지원형태",
    "지원조건",
    "사업위치",
    "시행주체",
    "추진근거",
    "추진경위",
    "추진계획",
]
_TARGET_BY_NORMALIZED = {_normalize_key(field): field for field in TARGET_FIELDS}
_TARGET_ALIASES = {
    _normalize_key("회계연도(당해)"): "회계연도",
    _normalize_key("조직명"): "조직",
    _normalize_key("사업내용(요약)"): "사업내용",
    _normalize_key("추진계획안"): "추진계획",
}
_TARGET_FALLBACK_HINTS = {
    "사업위치": tuple(_normalize_key(v) for v in ("사업위치", "소재지", "사업소재지")),
    "시행주체": tuple(_normalize_key(v) for v in ("시행주체", "주관부서", "주관기관", "시행부서", "수행주체")),
    "추진근거": tuple(_normalize_key(v) for v in ("추진근거", "법적근거", "근거및필요성")),
    "추진경위": tuple(_normalize_key(v) for v in ("추진경위", "추진배경", "추진현황", "추진이유")),
    "추진계획": tuple(_normalize_key(v) for v in ("추진계획", "향후계획", "실행계획", "세부계획", "추진일정")),
}
_SCRIPT_KEY_MAP = {
    "fyr": "회계연도",
    "deptNm": "조직",
    "bizPurpCn": "사업목적",
    "tbc": "총사업비",
    "bizCn": "사업내용",
    "spoFomNm": "지원형태",
    "spoCndCn": "지원조건",
    "bizLocCn": "사업위치",
    "impSubjNm": "시행주체",
    "bizBssCn": "추진근거",
    "bizPrgsCn": "추진경위",
    "bizPlanCn": "추진계획",
    # Observed variants in 일부 응답 payload.
    "bizLoc": "사업위치",
    "impSubj": "시행주체",
    "bizBasisCn": "추진근거",
    "bizProgCn": "추진경위",
    "bizPlan": "추진계획",
}
_SCRIPT_FALLBACK_HINTS = {
    "사업위치": tuple(_normalize_key(v) for v in ("사업위치", "bizLoc")),
    "시행주체": tuple(_normalize_key(v) for v in ("시행주체", "impSubj")),
    "추진근거": tuple(_normalize_key(v) for v in ("추진근거", "bizBss", "bizBasis")),
    "추진경위": tuple(_normalize_key(v) for v in ("추진경위", "bizPrgs", "bizProg", "bizHist")),
    "추진계획": tuple(_normalize_key(v) for v in ("추진계획", "bizPlan")),
}


def _clean_html_text(value: str) -> str:
    text = _TAG_RE.sub(" ", value)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_policy_detail(html: str) -> Dict[str, str]:
    """Parse key/value pairs from table rows and list/div layouts."""
    rows: Dict[str, str] = {}

    # 1) table layout: [th, td, th, td, ...]
    for tr in _TR_RE.findall(html):
        cells = _CELL_RE.findall(tr)
        if len(cells) < 2:
            continue

        for i in range(0, len(cells) - 1, 2):
            key = _clean_html_text(cells[i][1])
            value = _clean_html_text(cells[i + 1][1])
            if key:
                rows[key] = value

    # 2) div layout: item1(label) -> item2(value)
    current_key = ""
    for item_type, body in _ITEM_RE.findall(html):
        text = _clean_html_text(body)
        if not text:
            continue
        if item_type == "1":
            current_key = text
            continue
        if current_key:
            rows[current_key] = text
            current_key = ""

    # 3) list layout: <li>항목 : 값</li>
    for li_html in _LI_RE.findall(html):
        text = _clean_html_text(li_html)
        if not text:
            continue
        parts = re.split(r"\s*[:：]\s*", text, maxsplit=1)
        if len(parts) != 2:
            continue
        key, value = parts[0].strip(), parts[1].strip()
        if key:
            rows[key] = value

    return rows


def _decode_js_value(value: str) -> str:
    value = value.strip()
    if value == "null":
        return ""
    if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
        text = value[1:-1]
        text = text.replace("\\n", "\n").replace("\\t", "\t")
        text = text.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")
        return unescape(text).strip()
    return value.strip()


def _extract_js_field(block: str, key: str) -> str:
    pattern = re.compile(
        rf"(?:['\"])?{re.escape(key)}(?:['\"])?\s*:\s*('(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\"|null|[0-9]+)",
        re.DOTALL,
    )
    m = pattern.search(block)
    if not m:
        return ""
    return _decode_js_value(m.group(1))


def _format_ymd(value: str) -> str:
    if re.fullmatch(r"\d{8}", value):
        return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"
    return value


def _extract_list3_block(html: str) -> str:
    for pattern in _LIST3_PATTERNS:
        match = pattern.search(html)
        if match:
            return match.group(1)
    return ""


def parse_policy_detail_from_script(html: str) -> Dict[str, str]:
    """Extract target fields from JS object (list3 = {...}) when DOM labels are sparse."""
    out: Dict[str, str] = {}
    block = _extract_list3_block(html)
    if not block:
        return out

    for js_key, target in _SCRIPT_KEY_MAP.items():
        value = _extract_js_field(block, js_key)
        if value:
            out[target] = value

    start = _extract_js_field(block, "bizBgngYmd")
    end = _extract_js_field(block, "bizEndYmd")
    if start or end:
        out["사업기간"] = f"{_format_ymd(start)} ~ {_format_ymd(end)}".strip(" ~")

    return out


def parse_policy_detail_from_script_object(obj: dict) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not isinstance(obj, dict):
        return out

    normalized_obj: Dict[str, object] = {}
    for key, value in obj.items():
        normalized = _normalize_key(str(key))
        if normalized and normalized not in normalized_obj:
            normalized_obj[normalized] = value

    for js_key, target in _SCRIPT_KEY_MAP.items():
        value = obj.get(js_key)
        if value is None:
            value = normalized_obj.get(_normalize_key(js_key))
        if value is None:
            continue
        text = str(value).strip()
        if text:
            out[target] = text

    for target, hints in _SCRIPT_FALLBACK_HINTS.items():
        if out.get(target):
            continue
        for normalized_key, value in normalized_obj.items():
            if not any(hint and hint in normalized_key for hint in hints):
                continue
            text = str(value).strip()
            if text:
                out[target] = text
                break

    start_value = obj.get("bizBgngYmd")
    if start_value is None:
        start_value = normalized_obj.get(_normalize_key("bizBgngYmd"), "")
    end_value = obj.get("bizEndYmd")
    if end_value is None:
        end_value = normalized_obj.get(_normalize_key("bizEndYmd"), "")

    start = str(start_value or "").strip()
    end = str(end_value or "").strip()
    if start or end:
        out["사업기간"] = f"{_format_ymd(start)} ~ {_format_ymd(end)}".strip(" ~")

    return out


def select_target_fields(parsed: Dict[str, str]) -> Dict[str, str]:
    selected: Dict[str, str] = {field: "" for field in TARGET_FIELDS}
    for key, value in parsed.items():
        normalized = _normalize_key(key)
        canonical = _TARGET_BY_NORMALIZED.get(normalized) or _TARGET_ALIASES.get(normalized)
        if canonical is None:
            for target, hints in _TARGET_FALLBACK_HINTS.items():
                if any(hint and hint in normalized for hint in hints):
                    canonical = target
                    break
        if canonical is not None:
            selected[canonical] = value
    return selected
