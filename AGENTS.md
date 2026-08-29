# Codex Window Primer agent rules

- This repository is public source. Never add `auth.json.enc`, OAuth tokens, refresh tokens, account identifiers, or an age private key.
- A user must copy the template into a separate Private runtime repository before adding credentials.
- For natural-language schedule requests in a deployed private copy, use `python tools/codex_window.py`; do not hand-edit schedule JSON, workflow YAML, cron, or UTC values.
- Never read, print, copy, or include authentication files in prompts, commits, Issues, Pull Requests, or logs.
