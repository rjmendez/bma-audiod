# Dashboard login (ui/auth.py)

Contract: `contracts/specs/ui-http.md` 1.7.0, Authentication.

## Token and credentials

- One shared secret token: >= 24 printable ASCII characters (supervisor makes 48 hex characters, 192 bits). Stored in a file, never an environment variable or command line: `--auth-token-file`.
- Data API answers GET and HEAD only; the only POSTs are sign-in, sign-out, passkey routes (1.7.0).
- HTTP Basic: any user name, token as password (`curl -u x:TOKEN`).
- `?token=<token>` on path `/` only: correct token answers `302 -> /` and sets cookie `bma_auth` (`HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000`, plus `Secure` over HTTPS).
  - Cookie value `HMAC-SHA256(per-process random key, fingerprint of the token)`. A restart or a changed token invalidates it.
- `401` carries `WWW-Authenticate: Basic`, omitted when `Sec-Fetch-Mode` is not `navigate` (the page's own `fetch()`).
- Everything is behind the login (`/static/*`, unknown paths, non-GET methods). Check order: connection cap (503) -> Host allow-list (403) -> login (401/429) -> method and routing. Protected responses are `Cache-Control: no-store`.
- Secure by default: with a token file the login is mandatory. No token file: starts open only on a loopback bind. Any other bind refuses to start (exit 2) unless `--no-auth` (prints a warning).
  - An invalid token file (shorter than 24, leading/trailing whitespace, BOM, non-ASCII) or a missing explicit `--auth-token-file` also refuses to start.

## Brute-force defence

- Per client IP: 5 wrong credentials lock the address 60 s; each further failure doubles the lock, cap 15 min. While locked every request gets `429` + `Retry-After`, even with the correct token. Success clears the count;
  - idle address forgotten after 1 h; at most 1024 addresses tracked (least recently failing evicted).
- Counted: presented-and-wrong credential only. No credential or a stale cookie does not count.
- Log line only: `auth failure ip=<ip> failures=<n>` / `auth lockout ...`; never the token, cookie or Authorization header.
- Comparison: `hmac.compare_digest` on SHA-256 digests (`ct_equal`); every credential compared, no early exit.
- Clients behind one NAT share a counter; lockout state is in memory.

## TLS

- `--tls-cert`, `--tls-key` (PEM): stdlib `ssl`, TLS 1.2+, no client certificates, handshake in the connection's worker thread.
- Tailnet (WireGuard): plain HTTP is already encrypted. LAN: plain HTTP exposes token and cookie; use TLS. No tunnels or port forwards.

### Tailscale certificate (`pc/setup_tls.ps1`, 1.7.0)
Prerequisites: MagicDNS and "HTTPS Certificates" enabled in the admin console. Certificates: Let's Encrypt, 90 days, NOT renewed by Tailscale.

    powershell -File <data-dir>\setup_tls.ps1            # name from `tailscale status`; or  -Name <node>.<tailnet-domain>

Run once in an administrator shell (or `tailscale set --operator=<user>`), then as the dashboard's user.
- Runs `tailscale cert --cert-file <f> --key-file <f> <name>` into a scratch dir under `<data-dir>\tls\` (current-user ACL, inheritance removed); checks name, private key, not expiring;
  - then replaces `tls\dashboard_cert.pem`, `tls\dashboard_key.pem`, writes `tls\tls_name.txt`. Any failure leaves the installed pair untouched (exit 1). Nothing secret printed; nothing written outside the data dir.
- Idempotent: certificate more than 30 days from expiry (`-RenewBeforeDays`) -> nothing (exit 0). Exit 10 = installed or renewed. `-CheckOnly`: exit 0 current, 3 due, no tailscale call. `-Force`: renew now (Let's Encrypt rate caps).
- Supervisor (`pc/run_pipeline.ps1`): passes `--tls-cert/--tls-key` when both `tls\` files exist (older `<data-dir>\dashboard_cert.pem` pair still works); checks daily whether to renew (only when `tls\tls_name.txt` exists);
  - on exit 10 kills the dashboard so the restart loop loads it. `pc/install_task.ps1` copies `setup_tls.ps1` to the data dir.
- Docker (`docker/entrypoint.sh`, role `dashboard`): mount `/run/secrets/dashboard_cert`, `/run/secrets/dashboard_key` (readable by uid 10001); entrypoint adds `--tls-cert/--tls-key`. Renew on the host.
- Cookie is `Secure` whenever the browser sees HTTPS: own TLS, or `X-Forwarded-Proto: https` from a loopback peer (`tailscale serve` in front). If Serve does not send that header: run the dashboard with `--tls-cert/--tls-key` and `tailscale serve --https=443 https+insecure://127.0.0.1:48500`.
- URL: `https://<node-dns-name>:48500/` (IP access gives a name-mismatch warning).

## Sign-in page, sessions, CSRF (1.7.0)

- No cookie -> `/login` (form with the token, `autocomplete="current-password"`). Right token sets `bma_auth` = random 256-bit session id in server memory (`HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000`, `; Secure` over HTTPS).
  - Wrong token counts in the SAME lockout as Basic and `?token=`. `POST /logout` ends the session. `GET /api/session` gives the page its CSRF token.
- Sessions: per login, in memory, at most 256; restart signs everyone out.
- CSRF, two layers: `SameSite=Strict` + same-origin rule on every POST (`Sec-Fetch-Site` must be `same-origin` when present; `Origin` must equal Host when present; `Origin: null` only with `Sec-Fetch-Site: same-origin`);
  - per-session token `HMAC(key, session id)` sent as `X-CSRF-Token` (`POST /logout`, passkey management). Login form carries a stateless signed token (15 min).
- `?token=` stays (bookmarks, scripts, `show_dashboard_token.ps1` link), redirects to clean `/`; its cookie is the constant HMAC, revocable only by rotating the token file. Basic stays for scripts.
- Only `/` or `/index.html` with `Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document` and no credentials redirects to `/login`; everything else without credentials is 401.
- `static/session.js` shows "Sign out" / "Add a passkey" only when `/api/session` allows.

## Tailnet identity (`--tailnet-identity`, 1.7.0)

`tailscale serve` adds `Tailscale-User-Login: alice@example.com` to forwarded requests (set by Serve, client copies removed; absent for tagged devices and Funnel; non-ASCII may be RFC 2047 encoded).
- Trusted only from a loopback peer (TCP peer address, never `X-Forwarded-For`), exactly once, plain ASCII, login on the allow-list. Any other peer carrying the header is judged as if without it. Rule in `auth.identity_login`.
- `--allowed-users FILE` (default `<data-dir>\allowed_users.txt`): one login per line, `#` comments; missing/empty = nobody; re-read on change.
- `--require-identity`: refuse everything else (token, Basic, cookies, passkeys). Not passed by the supervisor.
- Supervisor: non-empty `allowed_users.txt` in the data dir turns on `--tailnet-identity --allowed-users`.
- Trust boundary: anything connecting from loopback is believed, including other local processes/users. Use only on a machine whose local processes are trusted.
- Setup: `tailscale serve --bg --https=443 http://127.0.0.1:48500` (`https+insecure://127.0.0.1:48500` when the dashboard serves TLS); add `<node>.<tailnet-domain>` to the Host allow-list (supervisor does); put the login in `allowed_users.txt`; restart.
- Lockout: wrong tokens via Serve count against `127.0.0.1`; an identity-authenticated request ignores lockout.

## Passkeys (WebAuthn, `--passkey-origin`, 1.7.0)

Needs HTTPS and a login: `--passkey-origin https://<node>.<tailnet-domain>[:48500]` (repeat per access path; one host name).
- `ui/passkeys.py`, stdlib only: ECDSA P-256 verification only, small CBOR reader, DER parsing; public data only. Missing file: flag refuses to start with a clear message.
- ES256 only; attestation not verified; user verification REQUIRED every time; at most 20 passkeys; challenges single use, 5 min, bound to the registering session; counter must increase when either side is non-zero.
- Registration only from an authenticated session ("Add a passkey" + CSRF token). Stored in `<data-dir>\passkeys.json`: credential id, public point (x, y), counter, label, times (0600 where the OS has modes; on Windows the data dir ACL applies). No secrets in the file.
- Login: `/login` "Sign in with a passkey" (discoverable credentials); failure counts in the same lockout.
- Verified against OpenSSL signatures and a software authenticator (`ui/test_passkeys.py`). Not exercised with a real browser or hardware authenticator; `static/login.js`, `static/session.js` have no automated test.
- Supervisor: `enable_passkeys.txt` (any content) + `tls\tls_name.txt` adds `--passkey-origin https://<name>` and `https://<name>:48500`.

## Operating

- First run: `pc/run_pipeline.ps1` (Start-Ui) creates `<data-dir>\dashboard_token.txt` if absent (48 hex chars, .NET RNG, no BOM, no trailing newline, ACL limited to current user, one log line naming the file) and passes `--auth-token-file`.
  - If the ACL step fails the file is deleted and the dashboard refuses to start. `install_task.ps1` never writes the token file;
  - it copies `auth.py` and `show_dashboard_token.ps1`.
- Phone: run `<data-dir>\show_dashboard_token.ps1` (PRINTS THE SECRET; one `http://<host>:48500/?token=...` URL per name/address); open one URL on the phone, bookmark the landing address.
- Rotate / revoke a lost device: write a new token file (same rules), restart; all cookies stop working.
- Keep the Windows Firewall rule as a second layer.

## Feature gates

- `handler.authenticated` (bool, class default `False`, reset every request): `True` exactly when THIS request passed the login (right Basic password or valid cookie).
  - `False` for no/wrong/stale credential, locked-out client (429), refused Host, the `?token=` redirect, and every request to a server with no login configured.
- `server.auth.enabled` (bool): `True` when a login is configured, else `False` (`auth.OPEN`). Never `None`.
- Unauthenticated requests are refused (401/429) before any route when `server.auth.enabled`. Audio-derived features MUST still check explicitly via `request_authenticated(handler)` in `bma_ui.py`: true only when `handler.server.auth` exists AND `handler.authenticated is True`.

### Spectrum (`GET /api/spectrum`)
`spectrum_client_allowed(authenticated, tier, detail_flag)` fed by `request_authenticated(handler)`:
- BOTH tiers only to an authenticated client, any network (peer address, Host, `X-Forwarded-*` play no part).
- DETAIL tier also needs `--spectrum-detail` (403 `spectrum_detail_disabled`, even when authenticated).
- Server without a login (`--no-auth`, or loopback without token file): 200 `state: auth_required`, no rows, for every client; the dashboard shows "sign in required". The supervisor always creates a token file.
- A session that stops being accepted: panel turns `unavailable` (fetch gets 401), strip says "Signed out".

## Tests

- `ui/test_auth.py`: real servers, real HTTP, startup rules, TLS when `openssl` exists (else exit 77 SKIP).
- `ui/test_dom_auth.py`: headless Edge, cookie flow, Signed out state.
- `pc/test_supervisor_config.ps1`: token creation, Start-Ui arguments.
- `contracts/test_contracts.py`: 401/429 problem examples.
- `ui/test_auth_spectrum.py`: login + live spectrum (real token file, real BMAS datagrams, Basic and cookie, wrong token, detail flag, open server, `status.human_hidden_24h`; needs jsonschema: contracts venv).
- `ui/test_dom_auth_spectrum.py`: headless Edge, panel live after cookie login, `unavailable` after lost session, "sign in required" on open server.
- `ui/test_auth2.py` (sign-in page, sessions, logout, CSRF, Secure behind a proxy, tailnet identity incl. forged header from a real non-loopback address, TLS)
- `ui/test_passkeys.py` (ECDSA, DER, CBOR, ceremonies, HTTP routes, optional import)
- `pc/test_setup_tls.py` (real `setup_tls.ps1` against a fake tailscale and OpenSSL certificates).
- `ui/mutation_auth.py`: mutations of all of the above; each must go red.
