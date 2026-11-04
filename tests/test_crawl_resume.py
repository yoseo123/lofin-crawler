"""Regression tests use only synthetic rows and temporary local files."""

import contextlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import crawl_lofin as cli
from src.chunked_writer import ChunkedPolicyWriter
from src.resume_state import central_entry_key, local_entry_key, atomic_write

ROOT = Path(__file__).resolve().parents[1]


def row(n=1, **extra):
    return {
        "사업명": f"사업{n}",
        "지역": "서울",
        "자치단체": "마포",
        "회계연도": "2026",
        **extra,
    }


def writer(path, **kw):
    options = dict(
        output_path=path,
        output_format="both",
        chunk_size=2,
        source="local",
        default_year="2026",
        default_region="서울",
        search_conditions={"keyword": "", "limit": 20},
    )
    options.update(kw)
    return ChunkedPolicyWriter(**options)


class WriterRecoveryTests(unittest.TestCase):
    def test_manifest_is_created_before_first_row_and_updated_after_each_chunk(self):
        with tempfile.TemporaryDirectory() as td:
            w = writer(Path(td) / "out.md")
            self.assertFalse(json.loads(w.manifest_path.read_text())["complete"])
            w.add_row(row(1))
            w.add_row(row(2))
            manifest = json.loads(w.manifest_path.read_text())
            self.assertEqual(manifest["total_count"], 2)
            self.assertEqual(len(manifest["chunks"]), 1)
            self.assertEqual(
                set(manifest["chunks"][0]["sha256"]),
                {"md_file", "xlsx_file", "json_file"},
            )
            w.close()

    def test_replays_pending_rows_and_deduplicates_across_chunks_and_restarts(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "out.md"
            w = writer(path)
            for n in range(1, 4):
                w.add_row(row(n))
            w.release()  # Deliberately no close/flush: row 3 only exists in SQLite.
            w = writer(path, resume=True)
            self.assertEqual(w.count, 3)
            for n in range(1, 6):
                w.add_row(row(n, **{"번호": "999"}))
            result = w.close()
            self.assertEqual(result.count, 5)
            raw = []
            for file in result.json_files:
                raw.extend(
                    p["raw"]
                    for p in json.loads((Path(td) / file).read_text())["policies"]
                )
            self.assertEqual([r["번호"] for r in raw], ["1", "2", "3", "4", "5"])

    def test_all_formats_repair_truncated_missing_or_parseable_modified_outputs(self):
        for fmt in ("md", "xlsx", "both"):
            for damage in ("missing", "truncated", "changed"):
                with self.subTest(
                    fmt=fmt, damage=damage
                ), tempfile.TemporaryDirectory() as td:
                    path = Path(td) / "out.md"
                    w = writer(path, output_format=fmt)
                    w.add_row(row(1))
                    w.add_row(row(2))
                    w.close()
                    manifest = json.loads(w.manifest_path.read_text())
                    for key, file in manifest["chunks"][0].items():
                        if key.endswith("_file") and file:
                            target = Path(td) / file
                            if damage == "missing":
                                target.unlink()
                            elif damage == "truncated":
                                target.write_bytes(b"{")
                            else:
                                target.write_text('{"policies":[],"count":0}')
                    w.manifest_path.write_text("{")
                    w = writer(path, output_format=fmt, resume=True)
                    self.assertTrue(w._chunk_valid(w._chunks[0]))
                    self.assertEqual(w.close().count, 2)
                    self.assertEqual(
                        json.loads((Path(td) / "out_part001.json").read_text())[
                            "count"
                        ],
                        2,
                    )

    def test_missing_manifest_is_regenerated_and_orphan_temp_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "out.md"
            w = writer(path)
            w.add_row(row())
            w.release()
            w.manifest_path.unlink()
            (Path(td) / ".out_part001.json.interrupted.tmp").write_text("bad")
            w = writer(path, resume=True)
            self.assertEqual(w.close().count, 1)

    def test_original_exception_and_export_failure_keep_durable_row(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "out.md"
            w = writer(path, chunk_size=1)
            with patch(
                "src.chunked_writer.export_to_excel", side_effect=OSError("disk full")
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    w.add_row(row())
            self.assertEqual(w.count, 1)
            self.assertEqual(w.chunk_count, 0)
            w.release()
            w = writer(path, chunk_size=1, resume=True)
            self.assertEqual(w.close().count, 1)

    def test_atomic_failure_keeps_old_manifest_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "manifest.json"
            path.write_text("old")
            with patch("src.resume_state.os.replace", side_effect=OSError("failed")):
                with self.assertRaises(OSError):
                    atomic_write(path, lambda p: p.write_text("new"))
            self.assertEqual(path.read_text(), "old")
            self.assertEqual(list(Path(td).iterdir()), [path])

    def test_existing_files_and_legacy_manifest_never_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "out.md"
            legacy = Path(td) / "out_manifest.json"
            legacy.write_text('{"total_count":10}')
            for resume in (False, True):
                with self.assertRaises(ValueError):
                    writer(path, resume=resume)
            self.assertEqual(legacy.read_text(), '{"total_count":10}')

    def test_conditions_mismatch_before_modifying_outputs_or_ledger(self):
        for changes in (
            {"source": "central"},
            {"default_year": "2027"},
            {"default_region": "경기"},
            {"chunk_size": 3},
            {"output_format": "md"},
            {"search_conditions": {"keyword": "다름", "limit": 20}},
            {"search_conditions": {"keyword": "", "limit": 30}},
        ):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as td:
                path = Path(td) / "out.md"
                w = writer(path)
                w.add_row(row())
                w.close(complete=False)
                before = {p.name: p.read_bytes() for p in Path(td).iterdir()}
                with self.assertRaisesRegex(ValueError, "조건/출력"):
                    writer(path, resume=True, **changes)
                self.assertEqual(
                    before, {p.name: p.read_bytes() for p in Path(td).iterdir()}
                )
                writer(path, resume=True).close()

    def test_indexer_change_rejects_mixing_search_id_contracts(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "out.md"
            w = writer(path)
            w.add_row(row())
            w.close(complete=False)
            changed_indexer = Path(td) / "changed_indexer.py"
            changed_indexer.write_text("# different search ID implementation")
            before = w.manifest_path.read_bytes()
            with patch(
                "src.chunked_writer.policy_index.__file__", str(changed_indexer)
            ):
                with self.assertRaisesRegex(ValueError, "조건/출력"):
                    writer(path, resume=True)
            self.assertEqual(w.manifest_path.read_bytes(), before)
            writer(path, resume=True).close()

    def test_effective_site_filters_must_match(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "out.md"
            w = writer(path)
            w.bind_conditions({"inqYr": "2026", "inqCap": ""})
            w.add_row(row())
            w.close(complete=False)
            w = writer(path, resume=True)
            with self.assertRaisesRegex(ValueError, "실제 검색 조건"):
                w.bind_conditions({"inqYr": "2027", "inqCap": ""})
            w.close(complete=False)

    def test_ledger_damage_fails_closed_without_deleting_or_resetting(self):
        for damage in (
            "not sqlite",
            "row digest",
            "sequence",
            "chunk range",
            "empty chunk",
        ):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as td:
                path = Path(td) / "out.md"
                w = writer(path)
                w.add_row(row())
                w.close(complete=False)
                if damage == "not sqlite":
                    w.state_path.write_bytes(b"truncated")
                else:
                    with sqlite3.connect(w.state_path) as db:
                        if damage == "row digest":
                            db.execute("UPDATE rows SET payload='{}'")
                        if damage == "sequence":
                            db.execute("UPDATE rows SET seq=4")
                        if damage == "chunk range":
                            db.execute(
                                "UPDATE chunks SET payload=?",
                                (
                                    json.dumps(
                                        {
                                            "part": 1,
                                            "first_row": 2,
                                            "last_row": 2,
                                            "count": 1,
                                        }
                                    ),
                                ),
                            )
                    if damage == "empty chunk":
                        with sqlite3.connect(w.state_path) as db:
                            record = json.loads(
                                db.execute("SELECT payload FROM chunks").fetchone()[0]
                            )
                            record.update(count=0, last_row=0)
                            db.execute(
                                "UPDATE chunks SET payload=?", (json.dumps(record),)
                            )
                before = {p.name: p.read_bytes() for p in Path(td).iterdir()}
                with self.assertRaises((ValueError, sqlite3.DatabaseError)):
                    writer(path, resume=True)
                self.assertEqual(
                    before, {p.name: p.read_bytes() for p in Path(td).iterdir()}
                )

    def test_lock_rejects_second_writer_and_releases_on_close(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "out.md"
            w = writer(path)
            with self.assertRaisesRegex(ValueError, "실행 중"):
                writer(path, resume=True)
            w.close()
            writer(path, resume=True).close()

    def test_explicit_keys_preserve_different_same_name_businesses(self):
        with tempfile.TemporaryDirectory() as td:
            w = writer(Path(td) / "out.md", source="central")
            a = {"acntYr": "2026", "offcNm": "부처A", "sayNm": "같은사업"}
            b = {**a, "offcNm": "부처B"}
            for raw in (a, b, a):
                w.add_row(row(1, 소관명=raw["offcNm"]), key=central_entry_key(raw))
            self.assertEqual(w.close().count, 2)

    def test_limit_remains_total_cap_including_previously_saved_rows(self):
        with tempfile.TemporaryDirectory() as td:
            w = writer(Path(td) / "out.md", search_conditions={"limit": 3})
            for n in range(10):
                w.add_row(row(n))
            self.assertTrue(w.at_limit)
            self.assertEqual(w.close().count, 3)

    @unittest.skipUnless(os.name == "posix", "SIGKILL requires POSIX")
    def test_sigkill_at_publication_boundaries_recovers_without_loss(self):
        # Blocking child announces the boundary, parent sends actual SIGKILL.
        script = r"""
import sys
from pathlib import Path
from src.chunked_writer import ChunkedPolicyWriter
import src.chunked_writer as module
phase, out = sys.argv[1:]
w = ChunkedPolicyWriter(output_path=Path(out),output_format="both",chunk_size=2,source="local",default_year="2026")
def ready():
    print("READY", flush=True)
    sys.stdin.readline()
if phase in {"buffer", "row-transaction"}:
    w.add_row({"사업명":"사업1","회계연도":"2026"})
    if phase == "row-transaction":
        from src.resume_state import canonical, fingerprint
        payload = canonical({"사업명":"사업2", "회계연도":"2026", "번호":"2"})
        w._state.db.execute("BEGIN IMMEDIATE")
        w._state.db.execute("INSERT INTO rows VALUES (?,?,?,?)", (2,"uncommitted",payload,fingerprint(["uncommitted",payload])))
    ready()
else:
    original = module.atomic_write
    def hooked(path, write):
        if phase == "before-replace" and path.suffix == ".md":
            def paused(temp):
                write(temp)
                ready()
            return original(path, paused)
        original(path, write)
        if ((phase == "md" and path.suffix == ".md") or
            (phase == "xlsx" and path.suffix == ".xlsx") or
            (phase == "json" and path.name.endswith("part001.json")) or
            (phase == "manifest" and path.name.endswith("manifest.json"))):
            ready()
    module.atomic_write = hooked
    if phase == "db-commit":
        original_save = w._state.save_chunk
        def save(chunk):
            original_save(chunk)
            ready()
        w._state.save_chunk = save
    w.add_row({"사업명":"사업1","회계연도":"2026"})
    w.add_row({"사업명":"사업2","회계연도":"2026"})
"""
        for phase in (
            "buffer",
            "row-transaction",
            "before-replace",
            "md",
            "xlsx",
            "json",
            "db-commit",
            "manifest",
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as td:
                path = Path(td) / "out.md"
                proc = subprocess.Popen(
                    [sys.executable, "-u", "-c", script, phase, str(path)],
                    cwd=ROOT,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    import selectors

                    with selectors.DefaultSelector() as selector:
                        selector.register(proc.stdout, selectors.EVENT_READ)
                        self.assertTrue(
                            selector.select(timeout=15),
                            "child did not reach crash boundary",
                        )
                    self.assertEqual(proc.stdout.readline().strip(), "READY")
                    proc.kill()
                    proc.wait(timeout=10)
                    self.assertEqual(proc.returncode, -signal.SIGKILL)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.communicate(timeout=10)
                w = ChunkedPolicyWriter(
                    output_path=path,
                    output_format="both",
                    chunk_size=2,
                    source="local",
                    default_year="2026",
                    resume=True,
                )
                expected = 1 if phase in {"buffer", "row-transaction"} else 2
                self.assertEqual(w.count, expected)
                for n in range(1, 4):
                    w.add_row({"사업명": f"사업{n}", "회계연도": "2026"})
                result = w.close()
                self.assertEqual(result.count, 3)
                self.assertTrue(json.loads(w.manifest_path.read_text())["complete"])


class CrawlResumeIntegrationTests(unittest.TestCase):
    def invoke(self, argv):
        with contextlib.redirect_stdout(io.StringIO()):
            return cli.main(argv)

    def test_cli_normal_exception_keyboard_interrupt_and_resume_without_timestamp(self):
        for failure in (RuntimeError("interrupted"), KeyboardInterrupt()):
            with self.subTest(
                failure=type(failure).__name__
            ), tempfile.TemporaryDirectory() as td:
                path = Path(td) / "fixed.md"
                base = [
                    "--year",
                    "2026",
                    "--limit",
                    "5",
                    "--chunk-size",
                    "2",
                    "--output",
                    str(path),
                ]

                def interrupted(**kw):
                    kw["checkpoint"].add_row(row(1), key="one")
                    raise failure

                with patch.object(
                    cli, "with_timestamp_suffix", return_value=path
                ), patch.object(cli, "crawl", side_effect=interrupted):
                    with self.assertRaises(type(failure)):
                        self.invoke(base)
                self.assertFalse(
                    json.loads((Path(td) / "fixed_manifest.json").read_text())[
                        "complete"
                    ]
                )

                def resumed(**kw):
                    self.assertTrue(kw["checkpoint"].contains("one"))
                    kw["checkpoint"].add_row(row(1), key="one")
                    kw["checkpoint"].add_row(row(2), key="two")

                with patch.object(
                    cli, "with_timestamp_suffix", side_effect=AssertionError("new run")
                ), patch.object(cli, "crawl", side_effect=resumed):
                    self.assertEqual(self.invoke(base + ["--resume"]), 0)
                self.assertEqual(
                    json.loads((Path(td) / "fixed_manifest.json").read_text())[
                        "total_count"
                    ],
                    2,
                )

    def test_cli_preserves_original_exception_if_cleanup_export_also_fails(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "fixed.md"

            def interrupted(**kw):
                kw["checkpoint"].add_row(row(1))
                raise RuntimeError("original crawl error")

            with patch.object(
                cli, "with_timestamp_suffix", return_value=path
            ), patch.object(cli, "crawl", side_effect=interrupted), patch(
                "src.chunked_writer.export_to_excel", side_effect=OSError("disk full")
            ), contextlib.redirect_stderr(
                io.StringIO()
            ):
                with self.assertRaisesRegex(RuntimeError, "original crawl error"):
                    self.invoke(
                        [
                            "--limit",
                            "5",
                            "--chunk-size",
                            "2",
                            "--format",
                            "both",
                            "--output",
                            str(path),
                        ]
                    )
            # The failed close released the OS lock and left the row recoverable.
            w = ChunkedPolicyWriter(
                output_path=path,
                output_format="both",
                chunk_size=2,
                source="local",
                source_url=cli.URL,
                search_conditions={
                    "keyword": "",
                    "limit": 5,
                    "detail_request_timeout_ms": cli.DETAIL_REQUEST_TIMEOUT_MS_DEFAULT,
                    "detail_runtime_timeout_ms": cli.DETAIL_RUNTIME_TIMEOUT_MS_DEFAULT,
                },
                resume=True,
            )
            self.assertEqual(w.close().count, 1)

    def test_cli_search_mismatch_fails_before_crawl_and_resume_single_mode_is_rejected(
        self,
    ):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "fixed.md"
            args = ["--limit", "5", "--chunk-size", "2", "--output", str(path)]
            with patch.object(
                cli, "with_timestamp_suffix", return_value=path
            ), patch.object(cli, "crawl"):
                self.invoke(args)
            with patch.object(
                cli, "crawl", side_effect=AssertionError("must not call site")
            ):
                with self.assertRaisesRegex(ValueError, "조건/출력"):
                    self.invoke(args + ["--resume", "--keyword", "other"])
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(
                SystemExit
            ):
                self.invoke(["--resume", "--chunk-size", "0"])

    def test_single_file_cli_retains_existing_path_and_call_contract(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "fixed.xlsx"
            with patch.object(
                cli, "with_timestamp_suffix", return_value=path
            ), patch.object(cli, "crawl", return_value=[row()]) as crawl:
                self.assertEqual(self.invoke(["--output", str(path)]), 0)
            self.assertNotIn("checkpoint", crawl.call_args.kwargs)
            self.assertTrue(path.exists())
            self.assertTrue(path.with_suffix(".json").exists())

    def test_central_reordered_list_skips_saved_detail_and_keeps_same_name_other_ministry(
        self,
    ):
        raw_a = {"acntYr": "2026", "offcNm": "부처A", "sayNm": "같은사업"}
        raw_b = {**raw_a, "offcNm": "부처B"}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "out.md"
            w = writer(path, source="central")
            w.bind_conditions({"source": "central", "year": "2026", "keyword": ""})
            w.add_row(row(1), key=central_entry_key(raw_a))
            w.close(complete=False)
            w = writer(path, source="central", resume=True)
            with patch.object(
                cli, "sync_playwright", return_value=MagicMock()
            ), patch.object(cli, "_apply_central_search"), patch.object(
                cli, "_wait_central_results"
            ), patch.object(
                cli, "_extract_central_raw_rows", return_value=[raw_b, raw_a, raw_b]
            ), patch.object(
                cli, "_extract_central_detail_fields", return_value={"사업내용": "내용"}
            ) as detail, contextlib.redirect_stdout(
                io.StringIO()
            ):
                cli.crawl(
                    "",
                    20,
                    True,
                    0,
                    source="central",
                    year="2026",
                    row_callback=w.add_row,
                    checkpoint=w,
                )
            self.assertEqual(detail.call_count, 1)
            self.assertEqual(detail.call_args.args[1], raw_b)
            self.assertEqual(w.close().count, 2)

    def test_local_browser_and_http_paths_skip_saved_before_detail(self):
        a = {
            "name": "같은사업",
            "region": "서울",
            "municipality": "마포",
            "dbizCd": "A",
            "lafCd": "1",
            "fyr": "2026",
            "inqYmd": "20260101",
        }
        b = {**a, "lafCd": "2", "municipality": "은평"}
        for use_http in (False, True):
            with self.subTest(http=use_http), tempfile.TemporaryDirectory() as td:
                path = Path(td) / "out.md"
                w = writer(path)
                if use_http:
                    effective = {
                        "mode": "http",
                        "form": {"inqYr": "2026", "inqYmd": "20260101"},
                    }
                else:
                    effective = {
                        "mode": "dom",
                        "year": "2026",
                        "region": "서울",
                        "keyword": "",
                    }
                w.bind_conditions(effective)
                w.add_row(row(1), key=local_entry_key(a))
                w.close(complete=False)
                w = writer(path, resume=True)
                manager = MagicMock()
                page = (
                    manager.__enter__.return_value.chromium.launch.return_value.new_context.return_value.new_page.return_value
                )
                if use_http:

                    def capture(event, fn):
                        if event == "request":
                            request = MagicMock(
                                method="POST",
                                url=cli.LIST_API_URL,
                                post_data="curPage=1&inqYr=2026&inqYmd=20260101",
                            )
                            fn(request)

                    page.on.side_effect = capture

                def collect(entries, **kw):
                    self.assertEqual(entries, [b])
                    return [row(2)]

                with patch.object(
                    cli, "sync_playwright", return_value=manager
                ), patch.object(
                    cli, "_first_visible", return_value=MagicMock()
                ), patch.object(
                    cli, "_set_filter_value", return_value=True
                ), patch.object(
                    cli, "_detail_request_headers", return_value={}
                ), patch.object(
                    cli,
                    "_iter_policy_entry_batches_across_pages",
                    return_value=iter([[b, a]]),
                ), patch.object(
                    cli,
                    "_iter_policy_entry_batches_via_http",
                    return_value=iter([[b, a]]),
                ), patch.object(
                    cli, "_collect_detail_rows_via_http_parallel", side_effect=collect
                ) as details:
                    cli.crawl(
                        "",
                        20,
                        True,
                        0,
                        year="2026",
                        region="서울",
                        detail_delay_ms=0,
                        row_callback=w.add_row,
                        checkpoint=w,
                    )
                self.assertEqual(details.call_count, 1)
                self.assertEqual(w.close().count, 2)

    def test_local_changed_actual_form_aborts_before_detail_requests(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "out.md"
            w = writer(path)
            w.bind_conditions({"mode": "http", "form": {"inqYr": "2026"}})
            w.add_row(row())
            w.close(complete=False)
            w = writer(path, resume=True)
            manager = MagicMock()
            page = (
                manager.__enter__.return_value.chromium.launch.return_value.new_context.return_value.new_page.return_value
            )

            def capture(event, fn):
                if event == "request":
                    fn(
                        MagicMock(
                            method="POST",
                            url=cli.LIST_API_URL,
                            post_data="curPage=1&inqYr=2027",
                        )
                    )

            page.on.side_effect = capture
            with patch.object(
                cli, "sync_playwright", return_value=manager
            ), patch.object(
                cli, "_first_visible", return_value=MagicMock()
            ), patch.object(
                cli, "_set_filter_value", return_value=True
            ), patch.object(
                cli, "_detail_request_headers", return_value={}
            ), patch.object(
                cli,
                "_collect_detail_rows_via_http_parallel",
                side_effect=AssertionError("must not fetch details"),
            ):
                with self.assertRaisesRegex(ValueError, "실제 검색 조건"):
                    cli.crawl("", 20, True, 0, year="2026", region="서울", checkpoint=w)
            self.assertEqual(w.count, 1)
            w.close(complete=False)

    def test_entry_keys_preserve_local_codes_year_and_central_hierarchy(self):
        a = {
            "name": "사업",
            "lafCd": "1",
            "dbizCd": "A",
            "fyr": "2026",
            "inqYmd": "20260101",
        }
        for changes in (
            {"lafCd": "2"},
            {"dbizCd": "B"},
            {"fyr": "2027"},
            {"inqYmd": "20260201"},
        ):
            self.assertNotEqual(local_entry_key(a), local_entry_key({**a, **changes}))
        self.assertEqual(
            local_entry_key(a), local_entry_key(dict(reversed(list(a.items()))))
        )
        a = {
            "acntYr": "2026",
            "offcNm": "A",
            "acntNm": "B",
            "pgmNm": "C",
            "actvNm": "D",
            "sayNm": "E",
        }
        for field in a:
            self.assertNotEqual(
                central_entry_key(a), central_entry_key({**a, field: "other"})
            )
        self.assertEqual(
            central_entry_key(a),
            central_entry_key({**a, "thyCfmtnMediAmt": "999", "rowNum": "999"}),
        )
        incomplete = {"acntYr": "2026", "sayNm": "사업", "offcNm": "A"}
        self.assertEqual(
            central_entry_key(incomplete),
            central_entry_key({**incomplete, "rowNum": "2"}),
        )
        self.assertNotEqual(
            central_entry_key(incomplete),
            central_entry_key({**incomplete, "offcNm": "B"}),
        )


if __name__ == "__main__":
    unittest.main()
