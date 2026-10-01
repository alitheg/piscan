"""SQLite database and page-image folder. The only module that touches either."""

from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image

INBOX = "inbox"
SENDING = "sending"
SENT = "sent"
FAILED = "failed"

THUMB_SIZE = (300, 300)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    paperless_task_id TEXT,
    paperless_document_id INTEGER,
    error TEXT,
    sent_at TEXT
);
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    rotation INTEGER NOT NULL DEFAULT 0,
    arrived_at TEXT NOT NULL,
    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    position INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS pages_draft ON pages(draft_id, position);
"""


@dataclass
class Page:
    id: int
    sha256: str
    path: Path
    thumb_path: Path
    rotation: int
    arrived_at: datetime
    draft_id: int
    position: int


@dataclass
class Draft:
    id: int
    created_at: datetime
    status: str
    pages: list[Page] = field(default_factory=list)
    paperless_task_id: str | None = None
    paperless_document_id: int | None = None
    error: str | None = None
    sent_at: datetime | None = None


def _thumb_for(path: Path) -> Path:
    return path.with_name(path.stem + ".thumb.jpg")


def _unlink(*paths: Path) -> None:
    for p in paths:
        with contextlib.suppress(FileNotFoundError):
            p.unlink()


def _iso(dt: datetime) -> str:
    # Always UTC so the SQL string ordering matches chronological ordering.
    if dt.tzinfo is None:
        raise ValueError("naive datetime; use timezone-aware UTC")
    return dt.astimezone(UTC).isoformat()


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.pages_dir = self.data_dir / "pages"
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # isolation_level=None: transactions are explicit, see _tx().
        self._db = sqlite3.connect(
            self.data_dir / "piscan.db", check_same_thread=False, isolation_level=None
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            try:
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    # -- row helpers -------------------------------------------------------

    def _page(self, row: sqlite3.Row) -> Page:
        path = Path(row["path"])
        return Page(
            id=row["id"],
            sha256=row["sha256"],
            path=path,
            thumb_path=_thumb_for(path),
            rotation=row["rotation"],
            arrived_at=datetime.fromisoformat(row["arrived_at"]),
            draft_id=row["draft_id"],
            position=row["position"],
        )

    def _draft(self, row: sqlite3.Row, pages: list[Page]) -> Draft:
        return Draft(
            id=row["id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            status=row["status"],
            pages=pages,
            paperless_task_id=row["paperless_task_id"],
            paperless_document_id=row["paperless_document_id"],
            error=row["error"],
            sent_at=datetime.fromisoformat(row["sent_at"]) if row["sent_at"] else None,
        )

    def _require_draft(self, db: sqlite3.Connection, draft_id: int) -> sqlite3.Row:
        row = db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise KeyError(draft_id)
        return row

    def _require_page(self, db: sqlite3.Connection, page_id: int) -> sqlite3.Row:
        row = db.execute("SELECT * FROM pages WHERE id=?", (page_id,)).fetchone()
        if row is None:
            raise KeyError(page_id)
        return row

    def _renumber(self, db: sqlite3.Connection, draft_id: int) -> None:
        ids = [
            r["id"]
            for r in db.execute(
                "SELECT id FROM pages WHERE draft_id=? ORDER BY position, id",
                (draft_id,),
            )
        ]
        for pos, pid in enumerate(ids):
            db.execute("UPDATE pages SET position=? WHERE id=?", (pos, pid))

    def _page_files(self, rows: Iterable[sqlite3.Row]) -> list[Path]:
        files: list[Path] = []
        for r in rows:
            p = Path(r["path"])
            files += [p, _thumb_for(p)]
        return files

    # -- ingest ------------------------------------------------------------

    def new_tmp_path(self) -> Path:
        return self.pages_dir / f"{uuid.uuid4().hex}.tmp"

    def has_sha(self, sha256: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM pages WHERE sha256=?", (sha256,)
            ).fetchone()
        return row is not None

    def add_page(
        self, tmp_file: Path, sha256: str, arrived_at: datetime
    ) -> Page | None:
        final = self.pages_dir / f"{sha256}.jpg"
        thumb = _thumb_for(final)
        with self._tx() as db:
            if db.execute("SELECT 1 FROM pages WHERE sha256=?", (sha256,)).fetchone():
                _unlink(tmp_file)
                return None
            # An orphan at `final` (crash after rename, before insert) is replaced.
            os.replace(tmp_file, final)
            with Image.open(final) as img:
                img.draft("RGB", THUMB_SIZE)
                img = img.convert("RGB")
                img.thumbnail(THUMB_SIZE)
                img.save(thumb, "JPEG")
            cur = db.execute(
                "INSERT INTO drafts (created_at, status) VALUES (?, ?)",
                (_iso(arrived_at), INBOX),
            )
            draft_id = cur.lastrowid
            cur = db.execute(
                "INSERT INTO pages (sha256, path, rotation, arrived_at, draft_id, position)"
                " VALUES (?, ?, 0, ?, ?, 0)",
                (sha256, str(final), _iso(arrived_at), draft_id),
            )
            page = self._page(self._require_page(db, cur.lastrowid))
        self._fsync_dir(self.pages_dir)
        return page

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        # Makes the rename durable before ingest deletes the scanner's copy.
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    # -- queries -----------------------------------------------------------

    def list_drafts(self, statuses: Iterable[str]) -> list[Draft]:
        statuses = list(statuses)
        if not statuses:
            return []
        marks = ",".join("?" * len(statuses))
        with self._lock:
            drafts = self._db.execute(
                f"SELECT * FROM drafts WHERE status IN ({marks}) ORDER BY created_at, id",
                statuses,
            ).fetchall()
            pages = self._db.execute(
                f"SELECT p.* FROM pages p JOIN drafts d ON d.id = p.draft_id"
                f" WHERE d.status IN ({marks}) ORDER BY p.position, p.id",
                statuses,
            ).fetchall()
        by_draft: dict[int, list[Page]] = {}
        for r in pages:
            by_draft.setdefault(r["draft_id"], []).append(self._page(r))
        return [self._draft(d, by_draft.get(d["id"], [])) for d in drafts]

    def get_draft(self, draft_id: int) -> Draft:
        with self._lock:
            row = self._require_draft(self._db, draft_id)
            pages = self._db.execute(
                "SELECT * FROM pages WHERE draft_id=? ORDER BY position, id",
                (draft_id,),
            ).fetchall()
        return self._draft(row, [self._page(r) for r in pages])

    # -- editing -----------------------------------------------------------

    @staticmethod
    def _require_editable(draft_row: sqlite3.Row) -> None:
        # The sender reads sending drafts' files, and sent drafts have none.
        if draft_row["status"] not in (INBOX, FAILED):
            raise ValueError(
                f"draft {draft_row['id']} is {draft_row['status']}, cannot edit"
            )

    def _editable_page(self, db: sqlite3.Connection, page_id: int) -> sqlite3.Row:
        page = self._require_page(db, page_id)
        self._require_editable(self._require_draft(db, page["draft_id"]))
        return page

    def merge(self, draft_ids: list[int]) -> int:
        ids = list(dict.fromkeys(draft_ids))
        if len(ids) < 2:
            raise ValueError("merge needs at least two distinct drafts")
        with self._tx() as db:
            rows = [self._require_draft(db, i) for i in ids]
            for r in rows:
                self._require_editable(r)
            target = min(rows, key=lambda r: (r["created_at"], r["id"]))["id"]
            marks = ",".join("?" * len(ids))
            ordered = [
                r["id"]
                for r in db.execute(
                    f"SELECT id FROM pages WHERE draft_id IN ({marks}) ORDER BY arrived_at, id",
                    ids,
                )
            ]
            for pos, pid in enumerate(ordered):
                db.execute(
                    "UPDATE pages SET draft_id=?, position=? WHERE id=?",
                    (target, pos, pid),
                )
            for i in ids:
                if i != target:
                    db.execute("DELETE FROM drafts WHERE id=?", (i,))
            # The content changed, so a previous failure no longer applies.
            db.execute(
                "UPDATE drafts SET status=?, error=NULL, paperless_task_id=NULL WHERE id=?",
                (INBOX, target),
            )
            return target

    def split(self, page_id: int) -> int:
        with self._tx() as db:
            page = self._editable_page(db, page_id)
            count = db.execute(
                "SELECT COUNT(*) FROM pages WHERE draft_id=?", (page["draft_id"],)
            ).fetchone()[0]
            if count < 2:
                raise ValueError("cannot split the only page of a draft")
            cur = db.execute(
                "INSERT INTO drafts (created_at, status) VALUES (?, ?)",
                (page["arrived_at"], INBOX),
            )
            new_id = cur.lastrowid
            db.execute(
                "UPDATE pages SET draft_id=?, position=0 WHERE id=?", (new_id, page_id)
            )
            self._renumber(db, page["draft_id"])
            return new_id

    def move_page(self, page_id: int, delta: int) -> None:
        if delta not in (-1, 1):
            raise ValueError("delta must be -1 or +1")
        with self._tx() as db:
            page = self._editable_page(db, page_id)
            other = db.execute(
                "SELECT id, position FROM pages WHERE draft_id=? AND position=?",
                (page["draft_id"], page["position"] + delta),
            ).fetchone()
            if other is None:
                return
            db.execute(
                "UPDATE pages SET position=? WHERE id=?", (other["position"], page_id)
            )
            db.execute(
                "UPDATE pages SET position=? WHERE id=?",
                (page["position"], other["id"]),
            )

    def rotate(self, page_id: int) -> None:
        with self._tx() as db:
            self._editable_page(db, page_id)
            db.execute(
                "UPDATE pages SET rotation=(rotation + 90) % 360 WHERE id=?", (page_id,)
            )

    def delete_page(self, page_id: int) -> None:
        with self._tx() as db:
            page = self._editable_page(db, page_id)
            db.execute("DELETE FROM pages WHERE id=?", (page_id,))
            remaining = db.execute(
                "SELECT COUNT(*) FROM pages WHERE draft_id=?", (page["draft_id"],)
            ).fetchone()[0]
            if remaining:
                self._renumber(db, page["draft_id"])
            else:
                db.execute("DELETE FROM drafts WHERE id=?", (page["draft_id"],))
            files = self._page_files([page])
        _unlink(*files)

    def delete_draft(self, draft_id: int) -> None:
        with self._tx() as db:
            self._require_editable(self._require_draft(db, draft_id))
            pages = db.execute(
                "SELECT * FROM pages WHERE draft_id=?", (draft_id,)
            ).fetchall()
            db.execute("DELETE FROM drafts WHERE id=?", (draft_id,))
            files = self._page_files(pages)
        _unlink(*files)

    # -- send lifecycle ----------------------------------------------------

    def set_sending(self, draft_id: int) -> None:
        with self._tx() as db:
            self._require_draft(db, draft_id)
            db.execute(
                "UPDATE drafts SET status=?, error=NULL, paperless_task_id=NULL WHERE id=?",
                (SENDING, draft_id),
            )

    def set_task(self, draft_id: int, task_id: str) -> None:
        with self._tx() as db:
            self._require_draft(db, draft_id)
            db.execute(
                "UPDATE drafts SET paperless_task_id=? WHERE id=?", (task_id, draft_id)
            )

    def mark_sent(self, draft_id: int, document_id: int | None, now: datetime) -> None:
        # Page rows stay (files go): the sha256 rows keep dedupe working while the
        # scan may still be on the flash, and the count feeds "Recently sent".
        # purge_sent removes them with the draft.
        with self._tx() as db:
            self._require_draft(db, draft_id)
            pages = db.execute(
                "SELECT * FROM pages WHERE draft_id=?", (draft_id,)
            ).fetchall()
            db.execute(
                "UPDATE drafts SET status=?, paperless_document_id=?, sent_at=?, error=NULL"
                " WHERE id=?",
                (SENT, document_id, _iso(now), draft_id),
            )
            files = self._page_files(pages)
        _unlink(*files)

    def mark_failed(self, draft_id: int, error: str) -> None:
        with self._tx() as db:
            self._require_draft(db, draft_id)
            db.execute(
                "UPDATE drafts SET status=?, error=? WHERE id=?",
                (FAILED, error, draft_id),
            )

    def reset_to_inbox(self, draft_id: int) -> None:
        with self._tx() as db:
            self._require_draft(db, draft_id)
            db.execute(
                "UPDATE drafts SET status=?, error=NULL, paperless_task_id=NULL WHERE id=?",
                (INBOX, draft_id),
            )

    def purge_sent(self, older_than: timedelta, now: datetime) -> None:
        cutoff = _iso(now - older_than)
        with self._tx() as db:
            rows = db.execute(
                "SELECT id, sent_at FROM drafts WHERE status=?", (SENT,)
            ).fetchall()
            for r in rows:
                if r["sent_at"] and r["sent_at"] < cutoff:
                    db.execute("DELETE FROM drafts WHERE id=?", (r["id"],))

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.data_dir).free
