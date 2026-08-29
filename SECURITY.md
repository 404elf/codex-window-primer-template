# Security policy

Codex Window Primer handles a user's ChatGPT/Codex OAuth state. Treat every
decrypted `auth.json`, OAuth token, refresh token, age private key, and account
identifier as a password.

## Do not disclose credentials

Never include credentials in a public repository, issue, pull request, prompt,
workflow log, screenshot, or support request. Do not run the credential-bearing
workflow from a public repository. If a credential may have been exposed,
revoke or replace it immediately and regenerate the encrypted state.

## Reporting a vulnerability

Please report security issues privately through GitHub's private vulnerability
reporting or Security Advisories for the affected repository. Do not open a
public issue for a credential-handling vulnerability. Include reproduction
steps and affected paths, but never include live credentials or decrypted
files.

This project is unofficial and is not affiliated with or endorsed by OpenAI.
