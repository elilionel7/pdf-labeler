# PDF Labeler

A small macOS desktop app that reads scanned (even handwritten!) PDFs,
figures out the file type (PCS / IHR / Trans / TS), the person's name, and
the service date(s), then renames each file according to your naming
protocol:

```
Client Firstname Lastname_Type_Date.pdf      (PCS / IHR / Trans)
Employee Firstname Lastname_TS_Date.pdf       (Timesheets)
```

Date rules:

| Case                 | Example                          |
| -------------------- | -------------------------------- |
| Single day           | `Kofi Jamar_PCS_2-23-26.pdf`     |
| Multiple days, 1 mo. | `Kofi Jamar_PCS_2-23,24,26-26.pdf` |
| Multiple months      | `Kofi Jamar_PCS_2-23,24_3-1,2-26.pdf` |

You always review (and can edit) the suggested name before anything is
renamed.

---

## 1. Choose a reader

The app has three "readers" — pick one in **Settings**:

| Reader | Handles handwriting? | Cost | Setup |
| ------ | -------------------- | ---- | ----- |
| **Gemini** (Google AI Studio) | Yes — excellent | **Free** (1,500 req/day) | Paste an API key |
| **Claude** (Anthropic)         | Yes — excellent | Pay-as-you-go (~1-3¢ per PDF) | Paste an API key |
| **Tesseract** (offline)        | No — printed text only | Free | `brew install tesseract poppler` |

Recommended: **Gemini** — it's free, handles handwriting, and needs no
credit card.

---

## 2. One-time setup on macOS

Open Terminal and run these once.

```bash
cd ~/Desktop/pdf-labeler

# Python virtual env (only needed the first time)
python3 -m venv .venv
source .venv/bin/activate

# Install packages
pip install -r requirements.txt

# (Optional) if you want the offline Tesseract fallback:
brew install tesseract poppler

# (Optional) if you want the Claude backend:
pip install anthropic
```

Tkinter (the window library) ships with Python, but Homebrew's Python
needs one extra package:

```bash
brew install python-tk
```

### Get a free Gemini API key

1. Go to **https://aistudio.google.com/apikey**
2. Sign in with any Google account.
3. Click **Create API key** → copy it.

---

## 3. Run the app

```bash
cd ~/Desktop/pdf-labeler
source .venv/bin/activate
python3 app.py
```

Or, to launch it like a normal Mac app (Finder, Spotlight, Dock), run
`./make_app.sh` once. It creates `~/Applications/PDF Labeler.app`.
Your code changes take effect the next time you open the app, so you
don't need to rebuild it. Errors are written to
`~/.pdf-labeler/app.log`.

First run:

1. Click **Settings…** (top right).
2. Select **Google Gemini**.
3. Paste your API key → **Save**.

After that it's remembered in `~/.pdf-labeler/settings.json`.

---

## 4. Daily workflow

1. Click **Add PDFs** or **Add Folder** to load scans.
2. Each file is analysed in the background. A proposed filename appears
   in the **Proposed filename** column.
3. Double-click any cell in **Name**, **Type**, or **Dates** to correct
   it. Dates use `M/D/YY` separated by commas:
   `3/23/26, 3/24/26, 4/1/26`.
4. Click **Apply Rename**. Files are renamed in place inside their
   original folder. If a target name already exists, the app appends
   `(2)`, `(3)`, … so nothing is overwritten.

Rows are colour-coded:

- green = ready to rename
- blue = already renamed
- red = error (look at the Status column)

### Progress, timer and metrics

The strip above the table shows **Remaining: 9 / 10** for the current
batch, a work timer, and running totals.

- **Work timer** starts when you add files. Click **Pause Timer** when
  you take a break. It resumes on its own as soon as you select, edit or
  rename a file, so forgetting to press Resume doesn't lose time. It stops
  when every file in the batch is renamed. (**Pause AI** is separate: it
  only pauses background reading.)
- **AI read** is the time the reader itself spent on each file. It is
  measured whether or not the work timer is running.
- **Review** is the time each file spent open in the preview pane while
  the timer was running.
- **Edited** is how many renamed files you changed from the AI's
  suggestion.

When a batch finishes, the app shows a summary and appends a record to
`~/.pdf-labeler/metrics.jsonl`. That is one JSON line per batch with
totals and per-file timings. It contains no names or file paths. Clearing
a half-finished batch also logs it, with `"completed": false`.

---

## 5. Files in this project

| File              | Purpose                                            |
| ----------------- | -------------------------------------------------- |
| `app.py`          | Tkinter GUI — the thing you run                    |
| `ai_reader.py`    | Gemini / Claude vision reader (handles handwriting)|
| `ocr.py`          | Tesseract fallback (printed text only)             |
| `extractor.py`    | Regex patterns used with the Tesseract fallback    |
| `formatter.py`    | Builds the final filename per the naming protocol  |
| `test_*.py`       | Unit tests — `python3 -m unittest`                 |

---

## 6. Troubleshooting

**Gemini: "API key not valid"** — Re-copy the key from AI Studio. Make
sure there are no extra spaces.

**Gemini: quota exceeded** — Free tier is 15 requests/minute, 1,500
per day. If you hit it, wait a minute (or a day) and try again.

**`ModuleNotFoundError: google.generativeai`** — Run
`pip install google-generativeai` inside the virtual environment.

**`ModuleNotFoundError: _tkinter`** — Run `brew install python-tk`.

**Handwriting comes back mis-read** — Open the PDF, check if the scan is
rotated or faint. You can also just edit the row inline and hit Apply.

**Costs** — Gemini Flash free tier covers far more than most users need.
With Claude, each PDF is about 1–3 cents depending on page count.
