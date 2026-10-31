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
`odometer.corrected`, `odometer.invalidated`, `session.changed`, `status.changed`
and `status.request`. `event_id` identifies the durable domain record or event.
Messages contain no names, notes, check outcomes, or other business payload.
The server rejects unsupported kinds and malformed identifiers.

Publishers register delivery with `transaction.on_commit`; a rolled-back
business change emits no hint. Delivery is best-effort: channel-layer failures
are logged without undoing the committed business change. Clients deduplicate
by event ID if useful, treat each event as a request to refresh, and always
re-read authoritative state after reconnect because an event may be missed.

Currently `record_check` publishes `check.recorded`, and the three operational-status
commands publish `status.changed` (the new status event, from setting a status or approving
a request) and `status.request` (the request or decision row, from filing a request or
deciding one; an approval publishes both). Other event kinds are
reserved for the corresponding correction, invalidation, deficiency, odometer,
and session commands as those features land. They must call
`publish_vehicle_update` from the command's write transaction and after
validating the organization and vehicle scope.

## Tests

The offline tests use the in-memory channel layer and cover socket authorization,
connection and frame bounds, supported event identifiers, and after-commit
delivery including rollback. No external service is used.
