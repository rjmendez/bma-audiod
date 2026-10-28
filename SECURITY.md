# Security policy

## Reporting
Report vulnerabilities privately through GitHub private vulnerability reporting (repository Security tab, "Report a
vulnerability"). Do not open a public issue. Expect an acknowledgement within 7 days.

## Scope
- The dashboard (`bench-kit/bma-audiod/ui/`, `ui-next/`): sign-in, CSRF, session, passkey and tailnet-identity code.
- The Windows supervisor and installer (`pc/`).

## Not in scope
- The vendor device's own firmware and its default credentials.
- Findings that need physical access to the bench network.

## Operating notes
- The dashboard binds loopback unless told otherwise and expects a TLS-terminating proxy; do not expose it to the internet
  without sign-in enabled.
- Tokens and local overrides live in untracked files (`bma.local.ps1`, the file named by `BMA_DSPD_TOKEN_FILE`,
  `classify/geo_county.local.json`). Never commit them; `scripts/check_private_paths.py` and CI fail on private values.
- Location is never stored finer than county level; audio storage is off by default.
