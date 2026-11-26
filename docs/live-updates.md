# Authenticated vehicle live updates

[issue #84](https://github.com/JustApu/CheckYourAmbulance-archive/issues/84) adds an identifier-only WebSocket hint channel. It does not add a live
inspection screen or a vehicle-state HTTP endpoint. The current inspection page
is a placeholder; clients must use the authoritative query that populated their
screen when reconnecting. The obligation projection query is
`cya.obligations.projections.get_vehicle_obligations_as_of`, which derives state
from persisted evidence and fails closed when evidence adapters are unavailable.
As read endpoints are added, a client must rerun the relevant endpoint after
every successful reconnect and whenever it detects a closed or lost channel.
Hints are never a source of inspection or compliance state.

## Connection and authorization

The ASGI route is
`/ws/v1/vehicles/<vehicle UUID>/live/`. The browser sends the normal signed
`cya_session` cookie and an Origin matching Django `ALLOWED_HOSTS`. The Origin
validator rejects missing or untrusted origins. Before joining a group, the
consumer verifies the signed-cookie user and age, reloads the active user and
organization, checks for a current built-in organization role, and verifies
that the active vehicle belongs to that organization. It repeats those checks
every 60 seconds by default (configurable through
`CYA_LIVE_AUTH_RECHECK_SECONDS`, bounded to 10–300 seconds) and closes a socket
when authorization is no longer valid or cannot be checked.

At most four live connections per user per application process are accepted by
default (`CYA_LIVE_MAX_CONNECTIONS_PER_USER`, bounded to 1–32). This matches the
initial single-process deployment assumption. A multi-process deployment needs
a shared channel layer and shared connection accounting. Client frames are
limited to 64 KiB; this protocol is server-to-client only, so all client data
frames are closed as invalid (oversized frames use close code 1009).

The default channel layer is in-memory and process-local, as required by the
current single-process deployment decision. It is suitable for local use and
tests, not for multiple application processes. The HTTP publisher and WebSocket
consumer must run in the same ASGI process for hints to reach connected clients.
Serving the publisher through WSGI or splitting HTTP and sockets across processes
loses those hints. A deployment change needs a shared channel layer and an
approved update to the single-process decision.

`CYA_ALLOWED_HOSTS` supplies the allowed host names for the HTTP application and
WebSocket Origin validation. The wildcard value `*` is rejected; list the
specific host names instead.

## Event contract and recovery

The server emits `connection.ready` after a successful join. Vehicle updates
have this JSON shape:

```json
{"type":"vehicle.update","event_id":"<UUID>","kind":"check.recorded"}
```

The allowed `kind` values are `check.recorded`, `check.corrected`,
`check.invalidated`, `deficiency.changed`, `odometer.recorded`, and
`odometer.corrected`, `odometer.invalidated`, `session.changed`, `status.changed`,
`status.request`, `obligations.changed` and `seal.changed`. `event_id` identifies the durable domain record or event.
Messages contain no names, notes, check outcomes, or other business payload.
The server rejects unsupported kinds and malformed identifiers.

**Pushed rows (SPEC 8.3.0 §17.3, OQ-88; [issue #278](https://github.com/JustApu/CheckYourAmbulance/issues/278)).**
A session page names its session when it connects: `/ws/v1/vehicles/<vehicle UUID>/live/?session=<session UUID>`.
If the user is a recorded participant of that session on that vehicle, the socket also joins the
session's group; otherwise the name is ignored, so the socket reveals nothing about the session.
This is the rule the checklist API applies to `session_id` (`cya.live_updates.auth._session_participant`).
After an item check commits (not a kit presence check), `cya.attention.live_rows` reads the item's
rows once for each open session on the vehicle (at most 8), exactly as that session's participants
would read them (`view=NORMAL`, no filter, `item_ids=<item>`), and sends them to the session's
group before the vehicle hint:

```json
{"type":"checklist.rows","event_id":"<UUID>","session_id":"<UUID>","item_id":"<UUID>","as_of":"<read instant>","items":[...]}
```

`items` are the checklist API's rows. A set larger than 48 KiB is left out, and those pages re-read.
The checklist page shows pushed rows only for its own session, in Normal order with no filter
and rows already loaded, and only when `as_of` is later than the read its rows came from. The
hint they cover then needs no read, and the saver waits up to a second for its own rows before
reading as before. A read that started earlier and answers later cannot replace newer pushed
rows. Every other frame is unchanged and identifier-only, and reconnection still reconciles from
the authoritative query.

Publishers register delivery with `transaction.on_commit`; a rolled-back
business change emits no hint. Delivery is best-effort: channel-layer failures
are logged without undoing the committed business change. Clients deduplicate
by event ID if useful, treat each event as a request to refresh, and always
re-read authoritative state after reconnect because an event may be missed.

**Signed out during a re-read** ([issue #37](https://github.com/JustApu/CheckYourAmbulance/issues/37)). The page routes that scripts re-read
(`/fleet/readiness/panel/`, `/status/vehicles/{id}/panel/`, `/problems/{id}/verification-items/`)
answer a request without a valid session with 401 `AUTHENTICATION_REQUIRED` instead of
redirecting, as the `/api/v1/...` endpoints already do. Full-page navigations still redirect to
`/auth/login/`, and a signed-in user without the role still gets 403. On a 401 each script stops
its timers, live re-reads and reconnects and says it is signed out, with a link to sign in again.
It never requests `/auth/login/` itself, so an open tab past its session cannot start sign-in or
count against the [sign-in rate limit](google-workspace-signin.md). The checklist keeps each
Unconfirmed item's Retry.

Seal break and audit commands publish `seal.changed`; inspection and verification publish
`obligations.changed`. The seal page re-reads authoritative status on a hint and after
reconnecting. `record_check` publishes `check.recorded`, and the three operational-status
commands publish `status.changed` (the new status event, from setting a status or approving
a request) and `status.request` (the request or decision row, from filing a request or
deciding one; an approval publishes both). Correction, invalidation, deficiency,
odometer, session and obligation commands publish their corresponding kinds.
Publishers call `publish_vehicle_update` from the command's write transaction
after validating the organization and vehicle scope.

## Tests

The offline tests use the in-memory channel layer and cover socket authorization,
connection and frame bounds, supported event identifiers, and after-commit
delivery including rollback. No external service is used.
`cya/attention/tests/test_live_rows.py` checks that pushed rows equal the API's one-row read for the
session, that only participants join a session's group, the consumer's checks, and that rows go
out before the hint (and the hint still goes out if reading the rows fails).
`workflow_script_harness.js` covers the page applying, ignoring and re-applying pushed rows.
