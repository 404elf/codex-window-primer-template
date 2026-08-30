# Codex Window Primer agent rules

- This repository is public source. Never add `auth.json.enc`, OAuth tokens, refresh tokens, account identifiers, or an age private key.
- A user must copy the template into a separate Private runtime repository before adding credentials.
- For natural-language schedule requests in a deployed private copy, Codex directly updates `schedule.json` and all marked workflow cron entries together; do not ask the user to hand-edit schedule JSON, workflow YAML, cron, or UTC values.
- Prefer v2 `weekly` and `dates` rules: `override` replaces, `extra` appends, and `cancel` limits its effect to the stated slot/date. Interpret “改成” as replacement and “再加一个/还要” as addition.
- Never read, print, copy, or include authentication files in prompts, commits, Issues, Pull Requests, or logs.
