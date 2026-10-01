"""
Admin view of activity across all anonymous visitors.

Open the app with `?admin` in the URL (e.g. http://localhost:8501/?admin) and enter
the password set in the ADMIN_PASSWORD environment variable (.env). Without that
variable the page stays locked.
"""

import hmac
import os
from datetime import datetime

import pandas as pd
import streamlit as st

import db_utils

KIND_LABELS = {
    "summary": "Summary",
    "quiz": "Quiz",
    "flashcards": "Flashcards",
    "upload": "Uploaded file",
    "paste": "Pasted text",
    "upload_failed": "Upload failed",
    "generation_failed": "Generation failed",
}


def _local(iso: str | None) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(db_utils.local_tz()).strftime("%d %b %Y, %H:%M")


def _short(visitor_id: str) -> str:
    return visitor_id if visitor_id == db_utils.LEGACY_VISITOR else visitor_id[:8]


def _unlocked() -> bool:
    expected = os.environ.get("ADMIN_PASSWORD", "")
    if not expected:
        st.error("Admin is disabled. Set `ADMIN_PASSWORD` in your `.env` file and restart the app.")
        return False
    if st.session_state.get("admin_ok"):
        return True
    with st.form("admin_login"):
        password = st.text_input("Admin password", type="password")
        if st.form_submit_button("Open admin", type="primary"):
            if hmac.compare_digest(password.encode(), expected.encode()):
                st.session_state.admin_ok = True
                st.rerun()
            st.error("Wrong password.")
    return False


def render() -> None:
    st.subheader(":material/monitoring: Visitor activity")
    if not _unlocked():
        return

    overview = db_utils.admin_overview()
    average = f"{overview['average_score']}%" if overview["average_score"] is not None else "None yet"
    cols = st.columns(5)
    for col, value, label in (
        (cols[0], overview["visitors"], "Visitors"),
        (cols[1], overview["active_today"], "Active today"),
        (cols[2], overview["activities"], "Activities"),
        (cols[3], overview["quizzes_completed"], "Quizzes completed"),
        (cols[4], average, "Average quiz score"),
    ):
        with col:
            st.markdown(
                f"<div class='ssa-stat'><div class='ssa-stat-value'>{value}</div>"
                f"<div class='ssa-stat-label'>{label}</div></div>",
                unsafe_allow_html=True,
            )

    visitors = db_utils.admin_visitors()
    st.markdown("#### Visitors")
    st.caption(
        "Each visitor is one browser (anonymous cookie). Clearing cookies or switching "
        "browser/device shows up as a new visitor. 'legacy' is data saved before tracking began."
    )
    if not visitors:
        st.info("No visitors yet.", icon=":material/info:")
        return
    st.dataframe(
        pd.DataFrame(
            {
                "Visitor": [_short(v["visitor_id"]) for v in visitors],
                "First seen": [_local(v["first_seen"]) for v in visitors],
                "Last seen": [_local(v["last_seen"]) for v in visitors],
                "Visits": [v["visits"] for v in visitors],
                "Documents": [v["documents"] for v in visitors],
                "Summaries": [v["summaries"] for v in visitors],
                "Quizzes": [v["quizzes"] for v in visitors],
                "Avg quiz score": [round(v["avg_score"]) if v["avg_score"] is not None else None for v in visitors],
                "Flashcard sets": [v["flashcard_sets"] for v in visitors],
            }
        ),
        hide_index=True,
        use_container_width=True,
        column_config={"Avg quiz score": st.column_config.NumberColumn(format="%d%%")},
    )

    st.markdown("#### Activity log")
    options = {"All visitors": None, **{_short(v["visitor_id"]): v["visitor_id"] for v in visitors}}
    chosen = st.selectbox("Show activity for", list(options))
    rows = db_utils.admin_activity(options[chosen])
    if not rows:
        st.info("No activity yet.", icon=":material/info:")
        return
    st.dataframe(
        pd.DataFrame(
            {
                "When": [_local(r["created_at"]) for r in rows],
                "Visitor": [_short(r["visitor_id"]) for r in rows],
                "Type": [KIND_LABELS.get(r["kind"], r["kind"]) for r in rows],
                "File / text": [
                    f"{r['source_name']} ({r['source_type']})" if r["source_type"] else r["source_name"]
                    for r in rows
                ],
                "Score": [
                    f"{r['score']} / {r['total']}" if r["kind"] == "quiz" and r["score"] is not None else ""
                    for r in rows
                ],
                "Details": [r["detail"] for r in rows],
            }
        ),
        hide_index=True,
        use_container_width=True,
    )
