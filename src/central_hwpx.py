from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import unquote_plus
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


_FILES_PATH_RE = re.compile(r"/files/([^/?#\"'<>]+)/([^/?#\"'<>]+)(?:\?([^\"'<>\s]*))?", re.IGNORECASE)
_DOWNLOAD_CALL_RE = re.compile(r"\$\.(?:download|downloadAjax)\s*\((.*?)\)\s*;", re.IGNORECASE | re.DOTALL)
_VAR_ASSIGN_RE = re.compile(
    r"(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*('(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\"|[0-9]+)\s*;",
    re.DOTALL,
)
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"([A-Za-z_:][\w:.-]*)\s*=\s*(['\"])(.*?)\2", re.DOTALL)
_SPLIT_EXPR_RE = re.compile(
    r"([A-Za-z_$][\w$]*)\s*(?:\|\|\s*['\"][^'\"]*['\"])?\s*\)?\s*\.split\(\s*(['\"])(.*?)\2\s*\)\s*\[\s*(\d+)\s*\]"
)
_WHITESPACE_RE = re.compile(r"\s+")
_MAX_HWPX_ARCHIVE_BYTES = 15 * 1024 * 1024
_MAX_HWPX_ENTRY_COUNT = 400
_MAX_HWPX_TOTAL_UNCOMPRESSED_BYTES = 40 * 1024 * 1024
_MAX_HWPX_SINGLE_ENTRY_BYTES = 10 * 1024 * 1024

_HEADING_RE = re.compile(r"사업\s*내용", re.IGNORECASE)
_STOP_HEADINGS = (
    "사업목적",
    "사업기간",
    "총사업비",
    "지원형태",
    "지원조건",
    "사업위치",
    "시행주체",
    "추진근거",
    "추진경위",
    "추진계획",
    "성과지표",
)


@dataclass(frozen=True)
class DownloadSpec:
    file_id: str
    file_seq: str
    file_name: str = ""


def _clean(value: object) -> str:
    return str(value or "").strip()


def _normalize_key(value: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", _clean(value).lower())


def _decode_js_string_literal(value: str) -> str:
    text = _clean(value)
    if len(text) >= 2 and text[0] in {"'", '"'} and text[-1] == text[0]:
        inner = text[1:-1]
        inner = inner.replace(r"\n", "\n").replace(r"\t", "\t")
        inner = inner.replace(r"\'", "'").replace(r'\"', '"').replace(r"\\", "\\")
        return inner.strip()
    return text


def _extract_query_name(raw_query: str) -> str:
    for part in _clean(raw_query).split("&"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip().lower() == "name":
            return unquote_plus(value)
    return ""


def _looks_like_seq(value: str) -> bool:
    text = _clean(value)
    return bool(re.fullmatch(r"[0-9A-Za-z_-]{1,40}", text))


def _build_spec(file_id: str, file_seq: str, file_name: str = "") -> DownloadSpec | None:
    clean_id = unquote_plus(_clean(file_id))
    clean_seq = unquote_plus(_clean(file_seq))
    clean_name = unquote_plus(_clean(file_name))
    if not clean_id or not clean_seq:
        return None
    if "/" in clean_id or "?" in clean_id or "/" in clean_seq or "?" in clean_seq:
        return None
    if not _looks_like_seq(clean_seq):
        return None
    return DownloadSpec(file_id=clean_id, file_seq=clean_seq, file_name=clean_name)


def _split_js_args(raw: str) -> list[str]:
    args: list[str] = []
    buf: list[str] = []
    quote = ""
    escape = False
    depth = 0

    for ch in raw:
        if quote:
            buf.append(ch)
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == quote:
                quote = ""
            continue

        if ch in {"'", '"'}:
            quote = ch
            buf.append(ch)
            continue

        if ch in "([{":
            depth += 1
            buf.append(ch)
            continue

        if ch in ")]}":
            depth = max(0, depth - 1)
            buf.append(ch)
            continue

        if ch == "," and depth == 0:
            args.append("".join(buf).strip())
            buf = []
            continue

        buf.append(ch)

    tail = "".join(buf).strip()
    if tail:
        args.append(tail)
    return args


def _resolve_js_expr(expr: str, var_map: Mapping[str, str]) -> str:
    text = _clean(expr)
    if not text:
        return ""

    if text in var_map:
        return var_map[text]

    if len(text) >= 2 and text[0] in {"'", '"'} and text[-1] == text[0]:
        return _decode_js_string_literal(text)

    split_match = _SPLIT_EXPR_RE.search(text)
    if split_match:
        var_name = split_match.group(1)
        delimiter = split_match.group(3)
        index = int(split_match.group(4))
        base = var_map.get(var_name, "")
        if base and delimiter:
            parts = base.split(delimiter)
            if 0 <= index < len(parts):
                return _clean(parts[index])

    identity_match = re.search(r"([A-Za-z_$][\w$]*)", text)
    if identity_match:
        key = identity_match.group(1)
        if key in var_map:
            return var_map[key]

    if re.fullmatch(r"[0-9]+", text):
        return text
    return ""


def _extract_specs_from_token(raw_token: str, fallback_name: str = "") -> list[DownloadSpec]:
    token = _clean(raw_token)
    if not token:
        return []

    specs: list[DownloadSpec] = []

    for path in _FILES_PATH_RE.finditer(token):
        name_from_query = _extract_query_name(path.group(3) or "")
        spec = _build_spec(path.group(1), path.group(2), name_from_query or fallback_name)
        if spec is not None:
            specs.append(spec)

    if token.startswith("{") and token.endswith("}"):
        try:
            payload = json.loads(token)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            file_id = _clean(payload.get("fileId") or payload.get("atchFileId") or payload.get("id"))
            file_seq = _clean(payload.get("fileSeq") or payload.get("fileSn") or payload.get("seq"))
            file_name = _clean(payload.get("fileNm") or payload.get("orgnlFileNm") or payload.get("orignlFileNm"))
            spec = _build_spec(file_id, file_seq, file_name or fallback_name)
            if spec is not None:
                specs.append(spec)

    for delimiter in ("|", ",", ";"):
        parts = [part.strip() for part in token.split(delimiter)]
        if len(parts) < 2:
            continue
        file_id, file_seq = parts[0], parts[1]
        file_name = ""
        if len(parts) >= 3:
            file_name = parts[2]
        spec = _build_spec(file_id, file_seq, file_name or fallback_name)
        if spec is not None:
            specs.append(spec)

    return specs


def _dedupe_specs(specs: list[DownloadSpec]) -> list[DownloadSpec]:
    seen: set[tuple[str, str, str]] = set()
    out: list[DownloadSpec] = []
    for spec in specs:
        key = (spec.file_id, spec.file_seq, spec.file_name)
        if key in seen:
            continue
        seen.add(key)
        out.append(spec)
    return out


def _extract_specs_from_row(row: Mapping[str, object]) -> list[DownloadSpec]:
    specs: list[DownloadSpec] = []
    normalized: dict[str, str] = {}

    for key, value in row.items():
        nkey = _normalize_key(str(key))
        normalized[nkey] = _clean(value)

    token = normalized.get("saybrkdfilenm")
    if token:
        specs.extend(_extract_specs_from_token(token))

    id_keys = [
        key
        for key in normalized.keys()
        if key in {"fileid", "atchfileid", "attchfileid", "atchid"} or ("file" in key and key.endswith("id"))
    ]
    seq_keys = [
        key
        for key in normalized.keys()
        if key in {"fileseq", "filesn", "seq", "sn"}
        or ("file" in key and ("seq" in key or key.endswith("sn") or key.endswith("no")))
    ]
    name_keys = [
        key
        for key in normalized.keys()
        if "filename" in key or key.endswith("filenm") or key.endswith("file_nm") or key.endswith("orgnlfilenm")
    ]

    for id_key in id_keys:
        for seq_key in seq_keys:
            file_name = ""
            for name_key in name_keys:
                candidate_name = normalized.get(name_key, "")
                if candidate_name:
                    file_name = candidate_name
                    break
            spec = _build_spec(normalized.get(id_key, ""), normalized.get(seq_key, ""), file_name)
            if spec is not None:
                specs.append(spec)

    for value in normalized.values():
        if "/files/" in value:
            specs.extend(_extract_specs_from_token(value))

    return specs


def _extract_specs_from_html(detail_html: str) -> list[DownloadSpec]:
    specs: list[DownloadSpec] = []
    html = str(detail_html or "")
    if not html:
        return specs

    var_map: dict[str, str] = {}
    for key, raw_value in _VAR_ASSIGN_RE.findall(html):
        var_map[_clean(key)] = _decode_js_string_literal(raw_value)

    for key, value in list(var_map.items()):
        normalized_key = _normalize_key(key)
        if normalized_key in {"saybrkdfilenm", "fileinfo", "downloadfile"}:
            specs.extend(_extract_specs_from_token(value))

    for input_tag in _INPUT_RE.findall(html):
        attrs = {name.lower(): value for name, _, value in _ATTR_RE.findall(input_tag)}
        field_name = _clean(attrs.get("name") or attrs.get("id")).lower()
        field_value = _clean(attrs.get("value"))
        if not field_name or not field_value:
            continue
        var_map[field_name] = field_value
        if "saybrkdfilenm" in field_name or "/files/" in field_value:
            specs.extend(_extract_specs_from_token(field_value))

    file_id = _clean(var_map.get("fileid") or var_map.get("atchfileid") or var_map.get("attchfileid"))
    file_seq = _clean(var_map.get("fileseq") or var_map.get("filesn") or var_map.get("seq") or var_map.get("sn"))
    file_name = _clean(var_map.get("filename") or var_map.get("filenm") or var_map.get("orgnlfilenm"))
    simple_spec = _build_spec(file_id, file_seq, file_name)
    if simple_spec is not None:
        specs.append(simple_spec)

    for call in _DOWNLOAD_CALL_RE.findall(html):
        args = _split_js_args(call)
        if len(args) < 2:
            continue
        resolved_1 = _resolve_js_expr(args[0], var_map)
        resolved_2 = _resolve_js_expr(args[1], var_map)
        resolved_3 = _resolve_js_expr(args[2], var_map) if len(args) >= 3 else ""

        spec = _build_spec(resolved_1, resolved_2, resolved_3)
        if spec is not None:
            specs.append(spec)

        specs.extend(_extract_specs_from_token(resolved_1, fallback_name=resolved_3))

    for match in _FILES_PATH_RE.finditer(html):
        name_from_query = _extract_query_name(match.group(3) or "")
        spec = _build_spec(match.group(1), match.group(2), name_from_query)
        if spec is not None:
            specs.append(spec)

    return specs


def extract_download_specs(detail_html: str, row: Mapping[str, object]) -> list[DownloadSpec]:
    specs: list[DownloadSpec] = []
    specs.extend(_extract_specs_from_row(row))
    specs.extend(_extract_specs_from_html(detail_html))
    return _dedupe_specs(specs)


def _decode_text_bytes(payload: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "utf-16le", "utf-16be", "cp949", "euc-kr"):
        try:
            return payload.decode(encoding)
        except Exception:
            continue
    return payload.decode("utf-8", errors="ignore")


def _normalize_plain_text(value: str, keep_newlines: bool) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    if keep_newlines:
        lines = []
        for line in text.split("\n"):
            clean_line = _WHITESPACE_RE.sub(" ", line).strip()
            if clean_line:
                lines.append(clean_line)
        return "\n".join(lines)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _local_name(tag: object) -> str:
    text = str(tag or "")
    if "}" in text:
        return text.split("}", 1)[1].lower()
    return text.lower()


def _extract_text_from_xml(payload: bytes) -> str:
    try:
        root = ElementTree.fromstring(payload)
    except Exception:
        return ""

    parts: list[str] = []
    for elem in root.iter():
        text = _clean(elem.text)
        if not text:
            continue
        local = _local_name(elem.tag)
        if local in {"t", "text", "line", "span", "run", "p", "para", "paragraph"} or len(list(elem)) == 0:
            parts.append(text)

    return _normalize_plain_text("\n".join(parts), keep_newlines=True)


def extract_hwpx_text(payload: bytes, max_chars: int = 8000) -> str:
    if not payload:
        return ""
    if len(payload) > _MAX_HWPX_ARCHIVE_BYTES:
        return ""

    safe_max_chars = max(1, int(max_chars))

    try:
        zf = ZipFile(io.BytesIO(payload))
    except BadZipFile:
        return ""

    try:
        infos = zf.infolist()
        if len(infos) > _MAX_HWPX_ENTRY_COUNT:
            return ""

        total_uncompressed = 0
        names: list[str] = []
        for info in infos:
            file_size = max(0, int(info.file_size))
            if file_size > _MAX_HWPX_SINGLE_ENTRY_BYTES:
                return ""
            total_uncompressed += file_size
            if total_uncompressed > _MAX_HWPX_TOTAL_UNCOMPRESSED_BYTES:
                return ""
            names.append(info.filename)

        preview_names = [name for name in names if name.lower().endswith("prvtext.txt")]
        for name in sorted(preview_names):
            try:
                preview_text = _decode_text_bytes(zf.read(name))
            except Exception:
                continue
            cleaned = _normalize_plain_text(preview_text, keep_newlines=True)
            if cleaned:
                return cleaned[:safe_max_chars]

        section_names = [
            name
            for name in names
            if name.lower().startswith("contents/section") and name.lower().endswith(".xml")
        ]
        if not section_names:
            section_names = [
                name
                for name in names
                if name.lower().endswith(".xml") and "manifest" not in name.lower() and "settings" not in name.lower()
            ]

        blocks: list[str] = []
        for name in sorted(section_names):
            try:
                xml_payload = zf.read(name)
            except Exception:
                continue
            text = _extract_text_from_xml(xml_payload)
            if text:
                blocks.append(text)

        if not blocks:
            return ""

        merged = _normalize_plain_text("\n".join(blocks), keep_newlines=True)
        return merged[:safe_max_chars]
    finally:
        zf.close()


def _is_stop_heading(line: str) -> bool:
    text = _clean(line)
    if not text:
        return False

    for label in _STOP_HEADINGS:
        if re.fullmatch(rf"{re.escape(label)}\s*[:：-]?", text):
            return True
        if re.match(rf"{re.escape(label)}\s*[:：-]", text):
            return True
    return False


def extract_business_content_from_text(raw_text: str, max_chars: int = 2000) -> str:
    cleaned = _normalize_plain_text(raw_text, keep_newlines=True)
    if not cleaned:
        return ""

    lines = [line.strip() for line in cleaned.split("\n") if line.strip()]

    for index, line in enumerate(lines):
        if not _HEADING_RE.search(line):
            continue

        first = re.sub(r"^.*?사업\s*내용\s*[:：-]?\s*", "", line).strip()
        collected: list[str] = []
        if first and first != line:
            collected.append(first)

        for next_line in lines[index + 1 :]:
            if _is_stop_heading(next_line):
                break
            collected.append(next_line)

        value = _WHITESPACE_RE.sub(" ", " ".join(collected)).strip()
        if value:
            return value[: max(1, int(max_chars))]

    fallback = _WHITESPACE_RE.sub(" ", " ".join(lines)).strip()
    return fallback[: max(1, int(max_chars))]
