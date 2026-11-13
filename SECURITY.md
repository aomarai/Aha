# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Report it privately through GitHub:
[**Report a vulnerability**](https://github.com/aomarai/Aha/security/advisories/new) (the
repository's *Security* tab, *Report a vulnerability*). Include what you found, how to reproduce it,
and what an attacker could do with it.

You can expect an acknowledgement within a few days. This is a small, volunteer-run project, so
please allow reasonable time for a fix before disclosing publicly; credit is given if you want it.

## Scope

In scope: the bot's code in `src/`, its container image, its CI configuration, and the way it
handles tokens, user input and outbound requests (the `/url` fetcher in `aha.net` is a deliberate
SSRF boundary, so a bypass is exactly the kind of report we want).

Out of scope: vulnerabilities in Discord itself, in third-party services, or findings that need
control of the host or of the bot token.

## How secrets are handled

- The bot token is read from `DISCORD_BOT_TOKEN` or from the file named by `DISCORD_BOT_TOKEN_FILE`
  (preferred in a cluster) and nowhere else. It is never in the image, a ConfigMap, or the
  repository, and it is masked as `***` in logs and tracebacks.
- `gitleaks` runs in pre-commit and in CI.
- If a token may have leaked, rotate it immediately: see the
  [token rotation runbook](docs/runbooks/token-rotation.md). Discord also resets tokens it finds on
  public GitHub, which takes the bot offline.

## Supported versions

Only the latest release and the `main` branch receive fixes.
