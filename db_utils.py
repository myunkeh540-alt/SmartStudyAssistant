"""
SQLite persistence for Smart Study Assistant.

History is stored as documents (the notes a student studied) and activities
(a summary, quiz, or flashcard set generated from a document). The original
`sessions` table is kept and its rows are migrated into documents/activities
the first time they are seen.
"""

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

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


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
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
        conn.execute("CREATE TABLE IF NOT EXISTS migrated_sessions (session_id INTEGER PRIMARY KEY)")
        _migrate_sessions(conn)


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
    cursor = conn.execute(
        """
        INSERT INTO documents (created_at, title, source_name, source_type, notes_text, notes_hash)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (created_at or _now(), title, source_name or "Pasted text", source_type or "Text", notes_text, notes_hash),
    )
    return cursor.lastrowid


def _insert_activity(
    conn: sqlite3.Connection,
    document_id: int,
    kind: str,
    payload,
    created_at: str | None = None,
    score: int | None = None,
    total: int | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO activities (document_id, kind, created_at, payload_json, score, total)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (document_id, kind, created_at or _now(), json.dumps(payload), score, total),
    )
    return cursor.lastrowid


def upsert_document(notes_text: str, source_name: str, source_type: str) -> int:
    """Finds or creates the document for these notes; the same notes always map to one document."""
    with _connect() as conn:
        return _upsert_document(conn, notes_text, source_name, source_type)


def add_activity(document_id: int, kind: str, payload) -> int:
    """kind is 'summary', 'quiz' or 'flashcards'."""
    with _connect() as conn:
        return _insert_activity(conn, document_id, kind, payload)


def record_quiz_result(activity_id: int, score: int, total: int) -> int:
    """
    Stores a finished quiz attempt. The first attempt fills in the score on the
    quiz's own record; later attempts (Try again) are saved as new records so
    every score counts. Returns the id of the record that holds this attempt.
    """
    with _connect() as conn:
        row = conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
        if row["score"] is None:
            conn.execute(
                "UPDATE activities SET score = ?, total = ?, created_at = ? WHERE id = ?",
                (score, total, _now(), activity_id),
            )
            return activity_id
        return _insert_activity(
            conn, row["document_id"], "quiz", json.loads(row["payload_json"]), score=score, total=total
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


def list_history() -> list[dict]:
    """
    Documents that have at least one activity, most recently studied first.
    Each has an "activities" list (newest first) of
    {id, kind, created_at, score, total, count, topics}.
    """
    with _connect() as conn:
        docs = conn.execute("SELECT id, title, source_name, source_type FROM documents").fetchall()
        acts = conn.execute(
            "SELECT id, document_id, kind, created_at, score, total, payload_json "
            "FROM activities ORDER BY created_at DESC, id DESC"
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


def history_stats() -> dict:
    with _connect() as conn:
        quiz_rows = conn.execute(
            "SELECT score, total FROM activities WHERE kind = 'quiz' AND score IS NOT NULL AND total > 0"
        ).fetchall()
        card_rows = conn.execute("SELECT payload_json FROM activities WHERE kind = 'flashcards'").fetchall()
    percents = [100 * r["score"] / r["total"] for r in quiz_rows]
    return {
        "quizzes_completed": len(quiz_rows),
        "average_score": round(sum(percents) / len(percents)) if percents else None,
        "flashcards_generated": sum(len(json.loads(r["payload_json"])) for r in card_rows),
    }


def get_activity(activity_id: int) -> dict:
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
            WHERE a.id = ?
            """,
            (activity_id,),
        ).fetchone()
        latest = {}
        for kind in ("summary", "quiz", "flashcards"):
            if kind == row["kind"]:
                latest[kind] = (row["id"], json.loads(row["payload_json"]))
                continue
            other = conn.execute(
                "SELECT id, payload_json FROM activities WHERE document_id = ? AND kind = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (row["document_id"], kind),
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


# ---------- original session API (kept for compatibility) ----------


def save_session(notes_text: str, summary: dict, quiz: list[dict], flashcards: list[dict]) -> int:
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO sessions (created_at, notes_text, summary, quiz_json, flashcards_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (_now(), notes_text, json.dumps(summary), json.dumps(quiz), json.dumps(flashcards)),
        )
        return cursor.lastrowid
