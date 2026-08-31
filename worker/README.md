# Cloudflare scheduler PoC

This is a fail-closed public example. Replace the `YOUR_*` placeholders only
in a private deployment and store `GITHUB_TOKEN` as a Cloudflare Worker Secret.
The public `fetch()` handler is health-only. Only `scheduled()` dispatches the
single GitHub workflow with `source=cloudflare`; GitHub still gates the request
against `schedule.json`. The Worker never receives Codex OAuth state, an age
key, or `auth.json.enc`.

## 中文说明

这是默认安全拒绝的公开示例。只在私有部署中替换 `YOUR_*` 占位符，并把
`GITHUB_TOKEN` 保存为 Cloudflare Worker Secret。公开 HTTP 地址只返回健康
状态；只有 `scheduled()` 会用 `source=cloudflare` 唤醒 GitHub，最终仍由
`schedule.json` gate 决定是否 prime。Worker 不接触 Codex OAuth、age 私钥或
`auth.json.enc`。
