"""
Smart Study Assistant

Run with:
    streamlit run app.py
"""

import base64
import html
import io
import random
import re
import uuid
from datetime import datetime

import streamlit as st
from dotenv import load_dotenv

import admin_page
import db_utils
from ai_utils import (
    SUMMARY_SECTIONS,
    AIGenerationError,
    generate_ai_flashcards,
    generate_ai_quiz,
    generate_ai_summary,
    is_api_key_configured,
)
from export_utils import flashcards_to_csv, quiz_to_csv, session_to_pdf
from study_utils import TextExtractionError, extract_text

load_dotenv()


@st.cache_resource
def init_database() -> bool:
    """Creates/migrates tables once per server process rather than on every rerun."""
    db_utils.init_db()
    return True


init_database()

st.set_page_config(page_title="Smart Study Assistant", layout="wide")

with open("styles.css") as f:
    st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)

VISITOR_COOKIE = "ssa_visitor"


def get_visitor_id() -> str:
    """
    Anonymous per-browser ID, kept in a cookie so it survives refreshes and later
    visits. A new ID is made (and the cookie set) when the browser has none; clearing
    cookies or switching browser/device starts a new visitor.
    """
    if "visitor_id" not in st.session_state:
        cookie = st.context.cookies.get(VISITOR_COOKIE, "")
        visitor_id = cookie if re.fullmatch(r"[0-9a-f]{32}", cookie) else uuid.uuid4().hex
        st.session_state.visitor_id = visitor_id
        db_utils.touch_visitor(visitor_id)
        # Set from JS on every new session so the one-year expiry slides forward.
        st.html(
            f"<script>document.cookie = '{VISITOR_COOKIE}={visitor_id}; max-age=31536000; path=/; SameSite=Lax';</script>",
            unsafe_allow_javascript=True,
        )
    return st.session_state.visitor_id


if "admin" in st.query_params:
    admin_page.render()
    st.stop()

visitor_id = get_visitor_id()

DEFAULTS = {
    "notes_text": "",
    "notes_source_name": "",
    "notes_source_type": "",
    "notes_word_count": 0,
    "summary_text": None,
    "quiz_data": [],
    "flashcards_data": [],
    "flashcard_index": 0,
    "flashcard_revealed": False,
    "active_section": "Summary",
    "quiz_pos": 0,
    "quiz_answers": {},
    "quiz_hints": [],
    "quiz_choices": [],
    "quiz_phase": "quiz",
    "quiz_review_pos": 0,
    "quiz_attempt": 0,
    "document_id": None,
    "quiz_activity_id": None,
}
for key, default in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = default

key_configured = is_api_key_configured()


def reset_quiz_progress() -> None:
    """Start (or restart) an attempt at the loaded quiz, reshuffling the choices."""
    quiz = st.session_state.quiz_data
    st.session_state.quiz_pos = 0
    st.session_state.quiz_answers = {}
    st.session_state.quiz_hints = []
    st.session_state.quiz_phase = "quiz"
    st.session_state.quiz_review_pos = 0
    st.session_state.quiz_attempt += 1
    st.session_state.quiz_choices = [
        random.sample(q["choices"], len(q["choices"])) if "choices" in q else [] for q in quiz
    ]


def log_event(event: str, name: str = "", source_type: str = "", detail: str = "") -> None:
    """Admin-visible trail of uploads and failures, even when nothing gets saved to History."""
    db_utils.log_event(visitor_id, event, name, source_type, detail)


def current_document_id() -> int:
    """History record for the notes currently loaded (created on first use)."""
    if st.session_state.document_id is None:
        st.session_state.document_id = db_utils.upsert_document(
            st.session_state.notes_text,
            st.session_state.notes_source_name,
            st.session_state.notes_source_type,
        )
    return st.session_state.document_id


def open_activity(activity_id: int) -> None:
    """Restore a past summary, quiz or flashcard set (and its notes) from History."""
    a = db_utils.get_activity(activity_id, visitor_id)
    st.session_state.notes_text = a["notes_text"]
    st.session_state.notes_source_name = a["source_name"]
    st.session_state.notes_source_type = a["source_type"]
    st.session_state.notes_word_count = len(a["notes_text"].split())
    st.session_state.document_id = a["document_id"]
    st.session_state.summary_text = a["summary"]
    st.session_state.quiz_data = a["quiz"] or []
    st.session_state.quiz_activity_id = a["quiz_activity_id"]
    st.session_state.flashcards_data = a["flashcards"] or []
    st.session_state.flashcard_index = 0
    st.session_state.flashcard_revealed = False
    reset_quiz_progress()
    st.session_state.active_section = {
        "summary": "Summary",
        "quiz": "Quiz",
        "flashcards": "Flashcards",
    }[a["kind"]]


def record_answer(index: int) -> None:
    """Radio callback: store the pick so the question locks and shows feedback."""
    pick = st.session_state[f"quiz_pick_{st.session_state.quiz_attempt}_{index}"]
    st.session_state.quiz_answers = {**st.session_state.quiz_answers, index: pick}


def show_hint(index: int) -> None:
    st.session_state.quiz_hints = [*st.session_state.quiz_hints, index]


def render_answered(item: dict, choices: list, picked: str) -> None:
    """Choices with right/wrong marks, then the verdict and explanation."""
    rows = []
    for choice in choices:
        if choice == item["answer"]:
            state, note = "ok", "Correct answer"
        elif choice == picked:
            state, note = "no", "Your answer"
        else:
            state, note = "idle", ""
        tag = f"<span class='ssa-choice-note'>{note}</span>" if note else ""
        rows.append(f"<div class='ssa-choice ssa-choice-{state}'>{html.escape(choice)}{tag}</div>")
    st.markdown("".join(rows), unsafe_allow_html=True)
    if picked == item["answer"]:
        st.markdown("<div class='ssa-verdict ssa-verdict-ok'>Correct</div>", unsafe_allow_html=True)
    else:
        st.markdown("<div class='ssa-verdict ssa-verdict-no'>Not quite</div>", unsafe_allow_html=True)
    if item.get("explanation"):
        st.markdown(
            f"<div class='ssa-explain'>{html.escape(item['explanation'])}</div>",
            unsafe_allow_html=True,
        )


def render_question_text(item: dict) -> None:
    st.markdown(
        f"<div class='ssa-question'>{html.escape(item['question'])}</div>", unsafe_allow_html=True
    )



@st.cache_resource
def logo_data_uri(path: str = "logo.png", size: int = 96) -> str:
    """Downscaled logo as a data URI so the sidebar doesn't ship the 600 KB original."""
    from PIL import Image

    img = Image.open(path).convert("RGBA")
    img.thumbnail((size, size))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


NAV_ITEMS = [
    ("Summary", ":material/summarize:"),
    ("Quiz", ":material/quiz:"),
    ("Flashcards", ":material/style:"),
    ("History", ":material/history:"),
]

SUMMARY_SECTION_META = {
    "key_points": (":material/checklist:", "Key Points"),
    "definitions": (":material/menu_book:", "Definitions"),
    "important_concepts": (":material/lightbulb:", "Important Concepts"),
    "formulas": (":material/functions:", "Formulas"),
    "exam_focus": (":material/target:", "Exam Focus"),
}

with st.sidebar:
    st.markdown(
        f"<div class='ssa-brand'><img src='{logo_data_uri()}' alt=''>"
        "<span class='ssa-brand-name'>Smart Study<br>Assistant</span></div>",
        unsafe_allow_html=True,
    )
    st.write("")
    for item, icon in NAV_ITEMS:
        is_active = st.session_state.active_section == item
        if st.button(
            item,
            icon=icon,
            key=f"nav_{item}",
            type="primary" if is_active else "secondary",
            use_container_width=True,
        ):
            st.session_state.active_section = item
            st.rerun()

st.caption("Turn your own notes and documents into a summary, quiz, and flashcards.")

if not key_configured:
    st.error(
        "No OpenAI API key found. Create a `.env` file (see `.env.example`) with "
        "`OPENAI_API_KEY=your-key` and restart the app to enable generation."
    )

# Reserved here so the export menu appears at the top of the page, but filled
# in at the very end of the script - section logic further down mutates
# summary/quiz/flashcards state in this same rerun, so rendering this content
# before that logic runs would show stale state (e.g. a just-generated quiz
# not yet reflected in the export menu).
toolbar_placeholder = st.container()

st.divider()

section = st.session_state.active_section

if section == "Summary":
    st.subheader("Summary", divider=False)

    paste_col, upload_col = st.columns(2)

    def load_and_summarize(text: str, name: str, source_type: str) -> None:
        """Load notes, then summarize them in the same click."""
        st.session_state.notes_text = text
        st.session_state.notes_source_name = name
        st.session_state.notes_source_type = source_type
        st.session_state.notes_word_count = len(text.split())
        st.session_state.summary_text = None
        st.session_state.document_id = None
        if not key_configured:
            log_event("generation_failed", name, source_type, "Summary: no API key configured")
            return
        with st.spinner("Reading your notes and writing the summary..."):
            try:
                st.session_state.summary_text = generate_ai_summary(text)
                db_utils.add_activity(
                    current_document_id(), "summary", st.session_state.summary_text, visitor_id, name
                )
            except AIGenerationError as e:
                log_event("generation_failed", name, source_type, f"Summary: {e}")
                st.error(str(e))

    with paste_col:
        with st.container(border=True):
            st.markdown(":material/content_paste: **Paste text**")
            with st.form("paste_form"):
                pasted_text = st.text_area(
                    "Paste your notes here", height=160, label_visibility="collapsed"
                )
                paste_submitted = st.form_submit_button(
                    "Summarize", icon=":material/auto_awesome:", type="primary"
                )
            if paste_submitted:
                if pasted_text.strip():
                    log_event("paste", "Pasted text", "Text", f"{len(pasted_text.split())} words")
                    load_and_summarize(pasted_text.strip(), "Pasted text", "Text")
                else:
                    st.warning("Please paste some text first.")

    with upload_col:
        with st.container(border=True):
            st.markdown(":material/upload_file: **Upload a file**")
            uploaded_file = st.file_uploader(
                "Upload a TXT, PDF, DOCX, PPTX, CSV, XLSX, Markdown, or image file",
                type=["txt", "pdf", "docx", "pptx", "csv", "xlsx", "md", "png", "jpg", "jpeg"],
                label_visibility="collapsed",
            )
            if uploaded_file is not None and st.button(
                "Summarize", icon=":material/auto_awesome:", type="primary", key="summarize_upload"
            ):
                try:
                    with st.spinner("Reading your file..."):
                        extracted = extract_text(uploaded_file)
                except TextExtractionError as e:
                    log_event("upload_failed", uploaded_file.name, "", str(e))
                    st.error(str(e))
                else:
                    ext = (
                        uploaded_file.name.rsplit(".", 1)[-1].upper()
                        if "." in uploaded_file.name
                        else "FILE"
                    )
                    if extracted.strip():
                        log_event("upload", uploaded_file.name, ext, f"{len(extracted.split())} words")
                        load_and_summarize(extracted.strip(), uploaded_file.name, ext)
                    else:
                        log_event("upload_failed", uploaded_file.name, ext, "No text could be extracted")
                        st.warning("Could not extract any text from that file.")

    if not st.session_state.notes_text:
        st.info(
            "Upload a file or paste your notes above, then select Summarize.",
            icon=":material/info:",
        )
    else:
        st.divider()
        st.markdown("#### Your notes")
        st.markdown(
            f"<div class='ssa-source'><strong>{html.escape(st.session_state.notes_source_name)}</strong>"
            f" ({html.escape(st.session_state.notes_source_type)},"
            f" {st.session_state.notes_word_count} words)</div>",
            unsafe_allow_html=True,
        )
        with st.container(border=True, height=340, key="notes_card"):
            st.markdown(st.session_state.notes_text)

        st.markdown("#### Summary")
        if not key_configured:
            st.info("Add an OpenAI API key to generate a summary.", icon=":material/key:")
        elif not st.session_state.summary_text:
            st.info("Select Summarize to generate a summary of these notes.", icon=":material/info:")

    if st.session_state.summary_text:
        for key in SUMMARY_SECTIONS:
            icon, label = SUMMARY_SECTION_META[key]
            items = st.session_state.summary_text.get(key) or []
            with st.container(border=True, key=f"sum_{key}"):
                st.markdown(f"##### {icon} {label}")
                if items:
                    for item in items:
                        st.markdown(f"- {item}")
                else:
                    st.caption("Nothing noted for this material.")

elif section == "Quiz":
    st.subheader(":material/quiz: Quiz")
    quiz = st.session_state.quiz_data
    legacy = bool(quiz) and any("choices" not in q for q in quiz)

    if not quiz or legacy:
        if legacy:
            st.info(
                "This saved quiz uses the old short-answer format. "
                "Start a new multiple-choice quiz from your notes.",
                icon=":material/info:",
            )
        if not st.session_state.notes_text:
            st.info("Add study material from the Summary page first.", icon=":material/info:")
        elif not key_configured:
            st.info("Add an OpenAI API key to generate a quiz.", icon=":material/key:")
        else:
            num_questions = st.number_input(
                "Number of questions", min_value=1, max_value=50, value=5
            )
            if st.button("Start quiz", icon=":material/play_arrow:", type="primary"):
                with st.spinner("Writing your questions..."):
                    try:
                        st.session_state.quiz_data = generate_ai_quiz(
                            st.session_state.notes_text,
                            int(num_questions),
                            st.session_state.summary_text,
                        )
                        st.session_state.quiz_activity_id = db_utils.add_activity(
                            current_document_id(),
                            "quiz",
                            st.session_state.quiz_data,
                            visitor_id,
                            st.session_state.notes_source_name,
                        )
                        reset_quiz_progress()
                        started = True
                    except AIGenerationError as e:
                        started = False
                        log_event(
                            "generation_failed",
                            st.session_state.notes_source_name,
                            st.session_state.notes_source_type,
                            f"Quiz: {e}",
                        )
                        st.error(str(e))
                if started:
                    st.rerun()
    else:
        if len(st.session_state.quiz_choices) != len(quiz):
            reset_quiz_progress()
        total = len(quiz)
        answers = st.session_state.quiz_answers
        choices_all = st.session_state.quiz_choices
        phase = st.session_state.quiz_phase

        if phase == "quiz":
            i = st.session_state.quiz_pos
            item = quiz[i]
            answered = i in answers

            st.markdown(
                f"<div class='ssa-qmeta'>Question {i + 1} of {total}</div>", unsafe_allow_html=True
            )
            st.progress(len(answers) / total)
            render_question_text(item)

            if not answered:
                st.radio(
                    "Choose one answer",
                    choices_all[i],
                    index=None,
                    key=f"quiz_pick_{st.session_state.quiz_attempt}_{i}",
                    on_change=record_answer,
                    args=(i,),
                    label_visibility="collapsed",
                )
                if item.get("hint"):
                    if i in st.session_state.quiz_hints:
                        st.markdown(
                            f"<div class='ssa-hint'>Hint: {html.escape(item['hint'])}</div>",
                            unsafe_allow_html=True,
                        )
                    else:
                        st.button(
                            "Show hint",
                            icon=":material/lightbulb:",
                            type="tertiary",
                            on_click=show_hint,
                            args=(i,),
                        )
            else:
                render_answered(item, choices_all[i], answers[i])
                is_last = i == total - 1
                if st.button(
                    "See results" if is_last else "Next question",
                    icon=":material/arrow_forward:",
                    type="primary",
                ):
                    if is_last:
                        final_answers = {**answers}
                        correct = sum(
                            1 for k, q in enumerate(quiz) if final_answers.get(k) == q["answer"]
                        )
                        if st.session_state.quiz_activity_id is not None:
                            st.session_state.quiz_activity_id = db_utils.record_quiz_result(
                                st.session_state.quiz_activity_id, correct, total, visitor_id
                            )
                        st.session_state.quiz_phase = "results"
                    else:
                        st.session_state.quiz_pos = i + 1
                    st.rerun()

        else:
            missed = [i for i in range(total) if answers.get(i) != quiz[i]["answer"]]
            score = total - len(missed)

            if phase == "results":
                percent = round(100 * score / total)
                st.markdown(
                    f"<div class='ssa-score' style='--ssa-target:{score}'>"
                    f"<span class='ssa-score-total'> / {total}</span></div>"
                    f"<div class='ssa-percent'>{percent}% correct</div>",
                    unsafe_allow_html=True,
                )
                if not missed:
                    st.success("You answered every question correctly.")
                else:
                    topics = list(dict.fromkeys(quiz[i].get("topic", "General") for i in missed))
                    st.markdown("##### Topics to review")
                    for topic in topics:
                        st.markdown(f"- {topic}")
                    st.markdown("##### Incorrect questions")
                    for i in missed:
                        st.markdown(f"- {quiz[i]['question']}")

                action_cols = st.columns(3)
                with action_cols[0]:
                    if missed and st.button(
                        "Review mistakes",
                        icon=":material/fact_check:",
                        type="primary",
                        use_container_width=True,
                    ):
                        st.session_state.quiz_phase = "review"
                        st.session_state.quiz_review_pos = 0
                        st.rerun()
                with action_cols[1]:
                    if st.button(
                        "Try again",
                        icon=":material/replay:",
                        type="secondary" if missed else "primary",
                        use_container_width=True,
                    ):
                        reset_quiz_progress()
                        st.rerun()
                with action_cols[2]:
                    if st.button("New quiz", icon=":material/add:", use_container_width=True):
                        st.session_state.quiz_data = []
                        reset_quiz_progress()
                        st.rerun()

            else:  # review
                pos = min(st.session_state.quiz_review_pos, len(missed) - 1)
                i = missed[pos]
                st.markdown(
                    f"<div class='ssa-qmeta'>Mistake {pos + 1} of {len(missed)}"
                    f" (question {i + 1}, {html.escape(quiz[i].get('topic', 'General'))})</div>",
                    unsafe_allow_html=True,
                )
                render_question_text(quiz[i])
                render_answered(quiz[i], choices_all[i], answers.get(i))
                nav = st.columns(3)
                with nav[0]:
                    if st.button(
                        "Previous",
                        icon=":material/chevron_left:",
                        disabled=pos == 0,
                        use_container_width=True,
                    ):
                        st.session_state.quiz_review_pos = pos - 1
                        st.rerun()
                with nav[1]:
                    if st.button("Back to results", use_container_width=True):
                        st.session_state.quiz_phase = "results"
                        st.rerun()
                with nav[2]:
                    if st.button(
                        "Next",
                        icon=":material/chevron_right:",
                        disabled=pos == len(missed) - 1,
                        use_container_width=True,
                    ):
                        st.session_state.quiz_review_pos = pos + 1
                        st.rerun()

elif section == "Flashcards":
    st.subheader(":material/style: Flashcards")
    if not st.session_state.notes_text:
        st.info("Add study material from the Summary page first.", icon=":material/info:")
    elif not key_configured:
        st.info("Add an OpenAI API key to generate flashcards.", icon=":material/key:")
    else:
        num_cards = st.number_input(
            "Number of flashcards", min_value=1, max_value=50, value=8
        )
        if st.button("Generate Flashcards", icon=":material/auto_awesome:", type="primary"):
            with st.spinner("Generating flashcards..."):
                try:
                    st.session_state.flashcards_data = generate_ai_flashcards(
                        st.session_state.notes_text, int(num_cards)
                    )
                    st.session_state.flashcard_index = 0
                    st.session_state.flashcard_revealed = False
                    db_utils.add_activity(
                        current_document_id(),
                        "flashcards",
                        st.session_state.flashcards_data,
                        visitor_id,
                        st.session_state.notes_source_name,
                    )
                except AIGenerationError as e:
                    log_event(
                        "generation_failed",
                        st.session_state.notes_source_name,
                        st.session_state.notes_source_type,
                        f"Flashcards: {e}",
                    )
                    st.error(str(e))

    if st.session_state.flashcards_data:
        st.divider()
        total = len(st.session_state.flashcards_data)
        index = min(st.session_state.flashcard_index, total - 1)
        card = st.session_state.flashcards_data[index]

        st.markdown(
            f"<div class='ssa-card-count'>Card {index + 1} of {total}</div>",
            unsafe_allow_html=True,
        )
        if st.session_state.flashcard_revealed:
            st.markdown(
                f"<div class='ssa-card ssa-card-back'><span class='ssa-card-side'>Answer</span>"
                f"{html.escape(card['back'])}</div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f"<div class='ssa-card'><span class='ssa-card-side'>Question</span>"
                f"{html.escape(card['front'])}</div>",
                unsafe_allow_html=True,
            )
        st.write("")

        nav_cols = st.columns(3)
        with nav_cols[0]:
            if st.button(
                "Previous",
                icon=":material/chevron_left:",
                disabled=index == 0,
                use_container_width=True,
            ):
                st.session_state.flashcard_index = index - 1
                st.session_state.flashcard_revealed = False
                st.rerun()
        with nav_cols[1]:
            revealed = st.session_state.flashcard_revealed
            reveal_label = "Show question" if revealed else "Show answer"
            reveal_icon = ":material/flip_to_front:" if revealed else ":material/flip_to_back:"
            if st.button(reveal_label, icon=reveal_icon, use_container_width=True):
                st.session_state.flashcard_revealed = not revealed
                st.rerun()
        with nav_cols[2]:
            if st.button(
                "Next",
                icon=":material/chevron_right:",
                disabled=index == total - 1,
                use_container_width=True,
            ):
                st.session_state.flashcard_index = index + 1
                st.session_state.flashcard_revealed = False
                st.rerun()

elif section == "History":
    st.subheader(":material/history: History")
    history = db_utils.list_history(visitor_id)

    if not history:
        st.info(
            "Nothing here yet. Summaries, quizzes and flashcards you create are saved "
            "automatically. Start on the Summary page.",
            icon=":material/info:",
        )
    else:
        stats = db_utils.history_stats(visitor_id)
        average = f"{stats['average_score']}%" if stats["average_score"] is not None else "None yet"
        stat_cols = st.columns(3)
        for col, value, label in (
            (stat_cols[0], stats["quizzes_completed"], "Quizzes completed"),
            (stat_cols[1], average, "Average quiz score"),
            (stat_cols[2], stats["flashcards_generated"], "Flashcards generated"),
        ):
            with col:
                st.markdown(
                    f"<div class='ssa-stat'><div class='ssa-stat-value'>{value}</div>"
                    f"<div class='ssa-stat-label'>{label}</div></div>",
                    unsafe_allow_html=True,
                )

        filter_col, search_col = st.columns([3, 2])
        with filter_col:
            chosen = st.segmented_control(
                "Show",
                ["All", "Summary", "Quiz", "Flashcards"],
                default="All",
                key="history_filter",
                label_visibility="collapsed",
            )
        with search_col:
            query = st.text_input(
                "Search history",
                placeholder="Search by document or topic",
                icon=":material/search:",
                label_visibility="collapsed",
            ).strip().lower()

        kind_filter = {"Summary": "summary", "Quiz": "quiz", "Flashcards": "flashcards"}.get(chosen)
        kind_label = {"summary": "Summary", "quiz": "Quiz", "flashcards": "Flashcards"}

        visible = []
        for doc in history:
            doc_matches = not query or query in f"{doc['title']} {doc['source_name']}".lower()
            acts = [
                a
                for a in doc["activities"]
                if (kind_filter is None or a["kind"] == kind_filter)
                and (doc_matches or any(query in t.lower() for t in a["topics"]))
            ]
            if acts:
                visible.append((doc, acts))

        if not visible:
            st.info(
                "No matching history. Clear the search or choose All.", icon=":material/search_off:"
            )

        for doc, acts in visible:
            with st.container(border=True, key=f"hdoc_{doc['id']}"):
                st.markdown(f"<div class='ssa-doc-title'>{html.escape(doc['title'])}</div>", unsafe_allow_html=True)
                if doc["source_name"] not in (doc["title"], "Saved session", "Pasted text"):
                    st.caption(f"{doc['source_name']} ({doc['source_type']})")
                elif doc["source_name"] == "Pasted text":
                    st.caption("Pasted text")
                for a in acts:
                    when = datetime.fromisoformat(a["created_at"]).astimezone(db_utils.local_tz()).strftime("%d %b %Y, %H:%M")
                    if a["kind"] == "quiz":
                        if a["score"] is not None and a["total"]:
                            detail = f"{a['score']} of {a['total']} ({round(100 * a['score'] / a['total'])}%)"
                        else:
                            detail = f"{a['count']} questions, not finished"
                    elif a["kind"] == "flashcards":
                        detail = f"{a['count']} cards"
                    else:
                        detail = ""
                    row = st.columns([1.3, 2, 2.2, 1])
                    row[0].markdown(f"**{kind_label[a['kind']]}**")
                    row[1].caption(when)
                    row[2].markdown(detail)
                    if row[3].button("Open", key=f"open_{a['id']}", use_container_width=True):
                        open_activity(a["id"])
                        st.rerun()

has_full_session = bool(
    st.session_state.summary_text
    and st.session_state.quiz_data
    and st.session_state.flashcards_data
)

if has_full_session or st.session_state.quiz_data or st.session_state.flashcards_data or st.session_state.summary_text:
    with toolbar_placeholder:
        with st.popover("Export", icon=":material/download:"):
            if st.session_state.quiz_data:
                st.download_button(
                    "Quiz (CSV)",
                    icon=":material/download:",
                    data=quiz_to_csv(st.session_state.quiz_data),
                    file_name="quiz.csv",
                    mime="text/csv",
                    use_container_width=True,
                )
            if st.session_state.flashcards_data:
                st.download_button(
                    "Flashcards (CSV)",
                    icon=":material/download:",
                    data=flashcards_to_csv(st.session_state.flashcards_data),
                    file_name="flashcards.csv",
                    mime="text/csv",
                    use_container_width=True,
                )
            if st.session_state.summary_text:
                try:
                    pdf_bytes = session_to_pdf(
                        st.session_state.notes_text,
                        st.session_state.summary_text,
                        st.session_state.quiz_data,
                        st.session_state.flashcards_data,
                    )
                except Exception:
                    st.button("Session (PDF)", disabled=True, use_container_width=True)
                    st.caption("PDF export failed for this session.")
                else:
                    st.download_button(
                        "Session (PDF)",
                        icon=":material/picture_as_pdf:",
                        data=pdf_bytes,
                        file_name="study_session.pdf",
                        mime="application/pdf",
                        use_container_width=True,
                    )
