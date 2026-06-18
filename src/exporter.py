from __future__ import annotations

import datetime as dt
import xml.sax.saxutils as xml_escape
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

_CORE_HEADERS = [
    "번호",
    "사업명",
    "지역",
    "자치단체",
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
_PREFERRED_HEADERS = _CORE_HEADERS


def _col_name(index: int) -> str:
    letters = ""
    i = index
    while i > 0:
        i, rem = divmod(i - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _build_sheet_xml(headers: list[str], rows: list[dict[str, str]]) -> str:
    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        '<sheetData>',
    ]

    # Header row
    cells = []
    for idx, header in enumerate(headers, start=1):
        ref = f"{_col_name(idx)}1"
        escaped = xml_escape.escape(str(header))
        cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escaped}</t></is></c>')
    lines.append(f'<row r="1">{"".join(cells)}</row>')

    # Data rows
    for r_idx, row in enumerate(rows, start=2):
        cells = []
        for c_idx, header in enumerate(headers, start=1):
            ref = f"{_col_name(c_idx)}{r_idx}"
            value = xml_escape.escape(str(row.get(header, "")))
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>')
        lines.append(f'<row r="{r_idx}">{"".join(cells)}</row>')

    lines.extend(["</sheetData>", "</worksheet>"])
    return "".join(lines)


def _ordered_headers(rows: list[dict[str, str]]) -> list[str]:
    headers: list[str] = []

    for key in _PREFERRED_HEADERS:
        if any(key in row for row in rows):
            headers.append(key)

    for row in rows:
        for key in row.keys():
            if key not in headers:
                headers.append(key)

    return headers


def export_to_excel(rows: list[dict[str, str]], output_path: Path) -> None:
    """Write rows to a minimal XLSX file without external dependencies."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    headers = _ordered_headers(rows) if rows else []

    sheet_xml = _build_sheet_xml(headers, rows)
    now = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>
"""

    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
"""

    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="policies" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>
"""

    workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>
"""

    core = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:creator>lofin-crawler</dc:creator>
  <cp:lastModifiedBy>lofin-crawler</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
</cp:coreProperties>
"""

    app = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Python</Application>
</Properties>
"""

    with ZipFile(output, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        zf.writestr("docProps/core.xml", core)
        zf.writestr("docProps/app.xml", app)


def _escape_markdown(value: object) -> str:
    text = str(value or "").strip()
    return text.replace("\\", "\\\\").replace("|", "\\|")


def _policy_heading(row: dict[str, str], index: int) -> str:
    title = str(row.get("사업명") or row.get("세부사업명") or row.get("정책명") or "").strip()
    if not title:
        title = f"정책 {index}"
    return title


def export_to_markdown(rows: list[dict[str, str]], output_path: Path, *, title: str = "Lofin 정책 수집 결과") -> None:
    """Write rows to a readable Markdown document."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    headers = _ordered_headers(rows) if rows else []
    lines: list[str] = [
        f"# {title}",
        "",
        f"- 생성시각: {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- 수집건수: {len(rows)}",
        "",
    ]

    if not rows:
        lines.append("수집 결과가 없습니다.")
        output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return

    lines.extend(["## 목록", "", "| 번호 | 사업명 | 지역 | 회계연도 |", "| --- | --- | --- | --- |"])
    for idx, row in enumerate(rows, start=1):
        number = _escape_markdown(row.get("번호") or idx)
        name = _escape_markdown(row.get("사업명") or row.get("세부사업명") or row.get("정책명") or "")
        region = _escape_markdown(row.get("지역") or row.get("소관명") or "")
        year = _escape_markdown(row.get("회계연도") or "")
        lines.append(f"| {number} | {name} | {region} | {year} |")

    lines.extend(["", "## 상세", ""])
    for idx, row in enumerate(rows, start=1):
        number = str(row.get("번호") or idx).strip()
        lines.extend([f"### {number}. {_policy_heading(row, idx)}", ""])
        for header in headers:
            value = str(row.get(header, "")).strip()
            if not value:
                continue
            lines.append(f"- **{header}**: {value}")
        lines.append("")

    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
