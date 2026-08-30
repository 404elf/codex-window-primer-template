# One-time private runtime bootstrap on Windows

Do this once on a trusted computer, after copying the public template into your own **Private** GitHub repository. After this boundary is complete, tell Codex your work intent; do not edit YAML, cron, timezone values, or credential files by hand.

## 1. Prepare an isolated Codex login

Open PowerShell in the private repository and run:

```powershell
$ErrorActionPreference = "Stop"
$seed = Join-Path $env:TEMP ("codex-window-primer-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $seed | Out-Null
$env:CODEX_HOME = $seed
Set-Content -LiteralPath (Join-Path $seed "config.toml") -Value 'cli_auth_credentials_store = "file"' -NoNewline
codex login
```

Complete the browser or device login with the ChatGPT/Codex account you intend to use. Do not use an API key. Do not open, copy, or print the resulting `auth.json`.

## 2. Encrypt the isolated credential file

Install the official `age` Windows binary separately and verify its release checksum before using it. Then run this from the repository root. The private key stays in a local file and is never printed:

```powershell
$ageKeyFile = Join-Path $seed "age-key.txt"
$keyLine = (& age-keygen | Select-String -Pattern '^AGE-SECRET-KEY-1').Line
if ([string]::IsNullOrWhiteSpace($keyLine)) { throw "age-keygen did not return a private key" }
Set-Content -LiteralPath $ageKeyFile -Value $keyLine
$recipient = ($keyLine | & age-keygen -y).Trim()
& age --encrypt --recipient $recipient --output (Join-Path (Get-Location) "auth.json.enc") (Join-Path $seed "auth.json")
```

Check only that `auth.json.enc` is non-empty and that no plaintext `auth.json` is inside the repository. Do not open or print either credential file.

## 3. Add the GitHub Actions Secret

In your private repository, open **Settings → Secrets and variables → Actions → New repository secret**. Use the exact name `AGE_PRIVATE_KEY` and paste the single line from the local `age-key.txt` file. GitHub must show only that the Secret exists, never its value.

Alternatively, after authenticating the GitHub CLI, set it without printing the value:

```powershell
Get-Content -Raw -LiteralPath $ageKeyFile | gh secret set AGE_PRIVATE_KEY
```

Keep a recovery copy of the age private key in a password manager. Never paste it into chat, commit it, or echo it in a workflow.

## 4. Commit only the encrypted bundle

```powershell
git add auth.json.enc
git commit -m "Add encrypted Codex auth bundle"
git push
```

Do not run `git add -A` until you have checked that the plaintext credential and age key are outside the repository. Remove the temporary seed directory securely after confirming the Secret is saved.

## 5. Set the first schedule without editing YAML

Tell Codex the work time in natural language. Codex updates `schedule.json` and all marked workflow cron/timezone entries together, pushes only those ordinary configuration changes, and verifies the remote workflow. It must not read or modify any credential file.

## 6. Test twice

Run **Actions → Codex window primer → Run workflow** manually. Wait for the first run to finish, then run it again. The expected result is a generic success message; response text and diagnostics remain suppressed. Do not run both at once. The project should not remove its paused legacy local automation until both runs succeed and the logs contain no secrets.

If a token expires or the encrypted bundle becomes unusable, pause the schedule, repeat the isolated `codex login` and encryption steps, replace `auth.json.enc`, and update the Secret only on GitHub. Never send credential contents to Codex.
