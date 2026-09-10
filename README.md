# email-automation

Flask web app that automates sending work-assignment emails via the Gmail API, with map generation for assignment locations.

## Setup

1. Install dependencies: `pip install -r requirements.txt`
2. Set the `FLASK_SECRET_KEY` environment variable (generate one with `python -c "import secrets; print(secrets.token_hex(32))"`).
3. Place your Google OAuth `credentials.json` (Gmail API) in the project root, or point to it via the `GOOGLE_CLIENT_SECRETS_FILE` environment variable.
4. Run `python app.py`.

## Notes

`credentials.json`, `uploads/`, and generated map files are intentionally excluded from version control since they contain secrets and personal/work data.
