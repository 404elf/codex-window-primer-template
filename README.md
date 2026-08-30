# Codex Window Primer

## 中文

将 Codex 的 5 小时 usage window 与用户的工作时间对齐。

这是一个非官方社区项目：它只调整 5 小时窗口的开始时间，不增加 5 小时或 weekly usage quota，不绕过限额，也不破解限流。This is an unofficial community project and is not affiliated with or endorsed by OpenAI.

### 工作原理

```text
用自然语言告诉 Codex 工作时间
        ↓
Codex 写入多时段 schedule.json
        ↓
私有 GitHub Actions 按生成的本地时区 cron entries 唤醒
        ↓
一次最小的 OAuth Codex 请求
        ↓
更早启动 5 小时窗口
```

默认 5 小时窗口、开工后 1 小时 30 分刷新：

prime_time(slot) = work_start(slot) - (window_duration - reset_after_start(slot))

例如 10:00 开工 → 06:30 prime → 约 11:30 刷新。GitHub Actions 是尽力调度，可能有延迟，不保证秒级精度。

### v2 多时段计划

schedule.json 是唯一业务 source of truth。weekly 的键为 0=周一 到 6=周日，每天可以有任意多个 HH:MM slot；slot 对象还可以单独覆盖 reset 延迟。dates 的规则优先于 weekly：override 替换当天 slot，extra 追加 slot，cancel 可取消指定时间或整天。prime 跨午夜时，cron 使用前一天的本地日期唤醒。

旧版 v1 的 mode、work_start_local 和 skip_dates_local 仍可直接读取，不需要用户手工迁移。

### 快速开始

1. 使用本模板创建一个 Private runtime repository，不能把该运行仓库公开。
2. 在可信电脑上安装 Git、Python 3.11+、官方 age、GitHub CLI 和 Codex CLI。
3. 按 docs/bootstrap-windows.md 完成一次隔离登录、加密 OAuth 状态和 GitHub Secret 设置；不要使用 API key。
4. 用自然语言告诉 Codex 工作安排。Codex 会更新 schedule.json 和所有标记 cron entries，并核对远端配置。
5. 手动运行 workflow 两次，每次等待前一次结束，并检查日志没有秘密。

安装完成后，用户不需要手工编辑 YAML、cron、UTC、timezone 或认证文件。

### 自然语言控制

- “每天 9 点和晚上 8 点” → 每天两个 slot
- “工作日 9 点，周末 11 点” → 工作日/周末规则
- “明天 10 点开工” → 指定日期的一次性 slot
- “再加一个 / 也开工” → 追加已有计划，不替换
- “改成下午 2 点” → 替换相关范围
- “今天取消” → 只取消今天的 occurrence
- “暂停 / 恢复” → 禁用/恢复现有计划
- “看看现在安排了什么” → 查看有效 slot 并验证远端 workflow

如果用户明确说今天临时开工或马上开工，Codex 会比较本地 prime 与当前时间：prime 未到则使用正常 cron；prime 已过且用户仍要今天工作，则保留日期计划、不生成过期日期 cron，并通过现有 workflow_dispatch 做一次 best-effort prime，同时说明原定 reset 目标已无法满足、实际窗口从 dispatch 时间开始。不会新增 CLI、服务器或轮询。

### 公共代码与私有状态

公开仓库只放源代码、workflow 模板、安装工具和文档。每位用户都应创建自己的私有 runtime repository。真实 auth.json、OAuth access/refresh token、账户信息、age 私钥和解密后的凭据绝不能进入公开仓库、Issue、PR、prompt 或日志。

### 安全与限制

workflow 只有 schedule 和 workflow_dispatch 触发器，使用单一 concurrency 防止 OAuth refresh 并发。Codex 在隔离的 CODEX_HOME 中运行，只有解密步骤接触 AGE_PRIVATE_KEY；checkout 不持久化 GitHub token，刷新后的加密状态才由最后步骤保存。

GitHub schedule 只是唤醒机制，可能延迟；一次性日期 cron 可能按日/月再次唤醒，但 gate 会阻止过期日期真正执行。项目不会增加 Codex quota。

## English

Align your Codex five-hour usage window with your work schedule.

This small community project sends one minimal Codex request before a planned work session. It changes when the rolling five-hour window starts; it does **not** increase the five-hour or weekly usage quota, bypass limits, or defeat rate limiting.

> This is an unofficial community project and is not affiliated with or endorsed by OpenAI.

## The simple idea

```text
Tell Codex your work time
          ↓
Codex writes the structured multi-slot plan
          ↓
Private GitHub Actions wakes at the generated local-time cron entries
          ↓
One tiny OAuth-authenticated Codex request
          ↓
The next five-hour window starts earlier
```

With the default five-hour window and a 1h30m target reset delay:

`prime_time(slot) = work_start(slot) - (window_duration - reset_after_start(slot))`

So 10:00 work start → 06:30 prime → approximately 11:30 reset. GitHub Actions scheduling is best-effort, so this is not a second-level timer.

## The schedule model

Version 2 keeps `schedule.json` as the only business source of truth. A plan
can contain any number of slots on each weekday and dated rules such as:

```json
{
  "version": 2,
  "enabled": true,
  "timezone": "Asia/Shanghai",
  "window_duration_minutes": 300,
  "reset_after_start_minutes": 90,
  "active_from_local": null,
  "active_until_local": null,
  "weekly": {
    "0": ["09:00", "20:00"],
    "1": ["09:00", "20:00"],
    "2": ["09:00", "20:00"],
    "3": ["09:00", "20:00"],
    "4": ["09:00", "20:00"],
    "5": ["11:00"],
    "6": ["11:00"]
  },
  "dates": {
    "2030-01-07": {"mode": "override", "slots": ["14:00"]},
    "2030-01-08": {"mode": "extra", "slots": ["22:00"]},
    "2030-01-09": {"mode": "cancel", "slots": ["09:00"]},
    "2030-01-10": {"mode": "cancel"}
  }
}
```

`weekly` uses Python weekday numbers: `0` is Monday and `6` is Sunday.
For a date, `override` replaces the weekly slots, `extra` appends slots, and
`cancel` with a slot list removes only those clock times; `cancel` without
`slots` cancels the whole day. A slot object can override the default reset
delay, for example `{ "time": "20:00", "reset_after_start_minutes": 120 }`.
The date rules take precedence over recurring rules, so “today temporarily
starts at 14:00” is represented as a dated rule. A prime time crossing
midnight belongs to the previous local calendar date and is scheduled that
way.

The older v1 fields (`mode`, `work_start_local`, and `skip_dates_local`) are
still accepted and normalized to the equivalent v2 plan. No credential or
manual migration step is required.

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
4. Tell Codex your work time. Codex updates the structured plan and all marked cron/timezone entries, commits and pushes the change, and verifies the remote workflow.
5. Run the workflow manually twice, waiting for each run to finish. Check that both runs succeed and that no secret appears in the logs.

After setup, users should not edit YAML, cron, timezone values, or authentication files. Codex is the normal human-facing control entry point and directly maintains the two ordinary schedule files.

## Everyday control through Codex

Examples of intent and the operation Codex should perform:

| You say | Result |
| --- | --- |
| “明天 10 点开工” | dated one-time slot, default 1h30m reset target |
| “每天 9 点和晚上 8 点” | two slots on every weekday |
| “工作日 9 点，周末 11 点” | weekday/weekend slots |
| “改成下午 2 点” | replace the relevant existing slot or rule |
| “再加一个晚上 8 点” | append an extra slot in the same scope |
| “明天希望开工 2 小时后刷新” | recompute that slot with a 2h reset target |
| “今天取消” | cancel only today's occurrence |
| “这周每天 9 点开始” | recurring daily plan for the requested period |
| “暂停” / “恢复” | disable or re-enable the existing plan |
| “看看现在安排了什么” | show all effective slots and verify the remote workflow |

It supports daily multi-slot plans, workdays, weekends, selected weekdays,
dated one-offs, overrides, additions, day/slot cancellations, temporary
plans, and prime times crossing midnight. “改成” means replacement, “再加一
个/还要” means addition, and “取消” is limited to the stated date, slot, or
plan scope. Codex keeps the intent in `schedule.json` and updates the marked
cron projection. GitHub's native IANA timezone keeps recurring plans at the
requested local time across DST changes.

“也开工” is also additive: it keeps the existing slots and adds the new one.

### A same-day request after its prime time

For an explicit “today temporary start” or “start as soon as possible” request,
Codex compares the local prime with the current local time. If the prime is
still ahead, it saves the dated rule and uses the normal cron path. If the
prime has passed and the wording clearly means that work should still happen
today, Codex keeps the dated plan, omits the expired date cron, and invokes the
existing `workflow_dispatch` once as a best-effort prime. It reports that the
original reset target can no longer be met and that this run's actual window
starts at dispatch time. No extra workflow, CLI, server, or polling loop is
introduced.

## Why OAuth state is saved

Codex OAuth may refresh or rotate credentials while a run is active. The workflow decrypts the private repository's encrypted bundle into a fresh runner-local `CODEX_HOME`, runs one request, detects a changed file, encrypts it again, and pushes only the ciphertext. This prevents the next run from receiving a stale refresh token. The plaintext file and temporary key are removed when the job exits.

The workflow uses a single concurrency group so two runs cannot refresh the same OAuth state at once. It has only `schedule` and `workflow_dispatch` triggers. There are no Pull Request, Issue, push, reusable-workflow, or external-input credential paths. Multiple cron entries are wake-ups only: the gate compares the event's cron text with the effective slot, so a wake-up for another slot cannot send a duplicate request.

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
- The job has a 10-minute ceiling and the Codex command has a separate 3-minute timeout. A timed-out request still reaches OAuth-state inspection and persistence; the final step then reports the request failure.

## Change, pause, or remove a plan

Tell Codex the new natural-language intent. It must update both schedule files and report every computed local prime time, cron/timezone entry, and remote verification result. `pause` keeps the plan but disables both scheduled and manual execution; `resume` restores its computed cron entries. `cancel` without a date pauses the whole plan; with a date it creates a dated cancellation, and with a time it cancels only that slot.

To uninstall, first pause the plan, wait for any active run to finish, remove the GitHub Secret and encrypted bundle, then delete the private runtime repository. Delete the public template only if you also want to remove the source; no credential is stored there.

## Recovery and FAQ

**The first run says the bundle or Secret is missing.** Confirm that the runtime repository is private, `AGE_PRIVATE_KEY` exists as an Actions Secret, and `auth.json.enc` is present. Do not print either value.

**The OAuth login has expired.** Pause the plan, repeat the isolated login and encryption steps in [docs/bootstrap-windows.md](docs/bootstrap-windows.md), replace the ciphertext and Secret, then test twice again.

**Why not an API key?** This project is designed for the user's ChatGPT/Codex OAuth login state. It does not use OpenAI API billing.

**Why not poll every 30 minutes?** Codex creates only the requested one-time or recurring schedule. There is no background polling loop.

**Why does a one-time plan have a day/month cron?** GitHub Actions has no native one-shot schedule. The calendar gate checks the exact dated slot and prevents later wake-ups from sending a request; the dated rule then expires naturally.

**Why is there a public template and a private repository?** Source can be audited and shared without exposing the per-user OAuth state or refresh-token history.

**What does `RRULE:FREQ=DAILY;COUNT=1` mean?** It means one total occurrence. The `COUNT=1` clause does not mean “run daily”. The cloud workflow uses the structured plan and a marked cron line instead of the paused local automation.

**What happens if another update reaches the private repository first?** The workflow keeps the ordinary remote configuration on the main branch, fails closed, and attempts to save the newly refreshed encrypted state on a private recovery branch. Do not delete that branch until the encrypted state has been safely reconciled.

## Reference audit

The design was reviewed against [`VIEWVIEWVIEW/codex-session-primer`](https://github.com/VIEWVIEWVIEW/codex-session-primer), [`icoretech/codex-action`](https://github.com/icoretech/codex-action), and [`icoretech/codex-docker`](https://github.com/icoretech/codex-docker). It retains encrypted file-backed OAuth, refresh persistence, concurrency, runner isolation, quiet mode, and fixed versions. `codex-action` and `codex-docker` are references only, not dependencies. It intentionally omits periodic polling, multiple model requests, commit-age heuristics, and force-push updates.

See [LICENSE](LICENSE) for the project license and [SECURITY.md](SECURITY.md) for credential-handling and private vulnerability-reporting guidance.
