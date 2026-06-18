#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import subprocess
import ssl
import sys
import time
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, quote, urlencode
from urllib.request import Request, urlopen

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from src.central_source import (
    CENTRAL_LIST_URL,
    CENTRAL_SOURCE,
    LOCAL_SOURCE,
    SUPPORTED_SOURCES,
    map_central_rows,
    validate_central_year,
    normalize_source,
)
from src.central_hwpx import (
    DownloadSpec,
    extract_business_content_from_text,
    extract_download_specs,
    extract_hwpx_text,
)
from src.chunked_writer import ChunkedPolicyWriter
from src.detail_fallback import should_open_detail_page
from src.detail_popup_api import build_popup_detail_form
from src.env_loader import load_env_file
from src.exporter import export_to_excel, export_to_markdown
from src.parser import (
    TARGET_FIELDS,
    parse_policy_detail,
    parse_policy_detail_from_script,
    parse_policy_detail_from_script_object,
    select_target_fields,
)
from src.policy_index import export_policy_index
from src.web_helpers import normalize_region_filter

load_env_file()

URL = "https://www.lofin365.go.kr/portal/LF3120202.do"
DETAIL_URL = "https://www.lofin365.go.kr/portal/LF3120204.do"
LIST_API_URL = "https://www.lofin365.go.kr/lf/lnncGramStst/laf/exeSvi/retvLstDtlsBybsnAneSitu.do"
DETAIL_POPUP_API_URL = "https://www.lofin365.go.kr/lf/lnncGramStst/laf/exeSvi/retvDtlsBybsnAneSituDts.do"
CENTRAL_DETAIL_URL = "https://www.openfiscaldata.go.kr/op/ko/bs/cls/UOPKOBSZ01"
CENTRAL_FILES_BASE_URL = "https://www.openfiscaldata.go.kr/files"
DETAIL_REQUEST_TIMEOUT_MS_DEFAULT = 7_000
DETAIL_RUNTIME_TIMEOUT_MS_DEFAULT = 800
DETAIL_DELAY_MS_DEFAULT = 20
DETAIL_CONCURRENCY_DEFAULT = 24
DETAIL_BATCH_SIZE_DEFAULT = 1_000
MAX_CRAWL_LIMIT = 500_000
HTTPS_CONTEXT = ssl.create_default_context()

# The site is dynamic, so keep multiple selector candidates.
YEAR_FILTER_SELECTORS = [
    "#inqYmd",
    "[name='inqYmd']",
    "[id*='inqYmd']",
    "[name*='inqYmd']",
    "#inqFyr",
    "[name='inqFyr']",
    "[id*='inqFyr']",
    "[name*='inqFyr']",
    "select#inqYmd",
    "select[name*='fyr']",
    "select[id*='fyr']",
    "select[name*='year']",
    "select[id*='year']",
    "select[name*='ymd']",
    "select[id*='ymd']",
    "input[name*='year']",
    "input[id*='year']",
    "input[name*='ymd']",
    "input[id*='ymd']",
]
REGION_FILTER_SELECTORS = [
    "#inqCap",
    "[name='inqCap']",
    "[id*='inqCap']",
    "[name*='inqCap']",
    "#inqSgg",
    "[name='inqSgg']",
    "[id*='inqSgg']",
    "[name*='inqSgg']",
    "#lafCd",
    "[name='lafCd']",
    "[id*='lafCd']",
    "[name*='lafCd']",
    "#inqLafCd",
    "[name='inqLafCd']",
    "[id*='inqLafCd']",
    "[name*='inqLafCd']",
    "select[name*='laf']",
    "select[id*='laf']",
    "select[name*='region']",
    "select[id*='region']",
    "select[name*='sido']",
    "select[id*='sido']",
    "select[name*='sigungu']",
    "select[id*='sigungu']",
    "input[name*='region']",
    "input[id*='region']",
]
SEARCH_INPUT_SELECTORS = [
    "#srchCrstCn",
    "[name='srchCrstCn']",
    "input[title*='사업명']",
    "input[placeholder*='사업명']",
    "input[name*='biz']",
    "input[name*='srch']",
    "input[type='text']",
]
SEARCH_BUTTON_SELECTORS = [
    "#srchBtn",
    "button.btnSearch",
    "button:has-text('검색')",
    "button:has-text('조회')",
    "a:has-text('검색')",
    "a:has-text('조회')",
    "input[type='submit']",
]
LIST_ITEM_SELECTORS = [
    "table tbody tr td a",
    "table tbody tr td button",
    "ul li a",
]
DETAIL_PANEL_SELECTORS = [
    "#layerPop",
    ".layerPop",
    ".modal",
    ".popup",
    "#contents",
]
DETAIL_CLOSE_SELECTORS = [
    "button:has-text('닫기')",
    "a:has-text('닫기')",
    ".btn_close",
]


class CrawlStoppedError(RuntimeError):
    """Raised when caller requested the crawling task to stop."""


def _raise_if_stopped(stop_requested: Callable[[], bool] | None) -> None:
    if stop_requested is not None and stop_requested():
        raise CrawlStoppedError("사용자 요청으로 작업이 중지되었습니다.")


def _iter_contexts(page):
    # Search in the main document first, then each iframe.
    yield page
    for frame in page.frames:
        if frame != page.main_frame:
            yield frame


def _first_visible(page, selectors: list[str]):
    for context in _iter_contexts(page):
        for selector in selectors:
            locator = context.locator(selector).first
            if locator.count() == 0:
                continue
            try:
                locator.wait_for(state="visible", timeout=1500)
                return locator
            except PlaywrightTimeoutError:
                continue
    return None


def _find_click_target(page, selectors: list[str], text: str):
    for context in _iter_contexts(page):
        for selector in selectors:
            target = context.locator(selector, has_text=text).first
            if target.count() == 0:
                continue
            return target
    return None


def _write_debug_artifacts(page, debug_dir: Path, reason: str) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    timestamp = str(int(time.time()))
    page.screenshot(path=str(debug_dir / f"{timestamp}.png"), full_page=True)
    (debug_dir / f"{timestamp}.html").write_text(page.content(), encoding="utf-8")
    frame_info = [
        {"name": frame.name, "url": frame.url}
        for frame in page.frames
    ]
    (debug_dir / f"{timestamp}.json").write_text(
        json.dumps({"reason": reason, "frames": frame_info}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _get_policy_names(page) -> list[str]:
    names: list[str] = []
    for context in _iter_contexts(page):
        for selector in LIST_ITEM_SELECTORS:
            loc = context.locator(selector)
            try:
                texts = loc.evaluate_all(
                    """els => els
                        .map(el => (el.textContent || '').replace(/\\s+/g, ' ').trim())
                        .filter(Boolean)"""
                )
            except Exception:
                texts = []

            for text in texts:
                if text and text not in names:
                    names.append(text)
            if names:
                break
        if names:
            break
    return names


def _get_policy_entries(page) -> list[dict[str, str]]:
    for context in _iter_contexts(page):
        try:
            data = context.evaluate(
                """() => {
  const inqYmd = (document.querySelector('#inqYmd')?.value || '').replace(/-/g, '');
  let rows = [];
  try {
    if (typeof gridInfos !== 'undefined' && gridInfos && gridInfos[0] && gridInfos[0].grid && gridInfos[0].grid.getData) {
      rows = gridInfos[0].grid.getData();
    } else if (typeof dataArr !== 'undefined' && Array.isArray(dataArr) && Array.isArray(dataArr[0])) {
      rows = dataArr[0];
    }
  } catch (e) {}
  return (rows || []).map(r => ({
    name: String(r.codeNm4 || r.dbizNm || r.dbiznm || '').trim(),
    region: String(r.codeNm || r.lafNm || r.sidoNm || '').trim(),
    municipality: String(r.codeNm2 || r.sggNm || r.sigunguNm || r.lafNm2 || '').trim(),
    dbizCd: String(r.code || r.dbizCd || '').trim(),
    lafCd: String(r.lafCd || r.lafcd || '').trim(),
    fyr: String(r.rsltYr || r.fyr || '').trim(),
    inqYmd: inqYmd
  })).filter(x => x.name);
}"""
            )
        except Exception:
            continue
        if isinstance(data, list) and data:
            entries: list[dict[str, str]] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                entries.append(
                    {
                        "name": name,
                        "region": str(item.get("region", "")).strip(),
                        "municipality": str(item.get("municipality", "")).strip(),
                        "dbizCd": str(item.get("dbizCd", "")).strip(),
                        "lafCd": str(item.get("lafCd", "")).strip(),
                        "fyr": str(item.get("fyr", "")).strip(),
                        "inqYmd": str(item.get("inqYmd", "")).strip(),
                    }
                )
            if entries:
                return entries
    return []


def _map_policy_list_items(items: list[object], inq_ymd: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("codeNm4") or item.get("dbizNm") or item.get("dbiznm") or "").strip()
        if not name:
            continue
        entries.append(
            {
                "name": name,
                "region": str(item.get("codeNm") or item.get("lafNm") or item.get("sidoNm") or "").strip(),
                "municipality": str(
                    item.get("codeNm2") or item.get("sggNm") or item.get("sigunguNm") or item.get("lafNm2") or ""
                ).strip(),
                "dbizCd": str(item.get("code") or item.get("dbizCd") or "").strip(),
                "lafCd": str(item.get("lafCd") or item.get("lafcd") or "").strip(),
                "fyr": str(item.get("rsltYr") or item.get("fyr") or "").strip(),
                "inqYmd": str(inq_ymd or "").replace("-", "").strip(),
            }
        )
    return entries


def _extract_policy_entries_from_list_html(html: str, inq_ymd: str) -> list[dict[str, str]]:
    marker = "var list ="
    idx = html.find(marker)
    if idx < 0:
        return []
    start = idx + len(marker)
    while start < len(html) and html[start].isspace():
        start += 1
    try:
        items, _ = json.JSONDecoder().raw_decode(html[start:])
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    return _map_policy_list_items(items, inq_ymd)


def _fetch_policy_entries_page_via_http(
    *,
    page_no: int,
    base_form: dict[str, str],
    headers: dict[str, str],
    timeout_ms: int,
) -> list[dict[str, str]]:
    form = dict(base_form)
    form["curPage"] = str(max(1, int(page_no)))
    html = _fetch_text_via_http(
        LIST_API_URL,
        timeout_ms=timeout_ms,
        headers=headers,
        form_data=form,
    )
    if not html:
        return []
    return _extract_policy_entries_from_list_html(html, str(form.get("inqYmd", "")))


def _iter_policy_entry_batches_via_http(
    *,
    base_form: dict[str, str],
    headers: dict[str, str],
    limit: int,
    timeout_ms: int,
    stop_requested: Callable[[], bool] | None = None,
):
    yielded = 0
    page_no = 1
    seen: set[tuple[str, str, str, str, str]] = set()
    max_page_turns = max(60, min(limit * 2, 500_000))
    while yielded < limit and page_no <= max_page_turns:
        _raise_if_stopped(stop_requested)
        entries = _fetch_policy_entries_page_via_http(
            page_no=page_no,
            base_form=base_form,
            headers=headers,
            timeout_ms=timeout_ms,
        )
        if not entries:
            break
        batch: list[dict[str, str]] = []
        for entry in entries:
            key = (
                entry.get("dbizCd", ""),
                entry.get("lafCd", ""),
                entry.get("fyr", ""),
                entry.get("name", ""),
                entry.get("municipality", ""),
            )
            if key in seen:
                continue
            seen.add(key)
            yielded += 1
            batch.append(entry)
            if yielded >= limit:
                break
        if not batch:
            break
        yield batch
        page_no += 1


def _wait_policy_entries(
    page,
    timeout_ms: int = 7000,
    stop_requested: Callable[[], bool] | None = None,
) -> list[dict[str, str]]:
    waited = 0
    while waited <= timeout_ms:
        _raise_if_stopped(stop_requested)
        entries = _get_policy_entries(page)
        if entries:
            return entries
        page.wait_for_timeout(300)
        waited += 300
    return []


def _get_paging_info(page) -> dict[str, object]:
    default = {"current": 1, "pages": [1]}
    for context in _iter_contexts(page):
        try:
            info = context.evaluate(
                """() => {
  const paging = document.querySelector('.paging');
  if (!paging) return { current: 1, pages: [1] };

  let current = 1;
  const strong = paging.querySelector('strong');
  if (strong) {
    const m = (strong.textContent || '').match(/\\d+/);
    if (m) current = Number(m[0]);
  }

  const pages = [];
  const links = Array.from(paging.querySelectorAll("a[onclick*='lf.search']"));
  for (const a of links) {
    const onclick = a.getAttribute('onclick') || '';
    const m = onclick.match(/lf\\.search\\('([^']+)'\\)/);
    if (!m) continue;
    const n = Number(m[1]);
    if (!Number.isNaN(n) && n > 0) pages.push(n);
  }

  if (!pages.includes(current)) pages.push(current);
  pages.sort((a, b) => a - b);
  return { current, pages: Array.from(new Set(pages)) };
}"""
            )
        except Exception:
            continue
        if isinstance(info, dict):
            return info
    return default


def _go_to_page(page, target_page: int) -> bool:
    changed = False
    for context in _iter_contexts(page):
        try:
            changed = context.evaluate(
                """(targetPage) => {
  if (typeof lf !== 'undefined' && lf && typeof lf.search === 'function') {
    lf.search(String(targetPage));
    return true;
  }

  const selector = `a[onclick*="lf.search('${targetPage}')"]`;
  const link = document.querySelector(selector);
  if (link) {
    link.click();
    return true;
  }
  return false;
}""",
                target_page,
            )
        except Exception:
            continue
        if changed:
            break

    if not changed:
        return False

    try:
        page.wait_for_function(
            """(targetPage) => {
  const strong = document.querySelector('.paging strong');
  if (!strong) return false;
  const m = (strong.textContent || '').match(/\\d+/);
  return !!m && Number(m[0]) === Number(targetPage);
}""",
            arg=target_page,
            timeout=7000,
        )
    except PlaywrightTimeoutError:
        page.wait_for_timeout(1200)

    return True


def _collect_policy_entries_across_pages(
    page,
    limit: int,
    stop_requested: Callable[[], bool] | None = None,
) -> list[dict[str, str]]:
    collected: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    page_turns = 0
    max_page_turns = max(60, min(limit * 2, 500_000))

    while len(collected) < limit and page_turns <= max_page_turns:
        _raise_if_stopped(stop_requested)
        current_entries = _wait_policy_entries(
            page,
            timeout_ms=8000 if page_turns == 0 else 5000,
            stop_requested=stop_requested,
        )
        for entry in current_entries:
            key = (
                entry.get("dbizCd", ""),
                entry.get("lafCd", ""),
                entry.get("fyr", ""),
                entry.get("name", ""),
                entry.get("municipality", ""),
            )
            if key in seen:
                continue
            seen.add(key)
            collected.append(entry)
            if len(collected) >= limit:
                break

        if len(collected) >= limit:
            break

        info = _get_paging_info(page)
        try:
            current = int(info.get("current", 1))
        except Exception:
            current = 1

        pages_raw = info.get("pages", [1])
        pages: list[int] = []
        for p in pages_raw if isinstance(pages_raw, list) else [1]:
            try:
                num = int(p)
            except Exception:
                continue
            if num > 0:
                pages.append(num)
        pages = sorted(set(pages))

        next_pages = [p for p in pages if p > current]
        if not next_pages:
            break

        _raise_if_stopped(stop_requested)
        next_page = next_pages[0]
        moved = _go_to_page(page, next_page)
        if not moved:
            break

        page_turns += 1

    return collected


def _iter_policy_entries_across_pages(
    page,
    limit: int,
    stop_requested: Callable[[], bool] | None = None,
):
    for batch in _iter_policy_entry_batches_across_pages(
        page,
        limit=limit,
        stop_requested=stop_requested,
    ):
        for entry in batch:
            yield entry


def _iter_policy_entry_batches_across_pages(
    page,
    limit: int,
    stop_requested: Callable[[], bool] | None = None,
):
    yielded = 0
    seen: set[tuple[str, str, str, str, str]] = set()
    page_turns = 0
    max_page_turns = max(60, min(limit * 2, 500_000))

    while yielded < limit and page_turns <= max_page_turns:
        _raise_if_stopped(stop_requested)
        current_entries = _wait_policy_entries(
            page,
            timeout_ms=8000 if page_turns == 0 else 5000,
            stop_requested=stop_requested,
        )
        batch: list[dict[str, str]] = []
        for entry in current_entries:
            key = (
                entry.get("dbizCd", ""),
                entry.get("lafCd", ""),
                entry.get("fyr", ""),
                entry.get("name", ""),
                entry.get("municipality", ""),
            )
            if key in seen:
                continue
            seen.add(key)
            yielded += 1
            batch.append(entry)
            if yielded >= limit:
                break

        if batch:
            yield batch

        if yielded >= limit:
            break

        info = _get_paging_info(page)
        try:
            current = int(info.get("current", 1))
        except Exception:
            current = 1

        pages_raw = info.get("pages", [1])
        pages: list[int] = []
        for p in pages_raw if isinstance(pages_raw, list) else [1]:
            try:
                num = int(p)
            except Exception:
                continue
            if num > 0:
                pages.append(num)
        pages = sorted(set(pages))

        next_pages = [p for p in pages if p > current]
        if not next_pages:
            break

        _raise_if_stopped(stop_requested)
        moved = _go_to_page(page, next_pages[0])
        if not moved:
            break

        page_turns += 1


def _iter_sized_entry_batches(
    page_batches,
    *,
    batch_size: int,
    stop_requested: Callable[[], bool] | None = None,
):
    safe_batch_size = max(1, min(int(batch_size), MAX_CRAWL_LIMIT))
    buffer: list[dict[str, str]] = []
    for page_batch in page_batches:
        _raise_if_stopped(stop_requested)
        buffer.extend(page_batch)
        while len(buffer) >= safe_batch_size:
            yield buffer[:safe_batch_size]
            buffer = buffer[safe_batch_size:]
    if buffer:
        yield buffer


def _build_detail_url(entry: dict[str, str]) -> str:
    dbiz_cd = entry.get("dbizCd", "").strip()
    if not dbiz_cd:
        return ""
    params = {
        "dbizCd": dbiz_cd,
        "lafCd": entry.get("lafCd", "").strip(),
        "fyr": entry.get("fyr", "").strip(),
        "inqYmd": entry.get("inqYmd", "").strip(),
    }
    return f"{DETAIL_URL}?{urlencode(params)}"


def _parse_post_form_data(post_data: str) -> dict[str, str]:
    return dict(parse_qsl(post_data or "", keep_blank_values=True))


def _match_value_or_text(target: str, value: str, text: str) -> bool:
    t = target.strip().lower()
    v = value.strip().lower()
    s = text.strip().lower()
    if not t:
        return False
    if t == v or t == s:
        return True
    return (t in v) or (t in s)


def _set_filter_value(page, selectors: list[str], desired: str, label: str) -> bool:
    script = """
({selector, desired, label}) => {
  const norm = (v) => String(v ?? '').trim().toLowerCase();
  const normalizeInputValue = (type, raw) => {
    const v = String(raw ?? '').trim();
    if (type === 'date') {
      if (/^\\d{4}$/.test(v)) return `${v}-01-01`;
      if (/^\\d{4}-\\d{2}$/.test(v)) return `${v}-01`;
    }
    if (type === 'month') {
      if (/^\\d{4}$/.test(v)) return `${v}-01`;
    }
    return v;
  };
  const match = (target, value, text) => {
    const t = norm(target);
    const v = norm(value);
    const s = norm(text);
    if (!t) return false;
    if (t === v || t === s) return true;
    return v.includes(t) || s.includes(t) || t.includes(v) || t.includes(s);
  };

  const elements = Array.from(document.querySelectorAll(selector));
  for (const el of elements) {
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'select') {
      const options = Array.from(el.options || []);
      let selectedOpt = null;
      for (const opt of options) {
        const value = String(opt.value || '').trim();
        const text = String(opt.textContent || '').trim();
        if (!value && !text) continue;
        if (norm(desired) === norm(value) || norm(desired) === norm(text)) {
          selectedOpt = {value, text};
          break;
        }
        if (!selectedOpt && match(desired, value, text)) {
          selectedOpt = {value, text};
        }
      }
      if (selectedOpt) {
        if (selectedOpt.value) {
          el.value = selectedOpt.value;
        } else {
          const found = options.find((o) => norm(o.textContent) === norm(selectedOpt.text));
          if (found) el.value = found.value;
        }
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        return true;
      }
      continue;
    }

    if (tag === 'input') {
      const type = String(el.getAttribute('type') || 'text').toLowerCase();
      if (['text', 'search', 'date', 'month', 'number', 'hidden'].includes(type)) {
        el.value = normalizeInputValue(type, desired);
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        return true;
      }
    }
  }
  return false;
}
"""

    for context in _iter_contexts(page):
        for selector in selectors:
            try:
                changed = context.evaluate(
                    script, {"selector": selector, "desired": desired, "label": label}
                )
            except Exception:
                continue
            if changed:
                page.wait_for_timeout(600)
                return True

    # Fallback: discover target among all selects/inputs using value/text heuristics.
    discover_script = """
({desired, label}) => {
  const norm = (v) => String(v ?? '').trim().toLowerCase();
  const nDesired = norm(desired);
  const tokenMap = {
    year: ['year', 'fyr', 'ymd', 'date', 'inqymd', 'inqfyr'],
    region: ['cap', 'sgg', 'laf', 'sido', 'sigungu', 'region', 'area', 'loc', 'inqlafcd']
  };
  const kind = label.includes('지역') ? 'region' : 'year';
  const tokens = tokenMap[kind];
  const hasToken = (txt) => {
    const t = norm(txt);
    return tokens.some(k => t.includes(k));
  };
  const maybeDate = (v) => /^\\d{4}(-\\d{2}(-\\d{2})?)?$/.test(String(v || '').trim());
  const normalizeInputValue = (type, raw) => {
    const v = String(raw ?? '').trim();
    if (type === 'date') {
      if (/^\\d{4}$/.test(v)) return `${v}-01-01`;
      if (/^\\d{4}-\\d{2}$/.test(v)) return `${v}-01`;
    }
    if (type === 'month') {
      if (/^\\d{4}$/.test(v)) return `${v}-01`;
    }
    return v;
  };

  const selects = Array.from(document.querySelectorAll('select'));
  for (const el of selects) {
    const idn = `${el.id || ''} ${el.name || ''} ${el.className || ''}`;
    const options = Array.from(el.options || []);
    let selectedValue = '';
    let selectedByText = false;
    for (const opt of options) {
      const ov = String(opt.value || '').trim();
      const ot = String(opt.textContent || '').trim();
      if (!ov && !ot) continue;
      const exact = nDesired === norm(ov) || nDesired === norm(ot);
      const fuzzy = norm(ov).includes(nDesired) || norm(ot).includes(nDesired) || nDesired.includes(norm(ov)) || nDesired.includes(norm(ot));
      if (exact || fuzzy) {
        selectedValue = ov || selectedValue;
        selectedByText = true;
        if (!selectedValue) {
          const found = options.find(x => norm(x.textContent) === norm(ot));
          if (found) {
            selectedValue = String(found.value || '');
            selectedByText = true;
          }
        }
        break;
      }
    }
    if (selectedValue || selectedByText || (hasToken(idn) && options.length)) {
      if (!selectedValue && hasToken(idn)) {
        const candidate = options.find(opt => norm(opt.textContent).includes(nDesired) || norm(opt.value).includes(nDesired));
        if (candidate) {
          selectedValue = String(candidate.value || '');
          selectedByText = true;
        }
      }
      if (selectedValue || selectedByText) {
        el.value = selectedValue;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        return true;
      }
    }
  }

  const inputs = Array.from(document.querySelectorAll('input'));
  for (const el of inputs) {
    const type = norm(el.type || 'text');
    const idn = `${el.id || ''} ${el.name || ''} ${el.className || ''}`;
    if (!['text','search','date','month','number','hidden'].includes(type)) continue;
    if (kind === 'year') {
      if (hasToken(idn) || maybeDate(desired)) {
        el.value = normalizeInputValue(type, desired);
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        return true;
      }
    } else {
      if (hasToken(idn)) {
        el.value = desired;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        return true;
      }
    }
  }

  return false;
}
"""
    for context in _iter_contexts(page):
        try:
            changed = context.evaluate(discover_script, {"desired": desired, "label": label})
        except Exception:
            continue
        if changed:
            page.wait_for_timeout(700)
            return True

    # Print lightweight candidates for quick selector tuning.
    for context in _iter_contexts(page):
        try:
            info = context.evaluate(
                """() => ({
  selects: Array.from(document.querySelectorAll('select')).slice(0, 30).map(el => ({
    id: el.id || '', name: el.name || '', cls: el.className || '',
    sample: Array.from(el.options || []).slice(0, 5).map(o => (o.textContent || '').trim())
  })),
  inputs: Array.from(document.querySelectorAll('input')).slice(0, 50).map(el => ({
    id: el.id || '', name: el.name || '', type: el.type || 'text', cls: el.className || ''
  }))
})"""
            )
        except Exception:
            continue
        try:
            print(f"[디버그] {label} 후보 selects: {json.dumps(info.get('selects', []), ensure_ascii=False)}")
            print(f"[디버그] {label} 후보 inputs: {json.dumps(info.get('inputs', []), ensure_ascii=False)}")
        except Exception:
            pass
        break

    print(f"[경고] {label} 필터를 찾지 못했거나 값 적용에 실패했습니다: {desired}")
    return False


def _extract_runtime_script_selected(page) -> dict[str, str]:
    for context in _iter_contexts(page):
        try:
            obj = context.evaluate("""
() => {
  if (typeof list3 !== 'undefined' && list3) return list3;
  if (typeof window !== 'undefined' && window.list3) return window.list3;
  if (typeof dataArr !== 'undefined' && Array.isArray(dataArr) && dataArr[2]) return dataArr[2];
  return null;
}
""")
        except Exception:
            continue

        if isinstance(obj, dict):
            parsed = parse_policy_detail_from_script_object(obj)
            if parsed:
                return parsed
    return {}


def _wait_runtime_script_selected(page, timeout_ms: int = 2500) -> dict[str, str]:
    waited = 0
    while waited <= timeout_ms:
        selected = _extract_runtime_script_selected(page)
        if any(selected.get(k) for k in TARGET_FIELDS):
            return selected
        page.wait_for_timeout(250)
        waited += 250
    return {}


def _extract_selected_from_html(detail_html: str, full_html: str) -> dict[str, str]:
    selected = select_target_fields(parse_policy_detail(detail_html))
    if detail_html == full_html:
        full_selected = selected
        script_selected = parse_policy_detail_from_script(detail_html)
        script_full_selected = script_selected
    else:
        full_selected = select_target_fields(parse_policy_detail(full_html))
        script_selected = parse_policy_detail_from_script(detail_html)
        script_full_selected = parse_policy_detail_from_script(full_html)
    for key in TARGET_FIELDS:
        if not selected.get(key):
            selected[key] = full_selected.get(key, "")
        if not selected.get(key):
            selected[key] = script_selected.get(key, "")
        if not selected.get(key):
            selected[key] = script_full_selected.get(key, "")
    return selected


def _extract_selected_from_responses(response_bodies: list[str]) -> dict[str, str]:
    selected: dict[str, str] = {key: "" for key in TARGET_FIELDS}
    for body in reversed(response_bodies):
        candidate = _extract_selected_from_html(body, body)
        for key in TARGET_FIELDS:
            if not selected.get(key):
                selected[key] = candidate.get(key, "")
        if any(selected.get(key) for key in TARGET_FIELDS):
            break
    return selected


def _count_selected_fields(selected: dict[str, str]) -> int:
    return sum(1 for key in TARGET_FIELDS if str(selected.get(key, "")).strip())


def _should_open_detail_page(selected: dict[str, str]) -> bool:
    return should_open_detail_page(selected, min_selected_fields=2)


def _merge_selected_fields(base: dict[str, str], extra: dict[str, str]) -> dict[str, str]:
    merged = dict(base)
    for key in TARGET_FIELDS:
        if not merged.get(key):
            merged[key] = str(extra.get(key, "")).strip()
    return merged


def _fetch_detail_html_via_request(page, detail_url: str, timeout_ms: int) -> str:
    api_request = getattr(page.context, "request", None)
    if api_request is None:
        return ""

    safe_timeout_ms = max(1_000, min(int(timeout_ms), 60_000))
    try:
        response = api_request.get(detail_url, timeout=safe_timeout_ms)
    except Exception:
        return ""

    try:
        ok = bool(getattr(response, "ok", False))
    except Exception:
        ok = False
    if not ok:
        return ""

    try:
        return str(response.text() or "")
    except Exception:
        return ""


def _fetch_detail_html_via_popup_api(page, entry: dict[str, str], timeout_ms: int) -> str:
    api_request = getattr(page.context, "request", None)
    if api_request is None:
        return ""

    form_data = build_popup_detail_form(entry)
    if not form_data.get("dbizCd"):
        return ""

    safe_timeout_ms = max(1_000, min(int(timeout_ms), 60_000))
    try:
        response = api_request.post(DETAIL_POPUP_API_URL, form=form_data, timeout=safe_timeout_ms)
    except Exception:
        return ""

    try:
        ok = bool(getattr(response, "ok", False))
    except Exception:
        ok = False
    if not ok:
        return ""

    try:
        text = str(response.text() or "")
    except Exception:
        return ""
    if "<" not in text:
        return ""
    return text


def _cookie_header_from_context(context) -> str:
    try:
        cookies = context.cookies()
    except Exception:
        return ""
    pairs: list[str] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name", "")).strip()
        value = str(cookie.get("value", "")).strip()
        if name:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def _detail_request_headers(page) -> dict[str, str]:
    try:
        user_agent = str(page.evaluate("() => navigator.userAgent") or "").strip()
    except Exception:
        user_agent = ""
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6",
        "Referer": URL,
    }
    if user_agent:
        headers["User-Agent"] = user_agent
    cookie_header = _cookie_header_from_context(page.context)
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers


def _fetch_text_via_http(
    url: str,
    *,
    timeout_ms: int,
    headers: dict[str, str],
    form_data: dict[str, str] | None = None,
) -> str:
    safe_timeout_sec = max(1.0, min(int(timeout_ms), 60_000) / 1000.0)
    request_headers = dict(headers)
    data = None
    method = "GET"
    if form_data is not None:
        data = urlencode(form_data).encode("utf-8")
        method = "POST"
        request_headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"

    try:
        request = Request(url, data=data, headers=request_headers, method=method)
        with urlopen(request, timeout=safe_timeout_sec, context=HTTPS_CONTEXT) as response:
            status = int(getattr(response, "status", 0) or 0)
            if status < 200 or status >= 400:
                return ""
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read()
    except Exception:
        return _fetch_text_via_curl(
            url,
            timeout_sec=safe_timeout_sec,
            headers=request_headers,
            form_data=form_data,
        )

    if form_data is not None and "<" not in body[:200].decode("utf-8", errors="ignore"):
        return ""
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _fetch_text_via_curl(
    url: str,
    *,
    timeout_sec: float,
    headers: dict[str, str],
    form_data: dict[str, str] | None = None,
) -> str:
    cmd = [
        "curl",
        "-L",
        "-sS",
        "--fail",
        "--max-time",
        str(max(1, int(timeout_sec))),
    ]
    for key, value in headers.items():
        if value:
            cmd.extend(["-H", f"{key}: {value}"])
    if form_data is not None:
        cmd.extend(["--data", urlencode(form_data)])
    cmd.append(url)

    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, timeout_sec + 1.0),
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    text = result.stdout or ""
    if form_data is not None and "<" not in text:
        return ""
    return text


def _fetch_detail_html_via_http(
    detail_url: str,
    *,
    timeout_ms: int,
    headers: dict[str, str],
) -> str:
    if not detail_url:
        return ""
    return _fetch_text_via_http(detail_url, timeout_ms=timeout_ms, headers=headers)


def _fetch_popup_html_via_http(
    entry: dict[str, str],
    *,
    timeout_ms: int,
    headers: dict[str, str],
) -> str:
    form_data = build_popup_detail_form(entry)
    if not form_data.get("dbizCd"):
        return ""
    return _fetch_text_via_http(
        DETAIL_POPUP_API_URL,
        timeout_ms=timeout_ms,
        headers=headers,
        form_data=form_data,
    )


def _collect_detail_fields_via_http(
    entry: dict[str, str],
    *,
    timeout_ms: int,
    headers: dict[str, str],
) -> dict[str, str]:
    selected: dict[str, str] = {key: "" for key in TARGET_FIELDS}
    popup_html = _fetch_popup_html_via_http(
        entry,
        timeout_ms=timeout_ms,
        headers=headers,
    )
    if popup_html:
        selected = _extract_selected_from_html(popup_html, popup_html)

    # The popup API usually contains the useful detail fields. Keep the page
    # request only as a fallback for rows where the popup response is sparse.
    if _should_open_detail_page(selected):
        detail_url = _build_detail_url(entry)
        request_html = _fetch_detail_html_via_http(
            detail_url,
            timeout_ms=timeout_ms,
            headers=headers,
        )
        if request_html:
            page_selected = _extract_selected_from_html(request_html, request_html)
            selected = _merge_selected_fields(selected, page_selected)

    return selected


def _row_from_entry(row_no: int, entry: dict[str, str], selected: dict[str, str]) -> dict[str, str]:
    return {
        "번호": str(row_no),
        "사업명": entry.get("name", "").strip(),
        "지역": entry.get("region", "").strip(),
        "자치단체": entry.get("municipality", "").strip(),
        **selected,
    }


def _collect_detail_rows_via_http_parallel(
    entries: list[dict[str, str]],
    *,
    start_row_no: int,
    timeout_ms: int,
    headers: dict[str, str],
    concurrency: int,
    stop_requested: Callable[[], bool] | None = None,
) -> list[dict[str, str]]:
    if not entries:
        return []

    safe_workers = max(1, min(int(concurrency), 64, len(entries)))

    def build_row(item: tuple[int, dict[str, str]]) -> dict[str, str]:
        offset, entry = item
        selected = _collect_detail_fields_via_http(
            entry,
            timeout_ms=timeout_ms,
            headers=headers,
        )
        return _row_from_entry(start_row_no + offset, entry, selected)

    if safe_workers <= 1:
        rows: list[dict[str, str]] = []
        for item in enumerate(entries):
            _raise_if_stopped(stop_requested)
            rows.append(build_row(item))
        return rows

    with ThreadPoolExecutor(max_workers=safe_workers) as executor:
        rows = list(executor.map(build_row, enumerate(entries)))
    _raise_if_stopped(stop_requested)
    return rows


def _build_central_url(year: str) -> str:
    params = {
        "pageIndex": "1",
        "pageSize": "5000",
        "totalCnt": "0",
        "acntYr": year,
        "chkoffcNm": "Y",
        "chkacntNm": "Y",
        "chkfldNm": "Y",
        "chksectNm": "Y",
        "chkpgmNm": "Y",
        "chkactvNm": "Y",
    }
    return f"{CENTRAL_LIST_URL}?{urlencode(params)}"


def _wait_central_results(
    page,
    timeout_ms: int = 90_000,
    stop_requested: Callable[[], bool] | None = None,
) -> None:
    waited = 0
    while waited <= timeout_ms:
        _raise_if_stopped(stop_requested)
        try:
            state = page.evaluate(
                """() => {
  const btn = document.getElementById('btn_search');
  const disabled = !!(btn && (btn.disabled || btn.hasAttribute('disabled')));
  const list = Array.isArray(window.fv_dataList) ? window.fv_dataList : null;
  return { ready: !!list && !disabled };
}"""
            )
        except Exception:
            state = {"ready": False}
        if isinstance(state, dict) and bool(state.get("ready")):
            return
        page.wait_for_timeout(300)
        waited += 300
    raise RuntimeError("중앙정부 목록 응답 대기 시간이 초과되었습니다.")


def _extract_central_raw_rows(page) -> list[dict[str, object]]:
    try:
        rows = page.evaluate(
            """() => {
  if (!Array.isArray(window.fv_dataList)) return [];
  return window.fv_dataList
    .filter(row => !!row)
    .map(row => {
      const out = {};
      Object.entries(row || {}).forEach(([key, value]) => {
        if (value === null || value === undefined) return;
        if (typeof value === 'object') {
          try {
            out[key] = JSON.stringify(value);
          } catch (e) {}
          return;
        }
        out[key] = String(value).trim();
      });
      return out;
    });
}"""
        )
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    out: list[dict[str, object]] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(row)
    return out


def _build_central_detail_params(row: dict[str, object]) -> dict[str, str]:
    params: dict[str, str] = {}
    for key, value in row.items():
        name = str(key or "").strip()
        text = str(value or "").strip()
        if not name or not text:
            continue
        if len(text) > 2_000:
            continue
        params[name] = text
    return params


def _fetch_central_detail_html(page, row: dict[str, object], timeout_ms: int) -> str:
    api_request = getattr(page.context, "request", None)
    if api_request is None:
        return ""

    params = _build_central_detail_params(row)
    if not params:
        return ""

    safe_timeout_ms = max(1_000, min(int(timeout_ms), 60_000))
    try:
        response = api_request.get(CENTRAL_DETAIL_URL, params=params, timeout=safe_timeout_ms)
    except Exception:
        return ""

    try:
        ok = bool(getattr(response, "ok", False))
    except Exception:
        ok = False
    if not ok:
        return ""

    try:
        text = str(response.text() or "")
    except Exception:
        return ""
    if "<" not in text:
        return ""
    return text


def _download_central_file_payload(page, spec: DownloadSpec, timeout_ms: int) -> bytes:
    api_request = getattr(page.context, "request", None)
    if api_request is None:
        return b""

    safe_timeout_ms = max(1_000, min(int(timeout_ms), 60_000))
    file_url = f"{CENTRAL_FILES_BASE_URL}/{quote(spec.file_id, safe='')}/{quote(spec.file_seq, safe='')}"
    params = {"name": spec.file_name} if spec.file_name else None
    try:
        response = api_request.get(file_url, params=params, timeout=safe_timeout_ms)
    except Exception:
        return b""

    try:
        ok = bool(getattr(response, "ok", False))
    except Exception:
        ok = False
    if not ok:
        return b""

    try:
        payload = response.body()
    except Exception:
        return b""
    if not payload:
        return b""

    try:
        content_type = (response.header_value("content-type") or "").lower()
    except Exception:
        content_type = ""
    if "text/html" in content_type:
        return b""
    return payload


def _extract_central_detail_fields(page, row: dict[str, object], timeout_ms: int) -> dict[str, str]:
    detail_html = _fetch_central_detail_html(page, row, timeout_ms=timeout_ms)
    if not detail_html:
        return {}

    fields: dict[str, str] = {}
    print(f"[DEBUG] 상세페이지 HTML 길이: {len(detail_html)}자, HWPX 링크: {len(list(extract_download_specs(detail_html, row)))}개")
    print(f"[DEBUG] 상세페이지 HTML 앞 500자: {detail_html[:500]}")

    for spec in extract_download_specs(detail_html, row):
        payload = _download_central_file_payload(page, spec, timeout_ms=timeout_ms)
        if not payload:
            continue
        hwpx_text = extract_hwpx_text(payload)
        if not hwpx_text:
            continue
        business_content = extract_business_content_from_text(hwpx_text)
        if business_content:
            fields["사업내용"] = business_content
            break

    parsed = select_target_fields(parse_policy_detail(detail_html))
    for key, value in parsed.items():
        if value and not fields.get(key):
            fields[key] = value

    script_parsed = parse_policy_detail_from_script(detail_html)
    for key, value in script_parsed.items():
        if value and not fields.get(key):
            fields[key] = value.strip() if isinstance(value, str) else value

    return {k: v for k, v in fields.items() if v}


def _apply_central_search(page, keyword: str, year: str) -> None:
    applied = page.evaluate(
        """({keyword, year}) => {
  const yearSelect = document.getElementById('cond_acntYr');
  if (yearSelect && year) {
    const target = String(year).trim();
    const option = Array.from(yearSelect.options || []).find(opt => String(opt.value || '').trim() === target);
    if (option) yearSelect.value = option.value;
  }

  const keywordInput = document.getElementById('cond_sayNm');
  if (keywordInput) keywordInput.value = String(keyword || '').trim();

  if (typeof fn_search === 'function') {
    fn_search();
    return true;
  }
  return false;
}""",
        {"keyword": keyword, "year": year},
    )
    if not applied:
        raise RuntimeError("중앙정부 검색 함수를 찾지 못했습니다.")


def _crawl_central_source(
    keyword: str,
    limit: int,
    headless: bool,
    slow_mo_ms: int,
    year: str | None,
    detail_timeout_ms: int,
    progress_callback: Callable[[dict[str, object]], None] | None,
    stop_requested: Callable[[], bool] | None,
    row_callback: Callable[[dict[str, str]], None] | None = None,
) -> list[dict[str, str]]:
    safe_limit = max(1, int(limit))
    central_year = validate_central_year(year or "")
    central_url = _build_central_url(central_year)

    def _notify_progress(message: str, percent: int, current: int = 0, total: int = 0) -> None:
        if progress_callback is None:
            return
        payload = {
            "message": str(message),
            "percent": max(0, min(int(percent), 100)),
            "current": max(0, int(current)),
            "total": max(0, int(total)),
        }
        try:
            progress_callback(payload)
        except Exception:
            return

    _notify_progress("중앙정부 검색 페이지를 여는 중입니다.", 5)
    _raise_if_stopped(stop_requested)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
        context = browser.new_context()
        page = context.new_page()
        page.goto(central_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        _raise_if_stopped(stop_requested)

        _notify_progress("중앙정부 검색을 실행합니다.", 18)
        _apply_central_search(page, keyword=keyword, year=central_year)
        _wait_central_results(page, stop_requested=stop_requested)
        _raise_if_stopped(stop_requested)

        _notify_progress("중앙정부 목록을 추출합니다.", 62)
        raw_rows = _extract_central_raw_rows(page)
        if keyword.strip():
            needle = keyword.strip().lower()
            raw_rows = [
                row
                for row in raw_rows
                if needle
                in " ".join(
                    [
                        str(row.get("offcNm", "")),
                        str(row.get("acntNm", "")),
                        str(row.get("fldNm", "")),
                        str(row.get("sectNm", "")),
                        str(row.get("pgmNm", "")),
                        str(row.get("actvNm", "")),
                        str(row.get("sayNm", "")),
                    ]
                ).lower()
            ]

        limited_raw_rows = raw_rows[:safe_limit]
        if limited_raw_rows:
            sample = limited_raw_rows[0]
            print(f"[DEBUG] 열린재정 raw row 키 ({len(sample)}개): {list(sample.keys())}")
            for k, v in sample.items():
                preview = str(v)[:120]
                print(f"  {k}: {preview}")
        rows = map_central_rows(limited_raw_rows, limit=safe_limit)

        total_rows = len(rows)
        if total_rows:
            for row_no, (mapped_row, raw_row) in enumerate(zip(rows, limited_raw_rows), start=1):
                _raise_if_stopped(stop_requested)
                _notify_progress(
                    f"세부사업 상세정보를 수집합니다. ({row_no}/{total_rows})",
                    65 + int((row_no / max(total_rows, 1)) * 30),
                    row_no - 1,
                    total_rows,
                )
                try:
                    detail_fields = _extract_central_detail_fields(
                        page,
                        raw_row,
                        timeout_ms=detail_timeout_ms,
                    )
                except Exception:
                    detail_fields = {}
                for field_name, field_value in detail_fields.items():
                    if field_value:
                        mapped_row[field_name] = field_value
                if row_callback is not None:
                    row_callback(dict(mapped_row))
        context.close()
        browser.close()

    _notify_progress("중앙정부 크롤링이 완료되었습니다.", 98, len(rows), len(rows))
    return [] if row_callback is not None else rows


def crawl(
    keyword: str,
    limit: int,
    headless: bool,
    slow_mo_ms: int,
    source: str = LOCAL_SOURCE,
    year: str | None = None,
    region: str | None = None,
    debug_dir: Path | None = None,
    detail_request_timeout_ms: int = DETAIL_REQUEST_TIMEOUT_MS_DEFAULT,
    detail_runtime_timeout_ms: int = DETAIL_RUNTIME_TIMEOUT_MS_DEFAULT,
    detail_delay_ms: int = DETAIL_DELAY_MS_DEFAULT,
    detail_concurrency: int = DETAIL_CONCURRENCY_DEFAULT,
    detail_batch_size: int = DETAIL_BATCH_SIZE_DEFAULT,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
    stop_requested: Callable[[], bool] | None = None,
    row_callback: Callable[[dict[str, str]], None] | None = None,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    source_name = normalize_source(source)
    safe_detail_request_timeout_ms = max(1_000, min(int(detail_request_timeout_ms), 60_000))
    safe_detail_runtime_timeout_ms = max(200, min(int(detail_runtime_timeout_ms), 5_000))
    safe_detail_delay_sec = max(0, min(int(detail_delay_ms), 3_000)) / 1000.0
    safe_detail_concurrency = max(1, min(int(detail_concurrency), 64))
    safe_detail_batch_size = max(1, min(int(detail_batch_size), MAX_CRAWL_LIMIT))

    def _notify_progress(message: str, percent: int, current: int = 0, total: int = 0) -> None:
        if progress_callback is None:
            return
        payload = {
            "message": str(message),
            "percent": max(0, min(int(percent), 100)),
            "current": max(0, int(current)),
            "total": max(0, int(total)),
        }
        try:
            progress_callback(payload)
        except Exception:
            return

    if source_name == CENTRAL_SOURCE:
        return _crawl_central_source(
            keyword=keyword,
            limit=limit,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
            year=year,
            detail_timeout_ms=safe_detail_request_timeout_ms,
            progress_callback=progress_callback,
            stop_requested=stop_requested,
            row_callback=row_callback,
        )

    _notify_progress("브라우저를 시작합니다.", 2)
    _raise_if_stopped(stop_requested)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
        context = browser.new_context()
        page = context.new_page()
        _notify_progress("검색 페이지를 여는 중입니다.", 6)
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        _raise_if_stopped(stop_requested)
        _notify_progress("검색 조건을 준비합니다.", 12)

        if year:
            _set_filter_value(page, YEAR_FILTER_SELECTORS, year, "연도/날짜")
        if region:
            _set_filter_value(page, REGION_FILTER_SELECTORS, region, "지역")

        search_input = _first_visible(page, SEARCH_INPUT_SELECTORS)
        if search_input is None:
            if debug_dir is not None:
                _write_debug_artifacts(page, debug_dir, "search_input_not_found")
            context.close()
            browser.close()
            raise RuntimeError("사업명 검색 입력창을 찾지 못했습니다. 셀렉터를 확인하세요.")

        search_input.fill(keyword or "")

        search_button = _first_visible(page, SEARCH_BUTTON_SELECTORS)
        if search_button is None:
            if debug_dir is not None:
                _write_debug_artifacts(page, debug_dir, "search_button_not_found")
            context.close()
            browser.close()
            raise RuntimeError("검색 버튼을 찾지 못했습니다. 셀렉터를 확인하세요.")

        list_request_forms: list[dict[str, str]] = []

        def _on_list_request(req) -> None:
            try:
                if req.method != "POST" or req.url != LIST_API_URL:
                    return
                post_data = req.post_data or ""
                if post_data:
                    list_request_forms.append(_parse_post_form_data(post_data))
            except Exception:
                return

        page.on("request", _on_list_request)
        _raise_if_stopped(stop_requested)
        _notify_progress("검색을 실행합니다.", 18)
        try:
            search_button.click()
            page.wait_for_timeout(1500)
        finally:
            try:
                page.remove_listener("request", _on_list_request)
            except Exception:
                pass
        _raise_if_stopped(stop_requested)

        _notify_progress("정책 목록을 수집합니다.", 24)
        headers = _detail_request_headers(page)
        if row_callback is not None:
            total_rows = max(1, int(limit))
            row_no = 1
            saw_entries = False
            _notify_progress(f"상세 정보를 병렬 수집합니다. (0/{total_rows})", 30, 0, total_rows)
            base_form = list_request_forms[-1] if list_request_forms else {}
            if base_form:
                page_batches = _iter_policy_entry_batches_via_http(
                    base_form=base_form,
                    headers=headers,
                    limit=limit,
                    timeout_ms=safe_detail_request_timeout_ms,
                    stop_requested=stop_requested,
                )
            else:
                page_batches = _iter_policy_entry_batches_across_pages(
                    page,
                    limit=limit,
                    stop_requested=stop_requested,
                )
            for entry_batch in _iter_sized_entry_batches(
                page_batches,
                batch_size=safe_detail_batch_size,
                stop_requested=stop_requested,
            ):
                saw_entries = True
                _raise_if_stopped(stop_requested)
                batch_rows = _collect_detail_rows_via_http_parallel(
                    entry_batch,
                    start_row_no=row_no,
                    timeout_ms=safe_detail_request_timeout_ms,
                    headers=headers,
                    concurrency=safe_detail_concurrency,
                    stop_requested=stop_requested,
                )
                for data in batch_rows:
                    row_callback(data)
                    row_no += 1
                    if safe_detail_delay_sec > 0:
                        time.sleep(safe_detail_delay_sec)
                _notify_progress(
                    f"상세 정보를 병렬 수집했습니다. ({row_no - 1}/{total_rows})",
                    30 + int(((row_no - 1) / max(total_rows, 1)) * 60),
                    row_no - 1,
                    total_rows,
                )

            if saw_entries:
                context.close()
                browser.close()
                _notify_progress("크롤링이 완료되었습니다.", 98, row_no - 1, row_no - 1)
                return []
        else:
            entries = _collect_policy_entries_across_pages(
                page,
                limit=limit,
                stop_requested=stop_requested,
            )
            if entries:
                total_rows = min(len(entries), limit)
                _notify_progress(f"상세 정보를 병렬 수집합니다. (0/{total_rows})", 30, 0, total_rows)
                rows = _collect_detail_rows_via_http_parallel(
                    entries[:limit],
                    start_row_no=1,
                    timeout_ms=safe_detail_request_timeout_ms,
                    headers=headers,
                    concurrency=safe_detail_concurrency,
                    stop_requested=stop_requested,
                )
                for row_no, _ in enumerate(rows, start=1):
                    _notify_progress(
                        f"상세 정보를 병렬 수집했습니다. ({row_no}/{total_rows})",
                        30 + int((row_no / max(total_rows, 1)) * 60),
                        row_no,
                        total_rows,
                    )
                    if safe_detail_delay_sec > 0:
                        time.sleep(safe_detail_delay_sec)

                context.close()
                browser.close()
                _notify_progress("크롤링이 완료되었습니다.", 98, len(rows), len(rows))
                return rows

        _raise_if_stopped(stop_requested)
        names = _get_policy_names(page)
        if not names:
            if debug_dir is not None:
                _write_debug_artifacts(page, debug_dir, "policy_list_not_found")
            context.close()
            browser.close()
            raise RuntimeError("정책 목록을 찾지 못했습니다. 목록 셀렉터를 확인하세요.")

        total_rows = min(len(names), limit)
        _notify_progress(f"상세 정보를 수집합니다. (0/{total_rows})", 30, 0, total_rows)
        for row_no, name in enumerate(names[:limit], start=1):
            _raise_if_stopped(stop_requested)
            clicked = False
            target = _find_click_target(page, LIST_ITEM_SELECTORS, name)
            _notify_progress(
                f"상세 정보를 수집합니다. ({row_no}/{total_rows})",
                30 + int(((row_no - 1) / max(total_rows, 1)) * 60),
                row_no - 1,
                total_rows,
            )
            if target is not None:
                popup_page = None
                response_bodies: list[str] = []

                def _on_response(resp):
                    try:
                        ctype = (resp.header_value("content-type") or "").lower()
                        url = resp.url
                        if ("json" not in ctype and "html" not in ctype and "javascript" not in ctype):
                            return
                        if "LF3" not in url and "portal" not in url:
                            return
                        body = resp.text()
                        if ("list3" in body) or ("bizPurpCn" in body) or ("dbizNm" in body):
                            response_bodies.append(body)
                    except Exception:
                        return

                page.context.on("response", _on_response)
                try:
                    with page.expect_popup(timeout=1200) as popup_info:
                        target.click(timeout=3000)
                    popup_page = popup_info.value
                    popup_page.wait_for_load_state("domcontentloaded")
                    clicked = True
                except PlaywrightTimeoutError:
                    try:
                        target.click(timeout=3000)
                        clicked = True
                    except PlaywrightTimeoutError:
                        pass
                finally:
                    try:
                        page.context.remove_listener("response", _on_response)
                    except Exception:
                        pass

            if not clicked:
                continue

            page.wait_for_timeout(1200)

            detail_html = ""
            script_runtime_selected: dict[str, str] = {}
            selected: dict[str, str] = {key: "" for key in TARGET_FIELDS}

            if popup_page is not None:
                popup_page.wait_for_timeout(800)
                detail_html = popup_page.content()
                full_html = detail_html
                selected = _extract_selected_from_html(detail_html, full_html)
                script_runtime_selected = _wait_runtime_script_selected(popup_page, timeout_ms=2000)
                if any(script_runtime_selected.get(k) for k in TARGET_FIELDS):
                    for key in TARGET_FIELDS:
                        if not selected.get(key):
                            selected[key] = script_runtime_selected.get(key, "")
                try:
                    popup_page.close()
                except Exception:
                    pass
            else:
                panel = _first_visible(page, DETAIL_PANEL_SELECTORS)
                if panel is not None:
                    detail_html = panel.inner_html()
                full_html = page.content()
                selected = _extract_selected_from_html(detail_html, full_html)
                script_runtime_selected = _wait_runtime_script_selected(page, timeout_ms=2000)
                if any(script_runtime_selected.get(k) for k in TARGET_FIELDS):
                    for key in TARGET_FIELDS:
                        if not selected.get(key):
                            selected[key] = script_runtime_selected.get(key, "")

            # Final fallback: parse captured network responses from detail request.
            response_selected = _extract_selected_from_responses(response_bodies)
            for key in TARGET_FIELDS:
                if not selected.get(key):
                    selected[key] = response_selected.get(key, "")

            data = {
                "번호": str(row_no),
                "사업명": name,
                "지역": region or "",
                "자치단체": "",
                **selected,
            }
            if row_callback is not None:
                row_callback(data)
            else:
                rows.append(data)
            _notify_progress(
                f"상세 정보를 수집했습니다. ({row_no}/{total_rows})",
                30 + int((row_no / max(total_rows, 1)) * 60),
                row_no,
                total_rows,
            )

            if debug_dir is not None and not any(selected.get(key) for key in TARGET_FIELDS):
                _write_debug_artifacts(page, debug_dir, f"empty_detail_{row_no}_{name}")

            close_btn = _first_visible(page, DETAIL_CLOSE_SELECTORS)
            if close_btn is not None:
                try:
                    close_btn.click(timeout=1500)
                    page.wait_for_timeout(400)
                except PlaywrightTimeoutError:
                    pass

            # Be polite to the target service.
            time.sleep(0.3)
            _raise_if_stopped(stop_requested)

        context.close()
        browser.close()

    _notify_progress("크롤링이 완료되었습니다.", 98, len(rows), len(rows))
    return [] if row_callback is not None else rows


def _infer_output_format(output: str, requested: str) -> str:
    if requested in {"md", "xlsx", "both"}:
        return requested
    suffix = Path(output).suffix.lower()
    if suffix == ".md":
        return "md"
    return "xlsx"


def _normalize_cli_output_path(output: str, output_format: str) -> Path:
    path = Path(output)
    suffix = ".xlsx" if output_format == "xlsx" else ".md"
    if path.suffix.lower() not in {".xlsx", ".md"}:
        return path.with_suffix(suffix)
    if output_format == "xlsx" and path.suffix.lower() != ".xlsx":
        return path.with_suffix(".xlsx")
    if output_format in {"md", "both"} and path.suffix.lower() != ".md":
        return path.with_suffix(".md")
    return path


def _chunked_output_enabled(limit: int, chunk_size: int) -> bool:
    return chunk_size > 0 and int(limit) > int(chunk_size)


def _clamp_cli_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_CRAWL_LIMIT))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Lofin365 정책 목록/상세 크롤링 후 Markdown/XLSX/JSON 저장")
    parser.add_argument("--keyword", default="", help="사업명 검색어. 비워두면 연도/지역 조건 기준으로 전체 조회")
    parser.add_argument(
        "--source",
        choices=SUPPORTED_SOURCES,
        default=LOCAL_SOURCE,
        help="데이터 소스(local|central)",
    )
    parser.add_argument("--year", default="", help="연도/날짜 필터 값 (예: 2026)")
    parser.add_argument("--region", default="", help="지역 필터 값 (예: 서울특별시)")
    parser.add_argument("--limit", type=int, default=30, help="수집할 최대 정책 개수")
    parser.add_argument("--chunk-size", type=int, default=10000, help="파트 파일당 저장할 정책 개수. 0이면 단일 파일 저장")
    parser.add_argument("--output", default="lofin_policies.md", help="출력 파일 경로(.md 또는 .xlsx)")
    parser.add_argument(
        "--format",
        choices=["auto", "md", "xlsx", "both"],
        default="auto",
        help="결과 파일 형식. auto는 --output 확장자를 따름",
    )
    parser.add_argument("--json-output", default="", help="검색 앱용 JSON 색인 출력 경로. 비우면 결과 파일과 같은 이름으로 저장")
    parser.add_argument("--headed", action="store_true", help="브라우저 창을 띄워서 실행")
    parser.add_argument("--slow-mo", type=int, default=0, help="동작 사이 지연(ms)")
    parser.add_argument("--debug-dir", default="debug", help="실패 시 스크린샷/HTML 저장 폴더")
    parser.add_argument(
        "--detail-request-timeout-ms",
        type=int,
        default=DETAIL_REQUEST_TIMEOUT_MS_DEFAULT,
        help="상세 페이지 HTTP 요청 타임아웃(ms)",
    )
    parser.add_argument(
        "--detail-runtime-timeout-ms",
        type=int,
        default=DETAIL_RUNTIME_TIMEOUT_MS_DEFAULT,
        help="상세 런타임 스크립트 fallback 대기(ms)",
    )
    parser.add_argument(
        "--detail-delay-ms",
        type=int,
        default=DETAIL_DELAY_MS_DEFAULT,
        help="상세 건별 지연(ms)",
    )
    parser.add_argument(
        "--detail-concurrency",
        type=int,
        default=DETAIL_CONCURRENCY_DEFAULT,
        help="상세 HTTP 요청 병렬 처리 수",
    )
    parser.add_argument(
        "--detail-batch-size",
        type=int,
        default=DETAIL_BATCH_SIZE_DEFAULT,
        help="상세 병렬 수집 전에 모을 목록 건수",
    )
    args = parser.parse_args(argv)
    debug_dir = Path(args.debug_dir) if args.debug_dir else None
    args.limit = _clamp_cli_limit(args.limit)
    chunk_size = max(0, min(int(args.chunk_size), MAX_CRAWL_LIMIT))
    output_format = _infer_output_format(args.output, args.format)
    output_path = with_timestamp_suffix(_normalize_cli_output_path(args.output, output_format))
    region_filter = normalize_region_filter(args.region)

    if _chunked_output_enabled(args.limit, chunk_size):
        writer = ChunkedPolicyWriter(
            output_path=output_path,
            output_format=output_format,
            chunk_size=chunk_size,
            source=args.source,
            source_url=URL if args.source == LOCAL_SOURCE else CENTRAL_LIST_URL,
            default_year=args.year or "",
            default_region=region_filter,
        )
        try:
            crawl(
                keyword=args.keyword,
                limit=args.limit,
                headless=not args.headed,
                slow_mo_ms=args.slow_mo,
                source=args.source,
                year=args.year or None,
                region=region_filter or None,
                debug_dir=debug_dir,
                detail_request_timeout_ms=args.detail_request_timeout_ms,
                detail_runtime_timeout_ms=args.detail_runtime_timeout_ms,
                detail_delay_ms=args.detail_delay_ms,
                detail_concurrency=args.detail_concurrency,
                detail_batch_size=args.detail_batch_size,
                row_callback=writer.add_row,
            )
        except Exception:
            writer.close(complete=False)
            raise

        result = writer.close(complete=True)
        if result.count == 0:
            print("수집 결과가 없습니다.")
            return 1
        print(f"파트 저장: {result.count}건 / {result.chunks}개 파트 -> {result.manifest_file}")
        print(f"검색 색인 저장: {result.indexed}건 (스킵 {result.index_skipped}건)")
        return 0

    rows = crawl(
        keyword=args.keyword,
        limit=args.limit,
        headless=not args.headed,
        slow_mo_ms=args.slow_mo,
        source=args.source,
        year=args.year or None,
        region=region_filter or None,
        debug_dir=debug_dir,
        detail_request_timeout_ms=args.detail_request_timeout_ms,
        detail_runtime_timeout_ms=args.detail_runtime_timeout_ms,
        detail_delay_ms=args.detail_delay_ms,
        detail_concurrency=args.detail_concurrency,
        detail_batch_size=args.detail_batch_size,
    )

    if not rows:
        print("수집 결과가 없습니다.")
        return 1

    md_path = output_path.with_suffix(".md") if output_format in {"md", "both"} else None
    xlsx_path = output_path.with_suffix(".xlsx") if output_format in {"xlsx", "both"} else None

    if md_path is not None:
        export_to_markdown(rows, md_path)
        print(f"Markdown 저장: {len(rows)}건 -> {md_path}")
    if xlsx_path is not None:
        export_to_excel(rows, xlsx_path)
        print(f"XLSX 저장: {len(rows)}건 -> {xlsx_path}")

    primary_path = md_path or xlsx_path
    if primary_path is None:
        print("출력 형식이 올바르지 않습니다.")
        return 1

    json_output_path = Path(args.json_output) if args.json_output else primary_path.with_suffix(".json")
    index_result = export_policy_index(
        rows,
        json_output_path,
        source=args.source,
        source_url=URL if args.source == LOCAL_SOURCE else CENTRAL_LIST_URL,
        default_year=args.year or "",
        default_region=region_filter,
        source_file=primary_path.name,
        xlsx_file=xlsx_path.name if xlsx_path is not None else "",
    )
    print(f"검색 색인 저장: {index_result.indexed}건 -> {json_output_path} (스킵 {index_result.skipped}건)")

    return 0


def with_timestamp_suffix(path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = path.suffix or ".xlsx"
    stem = path.stem if path.suffix else path.name
    return path.with_name(f"{stem}_{timestamp}{suffix}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
