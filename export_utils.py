"""
CSV and PDF export helpers for Smart Study Assistant.
"""

import csv
import io

from fpdf import FPDF
from fpdf.enums import XPos, YPos


def quiz_to_csv(quiz: list[dict]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Question", "Answer"])
    for item in quiz:
        writer.writerow([item["question"], item["answer"]])
    return buffer.getvalue().encode("utf-8")


def flashcards_to_csv(flashcards: list[dict]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Front", "Back"])
    for card in flashcards:
        writer.writerow([card["front"], card["back"]])
    return buffer.getvalue().encode("utf-8")


def _add_wrapped(pdf: FPDF, text: str) -> None:
    # multi_cell's default new_x leaves the cursor at the right margin, which
    # starves the *next* multi_cell of width - reset to the left margin/next
    # line explicitly so consecutive wrapped lines don't collide.
    pdf.multi_cell(
        0,
        8,
        text.encode("latin-1", errors="replace").decode("latin-1"),
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )


SUMMARY_SECTION_LABELS = {
    "key_points": "Key Points",
    "definitions": "Definitions",
    "important_concepts": "Important Concepts",
    "formulas": "Formulas",
    "exam_focus": "Exam Focus",
}


def session_to_pdf(notes_text: str, summary: dict, quiz: list[dict], flashcards: list[dict]) -> bytes:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Smart Study Assistant - Study Session", ln=True)
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 10, "Summary", ln=True)
    for key, label in SUMMARY_SECTION_LABELS.items():
        items = summary.get(key) or []
        if not items:
            continue
        pdf.set_font("Helvetica", "B", 12)
        _add_wrapped(pdf, label)
        pdf.set_font("Helvetica", "", 11)
        for item in items:
            _add_wrapped(pdf, f"- {item}")
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 10, "Quiz", ln=True)
    pdf.set_font("Helvetica", "", 11)
    for i, item in enumerate(quiz, start=1):
        _add_wrapped(pdf, f"Q{i}. {item['question']}")
        _add_wrapped(pdf, f"A{i}. {item['answer']}")
        pdf.ln(2)
    pdf.ln(2)

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 10, "Flashcards", ln=True)
    pdf.set_font("Helvetica", "", 11)
    for card in flashcards:
        _add_wrapped(pdf, f"Front: {card['front']}")
        _add_wrapped(pdf, f"Back: {card['back']}")
        pdf.ln(2)

    return bytes(pdf.output(dest="S"))
