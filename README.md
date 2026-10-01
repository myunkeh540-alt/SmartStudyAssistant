# Smart Study Assistant

A Streamlit dashboard where you paste or upload study notes and get an
**AI-generated** summary, quiz, and flashcards (via the OpenAI API), with
session history saved locally and export to CSV/PDF.

Navigate between Study Material, Summary, Quiz, Flashcards, and History from
the left sidebar. Summaries are organized with headings and bullet points
covering only what's in your material; quiz questions and flashcards are
generated only from your material too, in whatever quantity you ask for (up
to 50). The quiz is AI-graded — answer every question, submit once, and get
a score with per-question feedback.

Supported upload types: TXT, PDF, DOCX, PPTX, CSV, XLSX, Markdown, and
images (PNG/JPG/JPEG). Uploaded documents keep their structure (headings,
bullet points, tables) as Markdown where the source format supports it.
Image uploads are read with the OpenAI vision model, so they require an API
key (see below) — other file types work without one.

## Files

- `app.py` — the Streamlit app (sidebar navigation, dashboard layout, all five sections).
- `styles.css` — the app's custom styling (fonts, colors, cards, buttons).
- `study_utils.py` — text extraction from uploaded files (TXT, PDF, DOCX, PPTX, CSV, XLSX, Markdown, preserving structure as Markdown; routes images to `ai_utils`).
- `ai_utils.py` — OpenAI-backed summary/quiz/flashcard generation and quiz grading.
- `db_utils.py` — SQLite persistence for saved study sessions and anonymous visitor activity.
- `admin_page.py` — password-protected admin view of every visitor's activity.
- `export_utils.py` — CSV and PDF export helpers.
- `requirements.txt` — Python dependencies.

## 1. Set up your OpenAI API key

Copy `.env.example` to `.env` and fill in your key:

```bash
cp .env.example .env
```

```
OPENAI_API_KEY=sk-...
```

Get a key from the [OpenAI dashboard](https://platform.openai.com/api-keys).
Without a key set, the app still runs but shows a message asking you to add
one before it will generate a summary, quiz, or flashcards.

`.env` is git-ignored — your key never gets committed.

## 2. Run the app locally

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Streamlit will print a local URL, usually `http://localhost:8501`. Open it in your browser.

On first generation, the app creates a local `study_assistant.db` SQLite file
(also git-ignored) to store your saved sessions, viewable in the **History**
section of the sidebar.

### Seeing everyone's activity (admin)

Each browser gets a random anonymous ID stored in a cookie, and each person's
History shows only their own activity. To see all visitors:

1. Add `ADMIN_PASSWORD=your-password` to `.env` and restart the app.
2. Open `http://localhost:8501/?admin` and enter the password.

You get totals, a per-visitor table (visits, summaries, quizzes, average score,
flashcard sets) and a filterable activity log. Clearing cookies or switching
browser/device counts as a new visitor. Activity saved before tracking was added
appears as `legacy`. Visitors are only recorded in the database of the machine
running the app, so this works while everyone uses your ngrok link (section 3).

### Hosting on Streamlit Community Cloud (shared database)

A hosted copy loses local files when it restarts, and a copy on your PC and a copy
in the cloud each have their own SQLite file, so the admin page can't see across
them. Point every copy at one Postgres database instead:

1. Create a free project at [supabase.com](https://supabase.com) (or Neon).
2. In Supabase, click **Connect** and copy the **Session pooler** connection string
   (the direct connection is IPv6-only and fails on Streamlit Cloud). Put your
   database password in it.
3. In Streamlit Cloud, open your app, then **Settings > Secrets**, and add:

```toml
DATABASE_URL = "postgresql://postgres.xxxx:YOUR-PASSWORD@aws-0-xx.pooler.supabase.com:5432/postgres"
ADMIN_PASSWORD = "your-admin-password"
APP_TIMEZONE = "Asia/Kuala_Lumpur"
OPENAI_API_KEY = "sk-..."
```

4. Open `https://your-app.streamlit.app/?admin`. Tables are created automatically.

To see the same data from your PC's admin page, put the same `DATABASE_URL` in your
local `.env`. With `DATABASE_URL` empty the app uses the local SQLite file.

## 3. Expose it publicly with ngrok

1. [Download ngrok](https://ngrok.com/download) and unzip it somewhere on your PC.
2. With the Streamlit app still running on port 8501, open a **new** terminal and run:

```bash
ngrok http 8501
```

3. ngrok will print a public `https://....ngrok-free.app` URL that forwards to your local app. Share that URL to let others access your app while it's running.

Stop sharing by pressing `Ctrl+C` in the ngrok terminal (or closing Streamlit).

## 4. Configure your ngrok authtoken safely

Free ngrok accounts need an authtoken (from your [ngrok dashboard](https://dashboard.ngrok.com/get-started/your-authtoken)) before `ngrok http` will work.

Set it up **once**, outside the project folder, so it never ends up in your code or git history:

```bash
ngrok config add-authtoken YOUR_TOKEN_HERE
```

This saves the token to ngrok's own config file (e.g. `%USERPROFILE%\AppData\Local\ngrok\ngrok.yml` on Windows), not to any file in this project.
