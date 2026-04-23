# Security Policy

## API Key Safety

This project **never stores API keys in the repository**. 

- API keys are stored in `~/.pdf-labeler/settings.json` on your local machine only
- This file is listed in `.gitignore` and will never be committed
- Your API key is only used locally to make requests to Gemini/Claude/Tesseract APIs

## Reporting Vulnerabilities

If you discover a security vulnerability, please email instead of using the issue tracker.

## Dependencies

Dependencies are listed in `requirements.txt`. Consider using:
```bash
pip list --outdated
```

to check for security updates periodically.
