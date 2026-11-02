# Google Workspace sign-in

[issue #21](https://github.com/JustApu/CheckYourAmbulance-archive/issues/21). Non-normative implementation and operations notes. The product rules are [SPEC §15.1](../SPEC.md#151-google-workspace-sign-in-c--oq-13) (AUTH-1 to AUTH-6), §3.1, §16.2 and AC-22. The data model is in [the R1 contracts](data-model-contracts.md).

## How it works

Sign-in uses the OpenID Connect **authorization code flow with PKCE** against Google:

1. `GET /auth/login/` creates a random `state`, `nonce` and PKCE verifier. It stores them in a signed, HttpOnly, Secure, `SameSite=Lax` cookie limited to `/auth/` for 10 minutes, then redirects to Google. The `hd` parameter is sent only as a hint for Google's account chooser.
2. `GET /auth/callback/` checks `state` in constant time. It then exchanges the code once, over HTTPS, with the client secret and PKCE verifier, and verifies the returned ID token (`cya/accounts/google.py`):
   - the signature: RS256 only, using Google's published keys;
   - `iss` is `https://accounts.google.com` or `accounts.google.com`;
   - `aud` is exactly our client ID; `azp`, if present, is our client ID;
   - `exp` and `iat` are present and valid, with 60 s of clock leeway;
   - the `nonce` matches;
   - `email_verified` is true;
   - **`hd` equals the configured Workspace domain**.
3. The user is found by **issuer + subject** (`sub`), never by email. A new user is created and granted **Crew only**, in one transaction (AUTH-6). An existing user's email and name are refreshed. An inactive user is refused.
4. A new session starts. The session cookie is signed, Secure, HttpOnly and `SameSite=Strict`, so the callback answers with a page that refreshes to `/auth/status/` (a redirect would not carry the Strict cookie).

These rules were checked on 2026-10-20 against Google's [OpenID Connect documentation](https://developers.google.com/identity/openid-connect/openid-connect) (page last updated 2026-06-15) and its [discovery document](https://accounts.google.com/.well-known/openid-configuration). The documentation states that `hd` is "the domain associated with the Google Workspace or Cloud organization of the user", that "you must check this claim when restricting access … to only members of certain domains", and that its absence "indicates that the account does not belong to a Google hosted domain". It also says to "always use the `sub` field" as the identifier, never email. This is the verified organization claim that AUTH-3 and OQ-13 require. Personal accounts, and accounts that only use an address in the domain, have no matching `hd` and are refused (AC-22).

There is no password login or any other way to sign in (AUTH-5, OQ-13). `POST /auth/logout/` (CSRF-protected) ends the session.

## Revocation (AUTH-2, AUTH-4)

- **Deactivated in this application** (`user.active = false`): refused at sign-in. An existing session ends on its next request, because every request re-reads the user and organization. The user row is kept, so attribution stays intact.
- **Suspended or deleted in Google Workspace:** the session ends at most `CYA_SESSION_MAX_AGE` seconds after the last Google sign-in (default 8 hours, allowed 300 to 86 400). Signing in again needs Google to issue a new ID token, which a suspended account does not get: **confirmed on 2026-10-21** against real Google ("Your Google Account was disabled by your Google Workspace administrator"; see the manual verification below). Google does not notify the application of suspensions in this design.
- **Signing out:** the browser's session cookie is cleared. Sessions are signed cookies with no server-side session store (SPEC §17.7 item 5), so **a copy of the cookie taken before sign-out stays valid** until `CYA_SESSION_MAX_AGE` expires, or until the user is deactivated. This is a [documented limitation of Django's cookie-based sessions](https://docs.djangoproject.com/en/6.1/topics/http/sessions/#using-cookie-based-sessions). If sign-out must revoke copied cookies, a server-checked revocation marker per user or session has to be added; that is a design change for review, not part of [issue #21](https://github.com/JustApu/CheckYourAmbulance-archive/issues/21).

## Audit and failures

Sign-in reports `AuthAuditEvent`s to the configured audit sink. They never contain tokens or authorization codes.
- **Inside the sign-in transaction** (if auditing fails, the whole sign-in rolls back):
  - `USER_PROVISIONED`;
  - `SIGN_IN` with outcome `SUCCEEDED`;
  - `USER_PROFILE_UPDATED`, which carries each changed field's prior and new value, because email and display name are mutable attributes (SPEC AUD-3).
- **After that transaction has ended:**
  - `SIGN_IN` with outcome `REFUSED` and a reason code;
  - `SIGN_IN` with outcome `FAILED`, for `provider_unavailable`, `not_configured` or `internal_error`.

Every callback outcome is a page, never an unhandled error, and every outcome removes the one-time transaction cookie:
- **403:** the sign-in was refused, and the refusal was audited.
- **503:** sign-in is unavailable. This covers Google unreachable, configuration missing, a collaborator or the database failing, and a refusal or sign-out that could not be audited. A sign-out always ends the session, even when its audit record fails.

Failures are logged with the correlation ID that the 403 or 503 page shows as its reference. Logs contain only a fixed description and the exception class names, never exception messages or tracebacks: adapter, collaborator and database errors could contain tokens, credentials or profile data (SPEC §16.2).

## Configuration

All values are environment variables (nothing is committed; SPEC §17.6). If any is missing, sign-in shows "unavailable" (HTTP 503) and the rest of the application still runs.

| Variable | Meaning |
|---|---|
| `CYA_GOOGLE_CLIENT_ID`, `CYA_GOOGLE_CLIENT_SECRET` | OAuth client of type "Web application", owned by the organization |
| `CYA_GOOGLE_HOSTED_DOMAIN` | The Workspace domain users must belong to (the `hd` claim) |
| `CYA_GOOGLE_REDIRECT_URI` | Exactly `https://<host>/auth/callback/`, registered on the OAuth client (`http://localhost:<port>/…` is allowed for development) |
| `CYA_SESSION_MAX_AGE` | Session lifetime in seconds, i.e. the Workspace-revocation bound (default 28 800) |

Behind the TLS-terminating proxy, the application must also recognize HTTPS, or Django refuses HTTPS-origin posts such as sign-out with a CSRF error. Set `CYA_TRUST_PROXY_SSL_HEADER`, `CYA_SSL_REDIRECT` and `CYA_HSTS_SECONDS` as the [deployment runbook](deployment-runbook.md#proxy-and-application-service) describes ([issue #25](https://github.com/JustApu/CheckYourAmbulance/issues/25)).

Exactly one active organization row must exist: `python manage.py create_organization --name "<name>" --time-zone <IANA zone>`. The command is idempotent and has `--dry-run`.

Sign-in collaborators are installed as dotted paths in settings:
- `CYA_INITIAL_ROLE_GRANTER`: `cya.authorization.roles.grant_initial_crew`, backed by `CYA_AUTHORIZATION_BACKEND=cya.authorization.persistence.DatabaseRoleBackend`.
- `CYA_AUTH_AUDIT_SINK`: `cya.core.auth_audit.record_auth_event`, using the current, uncached `CYA_ROLE_LOOKUP=cya.authorization.persistence.current_role_codes`; see the [audit adapter](audit-implementation-notes.md#sign-in-adapter).

Initial provisioning commits the user, Crew role evidence, receipt and audit together.
Missing/failed collaborators or invalid role history fail closed. Tests inject
synthetic providers; the durable integration is described in the
[authorization procedure](authorization-implementation.md#sign-in-and-first-administrator-operator-procedure).

## Tests

`python manage.py test cya` runs everything offline (see [CONTRIBUTING.md](../CONTRIBUTING.md#python-and-django)):
- **Token checks:** `cya/tests/test_google_tokens.py` covers every check above with synthetic RSA keys, including tokens signed by another key, `alg=none` and HS256 key confusion.
- **Full flow:** `cya/tests/test_signin_flow.py` runs on the MariaDB test database. It covers provisioning, refusals, code replay, cookie flags, rollback when a collaborator fails, revocation and CSRF on sign-out.
- **Database rules:** `cya/tests/test_accounts_db.py` covers the identity-immutability triggers and the setup command.
- **AC-22 scenario:** bound in `cya/accounts/scenario_steps.py`.

The Google side is replaced by `cya.testing.stubs.StubGoogle`; no test reaches Google.

## Manual verification with real accounts (owner decision, option B)

The owner chose dedicated, **non-personal** test accounts in the organization's Workspace for this check ([issue #21](https://github.com/JustApu/CheckYourAmbulance-archive/issues/21), 2026-10-20). No credentials or account details go into the repository, CI or GitHub discussion. Record only pass/fail per step.

1. In the organization's Google Cloud project, create an OAuth client (Web application) with the redirect URI above. Set the variables on a non-production deployment, and configure the [issue #22](https://github.com/JustApu/CheckYourAmbulance-archive/issues/22)/[issue #23](https://github.com/JustApu/CheckYourAmbulance-archive/issues/23) collaborators.
2. Sign in with test account A, a managed account. Expect: signed in; a new user with Crew only; an audit entry.
3. Sign in with a personal Google account. Expect: refused (`not_workspace_account`) and no user created.
4. Suspend test account A in the Workspace Admin console. Within `CYA_SESSION_MAX_AGE`, expect the existing session to end and a new sign-in to be refused by Google. Then restore the account.
5. Deactivate test account A in the application. Expect: the next request is signed out, sign-in is refused (`user_inactive`), and its history is unchanged.
6. Delete the test client secret, or rotate it, after the check.

**Result (2026-10-20/21, also recorded on [PR #43](https://github.com/JustApu/CheckYourAmbulance-archive/pull/43)):**
- **Passed:** sign-in with a managed Workspace account; a signed-in session ending after the bound once the account was suspended (tested with `CYA_SESSION_MAX_AGE=300`); Google refusing the suspended account at sign-in; and in-application deactivation (immediate sign-out, then refusal with attribution kept).
- **Not exercised in this application's code:** sign-in with a personal Google account. The OAuth client's consent screen is "Internal", so Google itself refuses non-organization accounts before the callback. That refusal remains covered by the offline tests (AC-22).
- **Limits:** the check ran with stand-ins for the role granter and audit sink, it does not verify the concrete role/audit adapters now installed by [issue #22](https://github.com/JustApu/CheckYourAmbulance-archive/issues/22)/[issue #23](https://github.com/JustApu/CheckYourAmbulance-archive/issues/23). Those have offline MariaDB coverage; a deployment smoke check with the concrete collaborators remains unperformed here.
