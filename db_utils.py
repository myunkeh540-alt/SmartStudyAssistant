"""
Persistence for Smart Study Assistant.

Uses Postgres when the DATABASE_URL environment variable is set (needed on hosts that
lose local files, and so every deployment shares one database), otherwise a local
SQLite file.

History is stored as documents (the notes a student studied) and activities
(a summary, quiz, or flashcard set generated from a document). The original
`sessions` table is kept and its rows are migrated into documents/activities
the first time they are seen.
"""

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DB_PATH = "study_assistant.db"

_MARKDOWN_SYNTAX = re.compile(r"^[#>*\-]+\s*|\|", re.MULTILINE)
_GENERIC_SOURCES = {"Pasted text", "Loaded from history", "Saved session", ""}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plain_preview(notes_text: str, length: int = 120) -> str:
    """Strip Markdown syntax so a truncated notes preview reads as plain text."""
    plain = _MARKDOWN_SYNTAX.sub("", notes_text).replace("\n", " ")
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain[:length]


def local_tz():
    """Timezone used when showing times; set APP_TIMEZONE (e.g. Asia/Kuala_Lumpur) on a server that runs in UTC."""
    name = os.environ.get("APP_TIMEZONE", "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


class _Connection:
    """
    Thin wrapper so the queries below run on both SQLite and Postgres. Rows support
    row["column"] on both. Use as a context manager: commits on success, rolls back on
    error, and always closes.
    """

    def __init__(self) -> None:
        url = os.environ.get("DATABASE_URL", "").strip()
        self.pg = bool(url)
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row

            # prepare_threshold=None keeps this working behind Supabase's transaction pooler.
            self._raw = psycopg.connect(url, row_factory=dict_row, prepare_threshold=None)
        else:
            self._raw = sqlite3.connect(DB_PATH)
            self._raw.row_factory = sqlite3.Row

    def execute(self, sql: str, params=()):
        if self.pg:
            sql = sql.replace("?", "%s").replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
        return self._raw.execute(sql, params)

    def insert(self, sql: str, params=()) -> int:
        """Runs an INSERT into a table with an integer `id` key and returns the new id."""
        if self.pg:
            return self.execute(sql + " RETURNING id", params).fetchone()["id"]
        return self.execute(sql, params).lastrowid

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type:
                self._raw.rollback()
            else:
                self._raw.commit()
        finally:
            self._raw.close()


def _connect() -> _Connection:
    return _Connection()


def init_db() -> None:
    with _connect() as conn:
        if conn.pg:
            # Several sessions can start at once on a fresh database; let one create the tables.
            conn.execute("SELECT pg_advisory_xact_lock(727274)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                notes_text TEXT NOT NULL,
                summary TEXT NOT NULL,
                quiz_json TEXT NOT NULL,
                flashcards_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                title TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                notes_text TEXT NOT NULL,
                notes_hash TEXT NOT NULL UNIQUE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES documents(id),
                kind TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                score INTEGER,
                total INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS visitors (
                visitor_id TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                visits INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                visitor_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                event TEXT NOT NULL,
                source_name TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # source_name: the file/text an activity was generated from, as named by the visitor.
        # visitor_id: rows saved before visitor tracking keep a NULL visitor ("legacy").
        if conn.pg:
            conn.execute("ALTER TABLE activities ADD COLUMN IF NOT EXISTS source_name TEXT")
            conn.execute("ALTER TABLE activities ADD COLUMN IF NOT EXISTS visitor_id TEXT")
        else:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(activities)")}
            if "source_name" not in columns:
                conn.execute("ALTER TABLE activities ADD COLUMN source_name TEXT")
            if "visitor_id" not in columns:
                conn.execute("ALTER TABLE activities ADD COLUMN visitor_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_activities_visitor ON activities(visitor_id)")
        conn.execute("CREATE TABLE IF NOT EXISTS migrated_sessions (session_id INTEGER PRIMARY KEY)")
        _migrate_sessions(conn)


def log_event(visitor_id: str, event: str, source_name: str = "", source_type: str = "", detail: str = "") -> None:
    """
    Records something a visitor did that is not a saved activity, such as an upload,
    a paste, or a failed upload/generation, so it still shows up in the admin log.
    """
    with _connect() as conn:
        conn.execute(
            "INSERT INTO events (visitor_id, created_at, event, source_name, source_type, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (visitor_id, _now(), event, source_name, source_type, detail[:300]),
        )


def touch_visitor(visitor_id: str) -> None:
    """Records the start of a visit by an anonymous browser (first seen, last seen, visit count)."""
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO visitors (visitor_id, first_seen, last_seen) VALUES (?, ?, ?)
            ON CONFLICT(visitor_id) DO UPDATE SET last_seen = excluded.last_seen, visits = visitors.visits + 1
            """,
            (visitor_id, now, now),
        )


# ---------- documents and activities ----------


def _derive_title(notes_text: str) -> str:
    first_line = next((line for line in notes_text.splitlines() if line.strip()), "")
    return _plain_preview(first_line, length=60) or "Untitled notes"


def _upsert_document(
    conn: sqlite3.Connection,
    notes_text: str,
    source_name: str,
    source_type: str,
    created_at: str | None = None,
) -> int:
    notes_hash = hashlib.sha256(notes_text.encode("utf-8")).hexdigest()
    row = conn.execute("SELECT id FROM documents WHERE notes_hash = ?", (notes_hash,)).fetchone()
    if row:
        return row["id"]
    title = _derive_title(notes_text) if source_name in _GENERIC_SOURCES else source_name
    return conn.insert(
        """
        INSERT INTO documents (created_at, title, source_name, source_type, notes_text, notes_hash)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (created_at or _now(), title, source_name or "Pasted text", source_type or "Text", notes_text, notes_hash),
    )


def _insert_activity(
    conn: sqlite3.Connection,
    document_id: int,
    kind: str,
    payload,
    created_at: str | None = None,
    score: int | None = None,
    total: int | None = None,
    visitor_id: str | None = None,
    source_name: str | None = None,
) -> int:
    return conn.insert(
        """
        INSERT INTO activities (document_id, kind, created_at, payload_json, score, total, visitor_id, source_name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (document_id, kind, created_at or _now(), json.dumps(payload), score, total, visitor_id, source_name),
    )


def upsert_document(notes_text: str, source_name: str, source_type: str) -> int:
    """Finds or creates the document for these notes; the same notes always map to one document."""
    with _connect() as conn:
        return _upsert_document(conn, notes_text, source_name, source_type)


def add_activity(document_id: int, kind: str, payload, visitor_id: str, source_name: str | None = None) -> int:
    """kind is 'summary', 'quiz' or 'flashcards'."""
    with _connect() as conn:
        return _insert_activity(conn, document_id, kind, payload, visitor_id=visitor_id, source_name=source_name)


def record_quiz_result(activity_id: int, score: int, total: int, visitor_id: str) -> int:
    """
    Stores a finished quiz attempt. The first attempt fills in the score on the
    quiz's own record; later attempts (Try again) are saved as new records so
    every score counts. Returns the id of the record that holds this attempt.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM activities WHERE id = ? AND visitor_id = ?", (activity_id, visitor_id)
        ).fetchone()
        if row["score"] is None:
            conn.execute(
                "UPDATE activities SET score = ?, total = ?, created_at = ? WHERE id = ?",
                (score, total, _now(), activity_id),
            )
            return activity_id
        return _insert_activity(
            conn,
            row["document_id"],
            "quiz",
            json.loads(row["payload_json"]),
            score=score,
            total=total,
            visitor_id=visitor_id,
            source_name=row["source_name"],
        )


def _migrate_sessions(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT * FROM sessions WHERE id NOT IN (SELECT session_id FROM migrated_sessions) ORDER BY id"
    ).fetchall()
    for row in rows:
        doc_id = _upsert_document(conn, row["notes_text"], "Saved session", "Session", row["created_at"])
        summary = _decode_summary(row["summary"])
        quiz = json.loads(row["quiz_json"])
        cards = json.loads(row["flashcards_json"])
        if any(summary.get(k) for k in summary):
            _insert_activity(conn, doc_id, "summary", summary, row["created_at"])
        if quiz:
            _insert_activity(conn, doc_id, "quiz", quiz, row["created_at"])
        if cards:
            _insert_activity(conn, doc_id, "flashcards", cards, row["created_at"])
        conn.execute("INSERT INTO migrated_sessions (session_id) VALUES (?)", (row["id"],))


def _decode_summary(raw: str) -> dict:
    """Parses a stored summary, tolerating rows saved before summaries were structured."""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return {"key_points": [raw], "definitions": [], "important_concepts": [], "formulas": [], "exam_focus": []}


def list_history(visitor_id: str) -> list[dict]:
    """
    This visitor's documents that have at least one activity, most recently studied first.
    Each has an "activities" list (newest first) of
    {id, kind, created_at, score, total, count, topics}.
    """
    with _connect() as conn:
        docs = conn.execute("SELECT id, title, source_name, source_type FROM documents").fetchall()
        acts = conn.execute(
            "SELECT id, document_id, kind, created_at, score, total, payload_json "
            "FROM activities WHERE visitor_id = ? ORDER BY created_at DESC, id DESC",
            (visitor_id,),
        ).fetchall()

    by_doc: dict[int, list[dict]] = {}
    for a in acts:
        payload = json.loads(a["payload_json"])
        count = len(payload) if a["kind"] in ("quiz", "flashcards") else None
        topics = (
            sorted({q.get("topic", "") for q in payload if q.get("topic")}) if a["kind"] == "quiz" else []
        )
        by_doc.setdefault(a["document_id"], []).append(
            {
                "id": a["id"],
                "kind": a["kind"],
                "created_at": a["created_at"],
                "score": a["score"],
                "total": a["total"],
                "count": count,
                "topics": topics,
            }
        )

    history = [
        {
            "id": d["id"],
            "title": d["title"],
            "source_name": d["source_name"],
            "source_type": d["source_type"],
            "activities": by_doc[d["id"]],
        }
        for d in docs
        if d["id"] in by_doc
    ]
    history.sort(key=lambda d: d["activities"][0]["created_at"], reverse=True)
    return history


def history_stats(visitor_id: str) -> dict:
    with _connect() as conn:
        quiz_rows = conn.execute(
            "SELECT score, total FROM activities "
            "WHERE kind = 'quiz' AND score IS NOT NULL AND total > 0 AND visitor_id = ?",
            (visitor_id,),
        ).fetchall()
        card_rows = conn.execute(
            "SELECT payload_json FROM activities WHERE kind = 'flashcards' AND visitor_id = ?",
            (visitor_id,),
        ).fetchall()
    percents = [100 * r["score"] / r["total"] for r in quiz_rows]
    return {
        "quizzes_completed": len(quiz_rows),
        "average_score": round(sum(percents) / len(percents)) if percents else None,
        "flashcards_generated": sum(len(json.loads(r["payload_json"])) for r in card_rows),
    }


def get_activity(activity_id: int, visitor_id: str) -> dict:
    """
    The activity plus its document's notes, and the newest summary/quiz/flashcards
    for that document so the whole workspace can be restored consistently.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT a.id, a.kind, a.document_id, a.payload_json,
                   d.notes_text, d.source_name, d.source_type, d.title
            FROM activities a JOIN documents d ON d.id = a.document_id
            WHERE a.id = ? AND a.visitor_id = ?
            """,
            (activity_id, visitor_id),
        ).fetchone()
        latest = {}
        for kind in ("summary", "quiz", "flashcards"):
            if kind == row["kind"]:
                latest[kind] = (row["id"], json.loads(row["payload_json"]))
                continue
            other = conn.execute(
                "SELECT id, payload_json FROM activities WHERE document_id = ? AND kind = ? AND visitor_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (row["document_id"], kind, visitor_id),
            ).fetchone()
            latest[kind] = (other["id"], json.loads(other["payload_json"])) if other else (None, None)
    return {
        "id": row["id"],
        "kind": row["kind"],
        "document_id": row["document_id"],
        "notes_text": row["notes_text"],
        "source_name": row["source_name"],
        "source_type": row["source_type"],
        "title": row["title"],
        "summary": latest["summary"][1],
        "quiz": latest["quiz"][1],
        "quiz_activity_id": latest["quiz"][0],
        "flashcards": latest["flashcards"][1],
    }


# ---------- admin: activity across all visitors ----------

LEGACY_VISITOR = "legacy"


def admin_overview() -> dict:
    with _connect() as conn:
        visitors = conn.execute("SELECT COUNT(*) AS n FROM visitors").fetchone()["n"]
        row = conn.execute(
            """
            SELECT COUNT(*) AS activities,
                   COALESCE(SUM(CASE WHEN kind = 'quiz' AND score IS NOT NULL THEN 1 ELSE 0 END), 0)
                       AS quizzes_completed,
                   AVG(CASE WHEN kind = 'quiz' AND total > 0 THEN 100.0 * score / total END) AS average_score
            FROM activities WHERE visitor_id IS NOT NULL
            """
        ).fetchone()
        today_start = (
            datetime.now(local_tz()).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        )
        active_today = conn.execute(
            "SELECT COUNT(*) AS n FROM visitors WHERE last_seen >= ?", (today_start.isoformat(),)
        ).fetchone()["n"]
    return {
        "visitors": visitors,
        "active_today": active_today,
        "activities": row["activities"],
        "quizzes_completed": row["quizzes_completed"],
        "average_score": round(row["average_score"]) if row["average_score"] is not None else None,
    }


def admin_visitors() -> list[dict]:
    """One row per anonymous visitor, most recently seen first, plus a "legacy" row for pre-tracking data."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT v.visitor_id, v.first_seen, v.last_seen, v.visits,
                   COUNT(DISTINCT a.document_id) AS documents,
                   COALESCE(SUM(CASE WHEN a.kind = 'summary' THEN 1 ELSE 0 END), 0) AS summaries,
                   COALESCE(SUM(CASE WHEN a.kind = 'quiz' THEN 1 ELSE 0 END), 0) AS quizzes,
                   COALESCE(SUM(CASE WHEN a.kind = 'flashcards' THEN 1 ELSE 0 END), 0) AS flashcard_sets,
                   AVG(CASE WHEN a.kind = 'quiz' AND a.total > 0 THEN 100.0 * a.score / a.total END) AS avg_score,
                   MAX(a.created_at) AS last_activity
            FROM visitors v LEFT JOIN activities a ON a.visitor_id = v.visitor_id
            GROUP BY v.visitor_id ORDER BY v.last_seen DESC
            """
        ).fetchall()
        legacy = conn.execute(
            """
            SELECT COUNT(DISTINCT document_id) AS documents,
                   COALESCE(SUM(CASE WHEN kind = 'summary' THEN 1 ELSE 0 END), 0) AS summaries,
                   COALESCE(SUM(CASE WHEN kind = 'quiz' THEN 1 ELSE 0 END), 0) AS quizzes,
                   COALESCE(SUM(CASE WHEN kind = 'flashcards' THEN 1 ELSE 0 END), 0) AS flashcard_sets,
                   AVG(CASE WHEN kind = 'quiz' AND total > 0 THEN 100.0 * score / total END) AS avg_score,
                   MAX(created_at) AS last_activity, MIN(created_at) AS first_seen
            FROM activities WHERE visitor_id IS NULL
            """
        ).fetchone()
    out = [dict(r) for r in rows]
    if legacy["last_activity"]:
        out.append(
            {**dict(legacy), "visitor_id": LEGACY_VISITOR, "last_seen": legacy["last_activity"], "visits": None}
        )
    return out


def admin_activity(visitor_id: str | None = None, limit: int = 500) -> list[dict]:
    """Newest activities across all visitors, or for one visitor (LEGACY_VISITOR selects pre-tracking rows)."""
    where, params = "", []
    if visitor_id == LEGACY_VISITOR:
        where = "WHERE a.visitor_id IS NULL"
    elif visitor_id:
        where, params = "WHERE a.visitor_id = ?", [visitor_id]
    with _connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT a.id, a.created_at, COALESCE(a.visitor_id, '{LEGACY_VISITOR}') AS visitor_id,
                       a.kind, a.score, a.total, '' AS detail,
                       COALESCE(a.source_name, d.source_name) AS source_name, d.source_type
                FROM activities a JOIN documents d ON d.id = a.document_id
                {where} ORDER BY a.created_at DESC, a.id DESC LIMIT ?
                """,
                [*params, limit],
            )
        ]
        if visitor_id != LEGACY_VISITOR:
            event_where = "WHERE visitor_id = ?" if visitor_id else ""
            rows += [
                dict(r)
                for r in conn.execute(
                    f"""
                    SELECT id, created_at, visitor_id, event AS kind, NULL AS score, NULL AS total,
                           detail, source_name, source_type
                    FROM events {event_where} ORDER BY created_at DESC, id DESC LIMIT ?
                    """,
                    [*([visitor_id] if visitor_id else []), limit],
                )
            ]
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return rows[:limit]


# ---------- original session API (kept for compatibility) ----------


def save_session(notes_text: str, summary: dict, quiz: list[dict], flashcards: list[dict]) -> int:
    with _connect() as conn:
        return conn.insert(
            """
            INSERT INTO sessions (created_at, notes_text, summary, quiz_json, flashcards_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (_now(), notes_text, json.dumps(summary), json.dumps(quiz), json.dumps(flashcards)),
        )
