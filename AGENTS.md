# Codex Window Primer agent rules

- This repository is public source. Never add `auth.json.enc`, OAuth tokens, refresh tokens, account identifiers, or an age private key.
- A user must copy the template into a separate Private runtime repository before adding credentials.
- For natural-language schedule requests in a deployed private copy, Codex directly updates `schedule.json` and all marked workflow cron entries together; do not ask the user to hand-edit schedule JSON, workflow YAML, cron, or UTC values.
- Prefer v2 `weekly` and `dates` rules: `override` replaces, `extra` appends, and `cancel` limits its effect to the stated slot/date. Interpret “改成” as replacement and “再加一个/还要/也开工” as addition.
- For an explicit today-only work request, compare its local prime with the current local time (use `cron_entries(plan, now=...)` for the projection). If it is future, generate the dated cron normally. If it has passed and the wording clearly means work still happens today, save the dated plan without an expired date cron, invoke the existing `workflow_dispatch` once, and tell the user the requested reset target was missed and the actual window starts at dispatch time. Never add a new workflow or polling loop.
- Never read, print, copy, or include authentication files in prompts, commits, Issues, Pull Requests, or logs.
