name: Improve Learning App
description: Improve the usability, learning flow, interaction design, and professional UI of a student learning or study application.
---

# Improve Learning App

Use this skill when improving a study, education, quiz, flashcard, note-taking, or learning application.

## Goal

Make the application:
- easy to understand
- easy to navigate
- interactive
- visually professional
- useful for actual studying
- simple rather than feature-heavy

## Workflow

Before making changes:

1. Inspect the existing application.
2. Identify the current pages and major features.
3. Identify usability problems.
4. Explain the proposed changes briefly.
5. Preserve existing working features.
6. Implement improvements incrementally.
7. Test after every major change.

## General UI Guidelines

- Use a clear left-side navigation when appropriate.
- Make the active page obvious.
- Use consistent spacing and typography.
- Use cards to separate important content.
- Avoid unnecessary visual decoration.
- Avoid excessive gradients.
- Avoid emojis unless explicitly requested.
- Keep primary actions visually stronger than secondary actions.
- Provide helpful empty states.
- Keep layouts suitable for laptop and desktop use.
- Avoid overcrowding the page.

## Study Material

When an application accepts learning material:

- Clearly show the active source.
- Support paste text and file upload where relevant.
- Display useful source information such as filename, file type, and word count.
- Show a clear state when material is successfully loaded.
- Make it obvious what the user should do next.

## Summary

Do not present summaries as one long text block.

Prefer structured sections such as:

- Key Points
- Important Definitions
- Important Concepts
- Formulas
- Examples
- Exam Focus
- Things to Remember

Use cards, headings, or expandable sections when useful.

## Quiz Experience

Prefer an interactive tutor experience over displaying many questions at once.

When appropriate:

- Ask one question at a time.
- Allow free-text answers.
- Evaluate the student's response.
- Give concise feedback.
- Allow Hint, Explain, Skip, and Next Question.
- Allow the learner to request easier or harder questions.
- Allow topic-specific requests.
- Maintain quiz progress during the session.

Do not reveal the answer before the student attempts the question unless requested.

## Flashcards

Flashcards should behave like real study cards.

Prefer:

- one card at a time
- question on the front
- answer on reveal
- previous and next controls
- shuffle
- progress counter
- "I Know" and "I Don't Know"
- simple card-centered layout

Avoid presenting flashcards only as a long accordion list.

## History

For saved learning sessions, show useful metadata:

- topic/title
- date
- source
- quiz count
- flashcard count

Provide an obvious way to reopen or resume a session.

## Export Features

Export functions should remain available but should not dominate the main interface.

Prefer grouping them under:
- Export
- Download
- More actions

unless exporting is the application's primary purpose.

## Code Changes

- Keep implementation beginner-friendly when the existing project is beginner-oriented.
- Reuse existing components and functions when possible.
- Do not introduce frameworks or dependencies unless clearly useful.
- Avoid rewriting working code unnecessarily.
- Test existing behavior before and after changes.
- Explain significant architectural changes.