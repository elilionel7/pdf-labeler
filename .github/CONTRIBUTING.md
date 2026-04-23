# Contributing to PDF Labeler

Thanks for your interest in contributing! Here are some guidelines:

## Setup

1. Clone the repo
2. Create a virtual environment: `python3 -m venv .venv`
3. Activate it: `source .venv/bin/activate`
4. Install deps: `pip install -r requirements.txt`
5. Run the app: `python3 app.py`

## API Keys

API keys are stored in **`~/.pdf-labeler/settings.json`** — NOT in the repo. This keeps secrets safe and out of version control.

## Testing

Run the existing tests before submitting a PR:
```bash
python3 -m pytest test_*.py
```

## Pull Requests

- Keep changes focused and well-tested
- Update the README if you add new features
- Test with at least one reader (Gemini, Claude, or Tesseract)

## Issues

Report bugs or suggest features via GitHub Issues. Include:
- Steps to reproduce
- Expected vs. actual behavior
- Which reader you're using (Gemini/Claude/Tesseract)
