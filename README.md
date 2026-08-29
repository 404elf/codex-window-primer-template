# Codex Window Primer

Align your Codex five-hour usage window with your work schedule.

This small community project sends one minimal Codex request before a planned work session. It changes when the rolling five-hour window starts; it does **not** increase the five-hour or weekly usage quota, bypass limits, or defeat rate limiting.

> This is an unofficial community project and is not affiliated with or endorsed by OpenAI.

## The simple idea

```text
Tell Codex your work time
          ↓
Codex calculates the prime time
          ↓
Private GitHub Actions wakes at the UTC cron
          ↓
One tiny OAuth-authenticated Codex request
          ↓
The next five-hour window starts earlier
```

With the default five-hour window and a 1h30m target reset delay:

`prime_time = work_start - (window_duration - reset_after_start)`

So 10:00 work start → 06:30 prime → approximately 11:30 reset. GitHub Actions scheduling is best-effort, so this is not a second-level timer.

## Public code and private state

The public repository contains only source code, the workflow template, setup tools, and documentation. Each user creates a separate **Private** runtime repository from this template. Only that private repository may contain:

- the user's encrypted `auth.json.enc` state;
- the `AGE_PRIVATE_KEY` GitHub Actions Secret;
- the user's time plan.

Never put `auth.json`, an OAuth access or refresh token, an account identifier, an age private key, or a decrypted credential in this public repository, an Issue, a Pull Request, a prompt, or a log. Do not run the credential workflow from a public repository.

## 5-minute Quick Start

1. Use this template to create a new **Private** GitHub repository for your runtime. Do not make that runtime repository public.
2. On a trusted computer, install Git, Python 3.11+, the official `age` binary, GitHub CLI, and Codex CLI.
3. Follow [docs/bootstrap-windows.md](docs/bootstrap-windows.md) once. It logs Codex in under an isolated `CODEX_HOME`, encrypts the resulting OAuth file, and adds only the age private key as a GitHub Actions Secret. No API key is used.
4. Tell Codex your work time. Codex updates the structured plan and the one marked cron line, converts Beijing time to UTC, commits and pushes the change, and verifies the remote workflow.
5. Run the workflow manually twice, waiting for each run to finish. Check that both runs succeed and that no secret appears in the logs.

After setup, users should not edit YAML, cron, UTC values, or authentication files. Codex is the normal human-facing control entry point and directly maintains the two ordinary schedule files.

## Everyday control through Codex

Examples of intent and the operation Codex should perform:

| You say | Result |
| --- | --- |
| “明天 10 点开工” | one-time plan, default 1h30m reset target |
| “改成下午 2 点” | update the existing plan and cron |
| “明天希望开工 2 小时后刷新” | recompute prime time with a 2h reset target |
| “这周每天 9 点开始” | recurring daily plan for the requested period |
| “今天取消” | cancel the one-time plan, or skip only today's recurring occurrence |
| “暂停” / “恢复” | disable or re-enable the existing plan |
| “看看现在安排了什么” | show local plan and verify the remote workflow |

It supports one-time, daily, and weekly plans; skips a single date for recurring plans; handles a prime time crossing midnight; and keeps all local-time intent in `schedule.json`. The only workflow edit Codex makes is the marked cron line.

## Why OAuth state is saved

Codex OAuth may refresh or rotate credentials while a run is active. The workflow decrypts the private repository's encrypted bundle into a fresh runner-local `CODEX_HOME`, runs one request, detects a changed file, encrypts it again, and pushes only the ciphertext. This prevents the next run from receiving a stale refresh token. The plaintext file and temporary key are removed when the job exits.

The workflow uses a single concurrency group so two runs cannot refresh the same OAuth state at once. It has only `schedule` and `workflow_dispatch` triggers. There are no Pull Request, Issue, push, reusable-workflow, or external-input credential paths.

The credential-bearing stages are deliberately separated. Only the decrypt
stage receives `AGE_PRIVATE_KEY`; it derives and saves the public age
recipient, then removes the private key before Codex starts. Codex receives a
fresh `CODEX_HOME` and an empty process environment containing only the small
allowlist needed to run. Re-encryption uses the saved public recipient only.
The checkout uses `persist-credentials: false`, so no GitHub token is left in
git configuration or in the Codex process.

Only the final persistence stage receives a temporary `github.token`. It
fetches the newest branch, compares the remote `auth.json.enc` blob with the
blob that was decrypted, and safely rebases the ciphertext-only commit over
ordinary schedule/configuration commits. If the encrypted state changed or a
safe push is impossible, the run fails closed and attempts to preserve the
new encrypted state on a private, run-specific `codex-auth-recovery-*` branch.
It never force-pushes or claims success after a failed persistence operation.

## Security and supply chain

- The official `@openai/codex` CLI is fixed to version `0.150.1` in the workflow.
- The CLI runs one fixed `Reply with exactly: OK` prompt with `gpt-5.6-luna` and low reasoning; output and diagnostics are suppressed.
- The checkout action is pinned to a commit SHA. No remote shell script is executed.
- The runtime workflow uses an isolated `CODEX_HOME`, no `OPENAI_API_KEY`, and only the `contents: write` permission needed to persist encrypted state.
- `AGE_PRIVATE_KEY` exists only in the decrypt step; the Codex step uses an explicit `shell_environment_policy` allowlist and `env -i`.
- `codex-action` and `codex-docker` are audited references only, not runtime dependencies. No remote shell installer is used.
- GitHub Actions may start late. A late run does not create an additional quota; it only shifts the actual prime time.

## Change, pause, or remove a plan

Tell Codex the new natural-language intent. It must update both schedule files and report the computed Beijing prime time, UTC cron, and remote verification result. `pause` keeps the plan but disables both scheduled and manual execution; `resume` restores its computed cron. `cancel` without a date pauses the whole plan; with a date it skips that date for a recurring plan.

To uninstall, first pause the plan, wait for any active run to finish, remove the GitHub Secret and encrypted bundle, then delete the private runtime repository. Delete the public template only if you also want to remove the source; no credential is stored there.

## Recovery and FAQ

**The first run says the bundle or Secret is missing.** Confirm that the runtime repository is private, `AGE_PRIVATE_KEY` exists as an Actions Secret, and `auth.json.enc` is present. Do not print either value.

**The OAuth login has expired.** Pause the plan, repeat the isolated login and encryption steps in [docs/bootstrap-windows.md](docs/bootstrap-windows.md), replace the ciphertext and Secret, then test twice again.

**Why not an API key?** This project is designed for the user's ChatGPT/Codex OAuth login state. It does not use OpenAI API billing.

**Why not poll every 30 minutes?** Codex creates only the requested one-time or recurring schedule. There is no background polling loop.

**Why does a one-time plan have a day/month cron?** GitHub Actions has no native one-shot schedule. The calendar gate checks the exact dated plan and prevents later yearly wake-ups from sending a request; Codex can disable the plan after the intended run.

**Why is there a public template and a private repository?** Source can be audited and shared without exposing the per-user OAuth state or refresh-token history.

**What does `RRULE:FREQ=DAILY;COUNT=1` mean?** It means one total occurrence. The `COUNT=1` clause does not mean “run daily”. The cloud workflow uses the structured plan and a marked cron line instead of the paused local automation.

**What happens if another update reaches the private repository first?** The workflow keeps the ordinary remote configuration on the main branch, fails closed, and attempts to save the newly refreshed encrypted state on a private recovery branch. Do not delete that branch until the encrypted state has been safely reconciled.

## Reference audit

The design was reviewed against [`VIEWVIEWVIEW/codex-session-primer`](https://github.com/VIEWVIEWVIEW/codex-session-primer), [`icoretech/codex-action`](https://github.com/icoretech/codex-action), and [`icoretech/codex-docker`](https://github.com/icoretech/codex-docker). It retains encrypted file-backed OAuth, refresh persistence, concurrency, runner isolation, quiet mode, and fixed versions. `codex-action` and `codex-docker` are references only, not dependencies. It intentionally omits periodic polling, multiple model requests, commit-age heuristics, and force-push updates.

See [LICENSE](LICENSE) for the project license and [SECURITY.md](SECURITY.md) for credential-handling and private vulnerability-reporting guidance.
