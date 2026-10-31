# Notifications: outbox, in-app inbox and email delivery

Mechanism only (`SPEC.md` §13, NOT-1, NOT-4, NOT-5, AC-28). It sends no event's notification:
the commands that cause notifications call `enqueue`, and each is a separate issue (see issue
[issue #14](https://github.com/JustApu/CheckYourAmbulance/issues/14)). Which events are mandatory, escalation timings, role-to-recipient mappings and email safety
rules stay open (OQ-20); nothing here hard-codes them.

## Tables (`docs/data-model-contracts.md`)

| Table | Kind | Purpose |
|---|---|---|
| `notification_outbox` | A | One row per source event, channel (`IN_APP`, `EMAIL`) and recipient; written in the causing command's transaction. |
| `notification_delivery_event` | A | One row per email attempt: outcome `SENT`, `RETRY` or `GAVE_UP` and a fixed safe `error_code`. |
| `notification_read` | A | The recipient marked an in-app item read; at most one per item. |
| `notification_pref` | M | A user's optional on/off switch per event type and channel. |
| `notification_policy` | M | Administrator policy: which event/channel pairs are mandatory. |

All append-only tables have database triggers: no update or delete, and each insert must match
its command receipt. Delivery attempts must come from a worker receipt; read marks from the
recipient's own receipt. Foreign keys are organization-pair keys.

## Enqueueing (for the issues that feed the outbox)

Call `cya.notifications.enqueue.enqueue(receipt, ...)` from the command's `Plan.apply`. It
requires the command's transaction, so the intent commits or rolls back with the event, and a
replayed command (which never reaches `apply`) enqueues nothing. Arguments: the typed source
event (`source_event_type`, `source_event_id`), `event_type`, `template_key`, a minimal `payload`,
a same-site `deep_link`, recipients by `roles` and/or `user_ids`, optional `exclude_user_ids`
(usually the actor) and `channels`. Recipients are merged and de-duplicated; only active users of
the organization qualify; an unknown or foreign user rejects the whole command. Every active user is considered when
resolving a role; more than 500 distinct recipients rejects the command (`400`) rather than
silently dropping anyone.

A recipient who switched off an event type and channel receives nothing there, unless the
administrator made that pair mandatory: mandatory policy wins at enqueue even over a stale
off-switch, and a user cannot set an off-switch for a mandatory pair (`409 MANDATORY_EVENT`).

Payloads are bounded (12 keys, short strings, integers, booleans) and are for the in-app item.
They must hold identifiers and short labels only, never patient data or free text from a record.

## Email delivery

`deliver_pending(organization_id, sender)` is the bounded job (default 50 sends per pass). It pages through unresolved rows, so messages
waiting out a retry delay never keep newer messages from being reached.
`manage.py deliver_notifications` runs one pass per active organization from cron or a timer, and
`hooks.register_dispatcher` lets a deployment also wake delivery right after commit. Both are
best effort: the outbox row is the durable intent.

- **Injected sender.** Any object with `send(message)`. Tests use `cya.testing.notifications.StubSender`;
  nothing in the suite touches the network. `SmtpSender` sends through Django's email backend.
- **Message.** A fixed subject, a deep link and a fixed sentence. Payload text is never in an email.
- **Retries.** Each failed attempt is a `RETRY` row; the next attempt is due after
  `CYA_NOTIFICATION_RETRY_SECONDS` doubled per earlier attempt, up to
  `CYA_NOTIFICATION_MAX_ATTEMPTS`, then `GAVE_UP`. `REJECTED` and `INVALID_ADDRESS` give up at once.
  An inactive recipient is `GAVE_UP` with `INVALID_ADDRESS` and is not emailed.
- **Safe errors.** Only a fixed code is stored (`TRANSIENT`, `REJECTED`, `INVALID_ADDRESS`,
  `UNAVAILABLE`, `UNKNOWN`). Exception text, addresses and provider detail are never persisted.
  `delivery.failures(organization_id)` lists intents that failed and are not yet sent, for
  administrators.
- **Job identity.** The attempt is a worker command: service `worker.notifications`, command
  `NOTIFICATION_DELIVERY_ATTEMPT`, job key `["<outbox id>","<attempt number>"]`. Recording the same
  attempt twice is one row. If a second worker reaches the same attempt with a different outcome it
  collides: the stored row wins, an error is logged and the report counts a collision.

### What delivery does not promise

Delivery is **at-least-once**; no exactly-once SMTP claim is made. The sender is called before the
attempt row is written (a command callback may not use the network). A crash after the transport
accepted a message but before the row committed is an ambiguous outcome: the next pass repeats the
same attempt number and the message may be sent again. The intent is never lost, and that case is
tested with the injected sender. A real transport's own idempotency guarantee, if any, belongs in
that adapter and is described and tested there. `SmtpSender` has none; it passes the outbox id in
an `X-CYA-Reference` header so a recipient or relay can recognize a repeat.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `CYA_NOTIFICATION_SENDER` | unset | Dotted path to a zero-argument factory returning a sender. Unset leaves email queued; `deliver_notifications` then refuses to run. |
| `CYA_PUBLIC_BASE_URL` | unset | Prefix for email deep links, e.g. `https://cya.example`. Unset sends the bare path. |
| `CYA_NOTIFICATION_MAX_ATTEMPTS` | 5 | Attempts before `GAVE_UP` (1 to 20). |
| `CYA_NOTIFICATION_RETRY_SECONDS` | 60 | Base retry delay, doubled per attempt (1 to 86400). |

The email provider, sender identity and credentials are deployment configuration. They need the
owner's input before production, not before this mechanism merges.

## Audited commands

`cya.notifications.commands`: `set_policy` (administrator only,
`configure_schedules_thresholds_notifications`), `set_preference` (own preference, any role holder)
and `mark_read` (recipient only; another user's item looks absent). Reads are `inbox.inbox`,
`inbox.unread_count` and `delivery.failures`; each filters by the caller's own user and
organization server-side.

## HTTP endpoints

Authenticated same-origin JSON; actor, organization and roles come from the signed-in user, never
the body or query. Reads need a built-in role (401 signed out, 403 without one); unknown or
repeated query parameters are `400`. Writes carry a version 4 `operation_id` and are idempotent on
it (a replay returns the first result; reuse with different content is `409 OPERATION_REUSED`).

| Route | Who | Meaning |
|---|---|---|
| `GET /api/v1/notifications?limit=&unread=1` | any role holder | The caller's own in-app items, newest first (default 50, maximum 200), each with a deep link and read flag, plus `unread_count`. |
| `POST /api/v1/notifications/{id}/reads` | the recipient | Mark one item read. Another user's or an email item is `404`; repeating returns `ALREADY_READ`. |
| `GET`/`POST /api/v1/notification-preferences` | any role holder | Own optional switches. Setting a mandatory pair off is `409 MANDATORY_EVENT`. |
| `GET`/`POST /api/v1/notification-policy` | Administrator | List or set mandatory event/channel pairs; audited. |
| `GET /api/v1/notification-failures` | Administrator | Email intents that failed and are not yet sent: attempts, last outcome and safe error code only. |

Malformed input (bad event code, channel, boolean or identifier, unknown field) is `400
MALFORMED_REQUEST` and writes nothing.

## Not yet built

No inbox screen: the screens belong to later issues. No event sends a notification yet; the
commands that cause them call `enqueue` in [issue #14](https://github.com/JustApu/CheckYourAmbulance/issues/14).
