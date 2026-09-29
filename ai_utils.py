"""
OpenAI-backed generation for Smart Study Assistant.

Replaces the old rule-based demo logic with real AI-generated
summaries, quizzes, and flashcards.
"""

import base64
import json
import os

from openai import OpenAI, OpenAIError

DEFAULT_MODEL = "gpt-4o-mini"

IMAGE_MIME_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}


class AIGenerationError(Exception):
    """Raised when the AI call fails, with a message safe to show to the user."""


def is_api_key_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def _client() -> OpenAI:
    return OpenAI()


def _model() -> str:
    return os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)


def _call(**kwargs):
    try:
        return _client().chat.completions.create(model=_model(), **kwargs)
    except OpenAIError as e:
        raise AIGenerationError(f"AI request failed: {e}") from e


SUMMARY_SECTIONS = ["key_points", "definitions", "important_concepts", "formulas", "exam_focus"]


def generate_ai_summary(text: str) -> dict:
    """
    Returns a dict with keys SUMMARY_SECTIONS, each a list of short strings.
    A section is an empty list when it genuinely doesn't apply to the material
    (e.g. formulas for a history text) rather than invented content.
    """
    response = _call(
        messages=[
            {
                "role": "system",
                "content": (
                    "You identify the most important information in study notes for a "
                    "student. You do not simply shorten or truncate the text - you find "
                    "the key ideas, facts, and terms. You use only the information given "
                    "to you; you never add outside facts or context. "
                    'Respond only with JSON of the form {"key_points": [...], '
                    '"definitions": [...], "important_concepts": [...], "formulas": [...], '
                    '"exam_focus": [...]}, where each value is a list of short strings. '
                    "If a section genuinely doesn't apply to this material (e.g. formulas "
                    "for a non-technical text), return an empty list for it rather than "
                    "inventing content."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Organize the most important information from the following study "
                    "notes into these five sections: key_points (the main ideas), "
                    "definitions (important terms and their meanings), "
                    "important_concepts (concepts a student must understand), formulas "
                    "(any formulas/equations present), and exam_focus (what's most "
                    "likely to be tested). Base this entirely on the notes below - do "
                    "not add anything not present in them:\n\n" + text
                ),
            },
        ],
        response_format={"type": "json_object"},
    )
    data = json.loads(response.choices[0].message.content)
    return {key: data.get(key) or [] for key in SUMMARY_SECTIONS}


def _summary_as_text(summary: dict | None) -> str:
    if not summary:
        return ""
    lines = []
    for key in SUMMARY_SECTIONS:
        items = summary.get(key) or []
        if items:
            lines.append(key.replace("_", " ").title() + ":")
            lines.extend(f"- {item}" for item in items)
    return "\n".join(lines)


def _valid_mcq(q: dict) -> dict | None:
    """Keeps a question only if it has 4 distinct choices and `answer` is one of them."""
    choices = [str(c).strip() for c in q.get("choices") or []]
    answer = str(q.get("answer", "")).strip()
    if len(choices) != 4 or len(set(choices)) != 4 or answer not in choices:
        return None
    if not str(q.get("question", "")).strip():
        return None
    return {
        "question": str(q["question"]).strip(),
        "choices": choices,
        "answer": answer,
        "explanation": str(q.get("explanation", "")).strip(),
        "hint": str(q.get("hint", "")).strip(),
        "topic": str(q.get("topic", "")).strip() or "General",
    }


def generate_ai_quiz(text: str, num_questions: int = 5, summary: dict | None = None) -> list[dict]:
    """
    Multiple-choice questions: [{question, choices (4), answer, explanation, hint, topic}].
    `answer` is the text of the correct choice, so exports keep working unchanged.
    """
    summary_text = _summary_as_text(summary)
    material = "STUDY NOTES:\n" + text
    if summary_text:
        material += (
            "\n\nSUMMARY OF THE NOTES (use it to prioritise the most important "
            "ideas and what is likely to be tested):\n" + summary_text
        )
    response = _call(
        messages=[
            {
                "role": "system",
                "content": (
                    "You write multiple-choice quiz questions strictly based on the study "
                    "material a student gives you - never introduce facts or topics that "
                    "aren't in that material. Each question has exactly four distinct "
                    "answer choices, exactly one of which is correct, and plausible "
                    "wrong choices. 'answer' must be copied exactly from one of the "
                    "choices. 'explanation' is one or two short sentences saying why the "
                    "answer is correct. 'hint' nudges toward the answer without giving it "
                    "away. 'topic' is a short label (2-4 words) for the part of the "
                    "material the question covers. Respond only with JSON of the form "
                    '{"questions": [{"question": "...", "choices": ["...", "...", "...", '
                    '"..."], "answer": "...", "explanation": "...", "hint": "...", '
                    '"topic": "..."}, ...]}.'
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Write exactly {num_questions} multiple-choice questions covering "
                    "the key facts in the material below. Base every question, choice "
                    "and explanation only on this material:\n\n" + material
                ),
            },
        ],
        response_format={"type": "json_object"},
    )
    data = json.loads(response.choices[0].message.content)
    questions = [q for q in (_valid_mcq(q) for q in data.get("questions", [])) if q]
    if not questions:
        raise AIGenerationError("The AI response didn't contain usable questions. Try again.")
    return questions[:num_questions]


def generate_ai_flashcards(text: str, num_cards: int = 5) -> list[dict]:
    response = _call(
        messages=[
            {
                "role": "system",
                "content": (
                    "You turn study notes into flashcards, strictly based on the material "
                    "a student gives you - never introduce facts or topics that aren't in "
                    "that material. Each flashcard has one clear term or question on the "
                    "front and one concise answer on the back. "
                    'Respond only with JSON of the form '
                    '{"flashcards": [{"front": "...", "back": "..."}, ...]}.'
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Create exactly {num_cards} flashcards (front = one clear term or "
                    "question, back = one concise answer) covering the key points in "
                    "these study notes. Base every flashcard only on the material "
                    "below:\n\n" + text
                ),
            },
        ],
        response_format={"type": "json_object"},
    )
    data = json.loads(response.choices[0].message.content)
    cards = data.get("flashcards", [])
    if not cards:
        raise AIGenerationError("The AI response didn't contain any flashcards. Try again.")
    return cards


def grade_quiz(quiz: list[dict], user_answers: list[str]) -> list[dict]:
    """
    Grades a completed quiz in one AI call. Returns a list (same order/length as
    `quiz`) of {"correct": bool, "feedback": str}. The overall score should be
    computed by the caller from the "correct" booleans, not asked of the model.
    """
    items = [
        {
            "question": q["question"],
            "reference_answer": q["answer"],
            "user_answer": a,
        }
        for q, a in zip(quiz, user_answers)
    ]

    response = _call(
        messages=[
            {
                "role": "system",
                "content": (
                    "You grade a student's quiz answers against reference answers. "
                    "Be reasonably lenient: give credit for answers that are correct in "
                    "substance even if worded differently from the reference answer. "
                    'Respond only with JSON of the form {"results": '
                    '[{"correct": true|false, "feedback": "..."}, ...]}, with exactly '
                    "one result per item, in the same order as the input list. Feedback "
                    "should be one short sentence."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Grade each of these question/reference-answer/user-answer items:\n\n"
                    + json.dumps(items, ensure_ascii=False)
                ),
            },
        ],
        response_format={"type": "json_object"},
    )
    data = json.loads(response.choices[0].message.content)
    results = data.get("results", [])
    if len(results) != len(quiz):
        raise AIGenerationError("The AI grading response didn't match the quiz. Try again.")
    return results


def extract_text_from_image(uploaded_file) -> str:
    if not is_api_key_configured():
        raise AIGenerationError("An OpenAI API key is required to read text from images.")

    ext = uploaded_file.name.rsplit(".", 1)[-1].lower() if "." in uploaded_file.name else ""
    mime = IMAGE_MIME_TYPES.get(ext, "image/png")
    image_b64 = base64.b64encode(uploaded_file.getvalue()).decode("utf-8")
    data_url = f"data:{mime};base64,{image_b64}"

    response = _call(
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Transcribe all readable text from this image. Preserve its "
                            "structure as Markdown as much as possible (headings, bullet "
                            "points, paragraphs, line breaks). Return only the "
                            "transcription, no commentary."
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
    )
    return response.choices[0].message.content.strip()
