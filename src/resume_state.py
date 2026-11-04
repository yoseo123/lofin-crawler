"""Durable raw rows, independent of search-index policy IDs (including PR #1)."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unicodedata


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def local_entry_key(entry: dict) -> str:
    # Official codes avoid conflating different municipalities' same-name projects.
    if entry.get("dbizCd") and entry.get("lafCd"):
        fields = ("dbizCd", "lafCd", "fyr", "inqYmd")
    else:
        fields = ("name", "region", "municipality", "fyr", "inqYmd", "dbizCd", "lafCd")
    return fingerprint(["local-entry-v1", {k: str(entry.get(k, "")) for k in fields}])


def central_entry_key(raw: dict) -> str:
    fields = ("acntYr", "offcNm", "acntNm", "pgmNm", "actvNm", "sayNm")
    hierarchy = {
        k: " ".join(unicodedata.normalize("NFC", str(raw.get(k) or "")).split())
        for k in fields
    }
    if all(hierarchy.values()):
        return fingerprint(["central-entry-v1", hierarchy])
    # Incomplete hierarchy: conserve evidence rather than merge by title alone.
    evidence = {
        k: v for k, v in raw.items() if k not in {"번호", "rowNum", "rnum", "rn"}
    }
    return fingerprint(["central-entry-fallback-v1", evidence])


def fsync_directory(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_write(path: Path, write) -> None:
    """Publish a fully written sibling; an interrupted export keeps the old file."""
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temp = Path(name)
    try:
        write(temp)
        with temp.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


class RunLock:
    """OS-owned lock; SIGKILL releases it without stale PID guessing."""

    def __init__(self, path: Path):
        self.stream = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if path.stat().st_size == 0:
                    self.stream.write(b"0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise ValueError("같은 출력의 수집/복구가 이미 실행 중입니다.") from exc

    def close(self):
        if not self.stream.closed:
            if os.name == "nt":
                import msvcrt

                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            self.stream.close()


class ResumeState:
    def __init__(self, path: Path, config: dict, *, resume: bool):
        self.path = path
        self.lock = RunLock(path.with_suffix(path.suffix + ".lock"))
        self.db = None
        try:
            if resume and not path.exists():
                raise ValueError(
                    "재개 기록이 없습니다. 구버전 결과는 자동 변환하지 않습니다."
                )
            if not resume and path.exists():
                raise ValueError(
                    "기존 수집 기록이 있습니다. --resume 또는 다른 출력 경로를 사용하세요."
                )
            self.db = sqlite3.connect(str(path))
            if resume:
                if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError(
                        "수집 기록이 손상되었습니다. 원본을 보존하고 복구를 중단합니다."
                    )
                record = self.db.execute(
                    "SELECT value FROM meta WHERE key='config'"
                ).fetchone()
                if record is None or json.loads(record[0]) != config:
                    raise ValueError("재개 검색 조건/출력 설정이 기존 기록과 다릅니다.")
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("PRAGMA synchronous=FULL")
            if not resume:
                with self.db:
                    self.db.execute(
                        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                    )
                    self.db.execute(
                        "CREATE TABLE rows (seq INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL)"
                    )
                    self.db.execute(
                        "CREATE TABLE chunks (part INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
                    )
                    self.db.execute(
                        "INSERT INTO meta VALUES ('config', ?)", (canonical(config),)
                    )
                    self.db.execute(
                        "INSERT INTO meta VALUES ('started_at', ?)",
                        (datetime.now().isoformat(timespec="seconds"),),
                    )
                fsync_directory(path.parent)
            else:
                # Detect parseable corruption too. Never discard a bad middle row.
                for expected, (seq, key, payload, digest) in enumerate(
                    self.db.execute(
                        "SELECT seq,key,payload,digest FROM rows ORDER BY seq"
                    ),
                    1,
                ):
                    if (
                        seq != expected
                        or fingerprint([key, payload]) != digest
                        or not isinstance(json.loads(payload), dict)
                    ):
                        raise ValueError(
                            "수집 행 무결성 검사 실패. 원본을 보존하고 중단합니다."
                        )
        except BaseException:
            self.close()
            raise

    @property
    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0]

    def contains(self, key: str) -> bool:
        return (
            self.db.execute("SELECT 1 FROM rows WHERE key=?", (key,)).fetchone()
            is not None
        )

    def append(self, row: dict, key: str, seq: int) -> bool:
        payload = canonical(row)
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO rows VALUES (?,?,?,?) ON CONFLICT(key) DO NOTHING",
                (seq, key, payload, fingerprint([key, payload])),
            )
        return cursor.rowcount == 1

    def rows(self, first: int, last: int) -> list[dict]:
        return [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT payload FROM rows WHERE seq BETWEEN ? AND ? ORDER BY seq",
                (first, last),
            )
        ]

    def bind_conditions(self, conditions: dict) -> None:
        value = canonical(conditions)
        record = self.db.execute(
            "SELECT value FROM meta WHERE key='effective_search'"
        ).fetchone()
        if record is not None and record[0] != value:
            raise ValueError("사이트의 실제 검색 조건이 기존 수집과 다릅니다.")
        if record is None:
            if self.count:
                raise ValueError(
                    "기존 행의 실제 검색 조건을 확인할 수 없어 재개를 중단합니다."
                )
            with self.db:
                self.db.execute(
                    "INSERT INTO meta VALUES ('effective_search', ?)", (value,)
                )

    def chunks(self) -> list[dict]:
        return [
            json.loads(r[0])
            for r in self.db.execute("SELECT payload FROM chunks ORDER BY part")
        ]

    def save_chunk(self, chunk: dict) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO chunks VALUES (?,?)",
                (chunk["part"], canonical(chunk)),
            )

    def close(self) -> None:
        try:
            if self.db is not None:
                self.db.close()
                self.db = None
        finally:
            self.lock.close()
