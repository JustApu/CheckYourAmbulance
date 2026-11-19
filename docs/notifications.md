# Notifications: outbox, in-app inbox and email delivery

The outbox, inbox and delivery mechanism (`SPEC.md` §13, NOT-1, NOT-4, NOT-5, AC-28), and since
[issue #14](https://github.com/JustApu/CheckYourAmbulance/issues/14) the minimal notification set that the lifecycle and status commands emit
([below](#which-events-notify-and-whom)). Which events are mandatory, escalation timings and email
safety rules stay open (OQ-20): no event is mandatory and there is no escalation beyond the single
overdue notice until the owner decides.

## Tables (`docs/data-model-contracts.md`)

| Table | Kind | Purpose |
|---|---|---|
| `notification_outbox` | A | One row per source event, channel (`IN_APP`, `EMAIL`) and recipient; written in the causing command's transaction. |
| `notification_delivery_event` | A | One row per email attempt: outcome `SENT`, `RETRY` or `GAVE_UP` and a fixed safe `error_code`. |
| `notification_read` | A | The recipient marked an in-app item read; at most one per item. |
| `notification_pref` | M | A user's optional on/off switch per event type and channel. |
| `notification_policy` | M | Administrator policy: which event/channel pairs are mandatory. |
| `notification_recipient_rule` | M | Administrator override of one default recipient row (issue #14). |

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

## Which events notify, and whom

SPEC NOT-3's minimal configurable set, plus the correction and chain-invalidation notices. Each
notification is written by the command that causes it, in its transaction, through
`cya.notifications.recipients.notify`. The default recipients are documented data in
`cya/notifications/default_recipients.json`, approved by the owner on 2026-10-27 and 2026-11-01:

| Event type | Caused by | Default recipients |
|---|---|---|
| `deficiency.assigned` | `ASSIGNED` (assign or reassign) | the new assignee |
| `deficiency.reopened_verification_added` | `VERIFICATION_ADDED` | the **current** assignee only (SPEC §8.7, AC-17) |
| `deficiency.reopened_verification_invalidated` | `VERIFICATION_REOPENED` (INV-4) | the assignee and all supervisors |
| `deficiency.reopened_by_administrator` | `ADMINISTRATOR_REOPENED` | the assignee and the reporter |
| `deficiency.reopened_reported_in_error` | `REOPENED` (INV-7 chain invalidation) | all supervisors |
| `deficiency.correction_notice` | `CORRECTION_NOTICE` (COR-4) | all supervisors |
| `deficiency.extension_requested` | `EXTENSION_REQUESTED` | all supervisors |
| `deficiency.extension_decided` | `EXTENSION_DECIDED` | the requester and the assignee |
| `deficiency.deadline_overdue` | the overdue job ([below](#overdue-deadlines)) | the assignee and all supervisors |
| `deficiency.administrator_closed` | `ADMINISTRATOR_CLOSED` | the assignee and the reporter |
| `check.supervisor_completion_voided` | chain invalidation voiding a supervisor completion | all supervisors |
| `vehicle.status_changed` | a status set directly or by an approved request | all supervisors and administrators |
| `vehicle.status_change_requested` | a crew status-change request | all supervisors |
| `vehicle.status_request_decided` | the decision on a request | the requester |
| `session.reopened` | a session reopened | the other recorded participants |
| `deficiency.reported` | `REPORTED`, a new problem (issue #202) | the vehicle's current crew and all supervisors |
| `vehicle.check_due` | the check-due job ([below](#checks-coming-due)) | the vehicle's current crew |
| `vehicle_assignment.changed` | someone else assigned, moved, extended or ended a person's vehicle assignment, including by deactivating the vehicle | the person |
| `vehicle_assignment.moved_off` | a person moved from one vehicle to another | nobody by default; the crew left on the vehicle they left can be switched on |

Recipient kinds: `ASSIGNEE` (the deficiency's assignee after the event), `REPORTER`, `REQUESTER`
(of the extension or status request), `PARTICIPANTS` (of the session), `SUPERVISORS` and
`ADMINISTRATORS` (every active holder of that role), `PERSON` (the person whose vehicle assignment
changed) and `VEHICLE_CREW` (the active people assigned to the vehicle when the event is recorded,
SPEC 15.4 ASG-7; for a move, the vehicle left). Each event lists the kinds it can name. The
reopened-problem events can also name the vehicle's crew; it is off by default. The crew-notice
defaults are the owner's decisions of 2026-11-18 on [issue #202](https://github.com/JustApu/CheckYourAmbulance/issues/202).

- **Never the actor.** The person whose action caused the event is not told about it, so assigning
  yourself or reopening your own solo session notifies nobody.
- **Inactive people are skipped.** A named person who is no longer an active user of the
  organization receives nothing and the command still succeeds; roles resolve to active users.
- **De-duplicated.** One notification per recipient and channel for each source event, even when a
  person is both requester and assignee.
- **Both channels.** In-app and email, subject to each user's optional switches and the mandatory
  policy (above). Email carries a fixed sentence and a deep link only (NOT-4).
- **Payload.** Identifiers and codes only: the deficiency, vehicle, session or obligation id, the
  event and status codes and, for decisions, the decision. Never free text from a record.
- **Not notified.** Every other event, including empty-scope closure, scope expansion, a failed
  verification, dependency and progress updates and the acknowledgement itself. Each is a
  configuration addition when the owner wants it.

**Failure.** Resolving recipients or writing the outbox happens inside the causing command, so a
failure there rolls the whole command back (nothing is saved and the caller sees the error; a retry
with the same operation id is safe). Delivery happens after commit: an email that fails is retried
or given up as described below, and never undoes the command (NOT-5).

### Administering recipients

`GET /api/v1/notification-recipients` (administrator) lists every event with its available kinds,
the default, this organization's overrides and the effective recipients. `POST` with
`{operation_id, event_type, recipient, enabled}` turns one kind on or off for one event, overriding
the default (`set_recipient_rule`, audited as `NOTIFICATION_RECIPIENT_SET`, permission
`configure_schedules_thresholds_notifications`). An unknown event is `404`; a kind the event cannot
name, a non-boolean `enabled` or an unknown field is `400`. A replay returns the stored answer.

### Overdue deadlines

An overdue deadline has no event, so `manage.py notify_overdue` runs a bounded pass per active
organization (cron or a timer, like `deliver_notifications`; `--limit`, default 50 notices). A
deficiency still being worked whose governing deadline (SPEC DEF-5) has passed is notified **once
per governing deadline**: a later deadline revision or extension decision that also passes is a new
notice. While an escalation acknowledgement is active (SPEC §8.4) the notice is suppressed; a later
pass sends it once the acknowledgement has expired, if the deficiency is still overdue.

Each notice is a worker command (service `worker.deficiency_overdue`, command
`DEFICIENCY_OVERDUE_NOTICE`, job key `["<deficiency id>","<governing event id>"]`) whose operation id
is derived from the key, so a repeat or a second worker replays without writing. It locks the
deficiency and checks again; one that stopped being overdue or was acknowledged meanwhile is
skipped without a receipt. One deficiency's failure is logged and counted and never blocks the
others. Candidates are chosen by the governing deadline itself (the newest event carrying a
deadline), not the stored base deadline, so an extended deficiency is not a candidate until its new
date passes. The pass pages through them oldest governing deadline first, 200 per query, and
recognizes those already notified with one receipt query per page; neither they nor suppressed
ones count toward the limit, so a backlog cannot keep a newly overdue deficiency from being reached.

### Checks coming due

A check coming due has no event, so `manage.py notify_check_due` runs a bounded pass per active
organization (cron or a timer, like `notify_overdue`; `--limit`, default 50 notices). It finds
selected routine obligations whose window opened within the last two hours and whose grace has not
passed, on active vehicles, and sends **one notice per vehicle per due instant** with the number of
checks (owner, 2026-11-18). By default it goes to the vehicle's current crew when the notice is
written. A vehicle out of service at the due instant is skipped, because those obligations are
exempt. A window opened more than two hours ago is not announced, so a job that was down sends
nothing stale. A vehicle with nobody assigned has its notice recorded with no recipients, and it is
not sent again when someone is assigned later.

Each notice is a worker command (service `worker.vehicle_check_due`, command
`VEHICLE_CHECK_DUE_NOTICE`, job key `["<vehicle id>","<due instant>"]`) whose operation id is
derived from the key, so a repeat or a second worker replays without writing. One vehicle's failure
is logged and counted and never blocks the others.

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

## Outgoing-mail relay (SPEC 4.2.0 DEP-13, issue #8)

An Administrator (`configure_schedules_thresholds_notifications`) sets the organization's relay on
**Administration → Outgoing mail** (`/administration/mail-relay/`, `cya/notifications/relay_views.py`):
- host and port;
- encryption: STARTTLS or TLS from the start. A plaintext relay is refused;
- authentication: a username and password, or this server's IP address alone (the relay operator
  allows it, as Google Workspace's SMTP relay can). IP authentication stores no credential;
- the sender address;
- the operator and deputy recipient addresses for operations alerts, 1 to 10 each.

The settings live in `mail_relay`, one row per organization:
- **The password** is stored only as Fernet ciphertext under `CYA_MAIL_RELAY_KEY`, from the
  root-owned secrets file (the DEP-10 exception). It is never rendered back; a blank field keeps it.
- **Saving** runs the audited `configure_mail_relay` (`MAIL_RELAY_SET`). Its snapshots and request
  digest record only whether a password is set or replaced. `base_revision` refuses a save over a
  change made since the page was shown.
- **Send test email** records an audited `MAIL_RELAY_TEST` request, then sends one message through
  the saved relay to every operator and deputy address. The page shows a fixed safe result (`SENT`,
  `CONNECT_FAILED`, `TLS_FAILED`, `AUTH_FAILED`, `REJECTED`, `TIMEOUT`, `KEY_MISSING`,
  `UNKNOWN`). No provider reply or exception text is stored or shown.
- **The host copy.** `manage.py export_mail_relay --organization <id> --output <file>` writes the
  settings the host alerts need, including the decrypted password, as JSON. It writes mode 0600
  through a temporary file and an atomic rename. On any failure, such as a missing or wrong key, it
  exits nonzero and leaves the previous copy untouched. It runs as root from a timer (see the
  [deployment runbook](deployment-runbook.md)), so host alerts still send while the app or database
  is down.

**Notification email goes through this relay** (DEP-13: "Email notifications (§13) use the same
relay"; [issue #224](https://github.com/JustApu/CheckYourAmbulance/issues/224)). `deliver_notifications`
picks each organization's sender with `senders.sender_for`: the `CYA_NOTIFICATION_SENDER` override
when set, otherwise a `RelaySender` for that organization's saved relay. It sends one message per
recipient through the same transport as the test email (TLS required, password or IP authentication),
with the `X-CYA-Reference` header. An organization with no relay is skipped with a note, and its
email stays queued; the job still succeeds, so its timer does not fail. Relay outcomes map to
delivery codes as follows:
- `REJECTED` is permanent (given up);
- `CONNECT_FAILED` and `TIMEOUT` are `TRANSIENT`;
- `TLS_FAILED`, `AUTH_FAILED` and `KEY_MISSING` are `UNAVAILABLE`, retried until the operator fixes
  the relay or the attempts run out;
- anything else is `UNKNOWN`.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `CYA_NOTIFICATION_SENDER` | unset | Optional override: dotted path to a zero-argument factory returning a sender, used instead of each organization's Outgoing-mail relay (for example in tests). Unset: each organization's relay is used, and an organization without one keeps its email queued. |
| `CYA_PUBLIC_BASE_URL` | unset | Prefix for email deep links, e.g. `https://cya.example`. Unset sends the bare path. |
| `CYA_NOTIFICATION_MAX_ATTEMPTS` | 5 | Attempts before `GAVE_UP` (1 to 20). |
| `CYA_NOTIFICATION_RETRY_SECONDS` | 60 | Base retry delay, doubled per attempt (1 to 86400). |
| `CYA_MAIL_RELAY_KEY` | unset | Fernet key for the relay password (root-owned secrets file). Unset: a password cannot be saved or used; IP authentication still works. |
| `CYA_MAIL_RELAY_TRANSPORT` | `cya.notifications.relay.SmtpTransport` | Dotted path to the test-email transport factory; tests inject a stub. |

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

No inbox or recipient-administration screen: the screens belong to later issues. Mandatory events,
escalation timings beyond the single overdue notice and email safety rules wait for OQ-20.
