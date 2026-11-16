# Application shell

[issue #24](https://github.com/JustApu/CheckYourAmbulance-archive/issues/24). Non-normative implementation notes. The product rules are SPEC §20 (NFR-1 to NFR-3), §16.4 and PRN-7.

## What it provides

- **Layout:** `cya/shell/templates/shell/base.html` is the one page frame (skip link, header with the signed-in user and a CSRF-protected sign-out, main navigation, connection banner, `<main>`). The sign-in, callback, status and sign-out pages use it. Feature pages extend it.
- **Compact navigation:** below 48rem, the Menu toggle stays in the header and expands the navigation list below it; wider layouts show the list directly.
- **Responsive and touch-friendly:** one stylesheet, mobile first, with a wider layout from 48rem. Every control is at least 48 px tall. It follows the light/dark preference and reduced motion.
- **Status never by color alone:** the current section is bold, underlined and marked `aria-current="page"`. Every save state has an icon and words.
- **Navigation:** Fleet, Inspection, Vehicle status and Deficiencies (every built-in role) and Administration (Administrator only), per SPEC §3.3. Fleet, Deficiencies and Administration are placeholder pages; Inspection is built in [issue #85](https://github.com/JustApu/CheckYourAmbulance-archive/issues/85) (see [crew inspection screens](crew-inspection-ui.md)) and Vehicle status in [issue #148](https://github.com/JustApu/CheckYourAmbulance-archive/issues/148) (see [vehicle status](vehicle-status.md#http-endpoints-and-screens)). Feature pages add `{% block head %}` and `{% block scripts %}` content to the frame.
- **Save and connection indicators (PRN-7, §16.4):**
  - `shell/save_state.html` renders `pending`, `saved` or `failed`; `cyaShell.setSaveState` also knows `unconfirmed` (a request with no definite answer, which may or may not have committed);
  - `cyaShell.setSaveState(element, state)` changes it, and treats an unknown state as `failed`;
  - a connection-lost banner (`role="alert"`) appears when the browser goes offline, and says an item marked Unconfirmed may have been recorded, so use its Retry rather than a new tap.

## Roles and enforcement

Navigation shows a section only if the user holds one of its roles, but the **server enforces every section** (SPEC §3.2): signed-out visitors are redirected to sign-in, and a user without a permitted role gets 403.

Roles come from `settings.CYA_ROLE_LOOKUP`, a dotted path to `lookup(user) -> iterable of role names`. It defaults to `cya.authorization.persistence.current_role_codes`, which reads current database roles on every call. Missing configuration yields no roles and no sections. A lookup that raises is treated the same way, and only the exception class is logged. Denied section requests are durably audited through the authorization backend; audit failure returns 503. Feature handlers must use the [request/command guards](authorization-implementation.md#policy-and-request-guards) and server-owned resource facts when real features arrive.

The Administration page lists its tools; today that is a link to [role administration](authorization-implementation.md#role-administration-over-http-issue-25). A section's sub-pages (route names `<section>-<page>`) keep the section highlighted in the navigation.

## Shared responsive and semantic UI conventions

- **Page structure:** feature pages extend the shared shell, use one descriptive `<h1>`, and divide long tasks with labelled sections and a logical heading order. Keep the current state and the next task near the top; put history and less common controls in secondary sections or disclosures.
- **Navigation and actions:** use links for navigation and buttons for actions. Give each control a concise, specific name that says what it does. Require an explicit user action to submit a clearly identified high-impact role or operational-status change. When a flow includes a preview, keep it distinct from the final confirmation.
- **Shared visual patterns:** use `.panel` to group related content, `.button` for action or link styling while retaining the correct element semantics, `.pill` for concise textual state labels, `.banner` for connection or blocking notices, and `.save-state` for pending/saved/failed/unconfirmed writes. Reserve `.button.primary` for the current primary action; do not use color or primary styling to imply that a new result is already recorded.
- **Forms:** associate every input with a visible `<label>`; group related options with `<fieldset>` and `<legend>`. Put field errors next to the field and use `role="alert"` for blocking errors. In an `.admin-form`, a field error is a `.field-error` element starting "Error:", referenced by the field's `aria-describedby`, and the field carries `aria-invalid="true"` (issue #130 part 6a). Use `role="status"` for save, pending, and non-urgent update messages so they can be announced without moving focus.
- **Status language:** show the status as text and, where helpful, an icon; never encode it by color alone. Keep an inspection result, obligation state, deficiency state, and vehicle operational status as separate facts. A button records an outcome; a recorded-result marker shows what was saved.
- **Touch, focus, and tables:** retain the shared 48 CSS-pixel minimum for controls (the issue target is at least 44 px where feasible). Preserve a visible 3 px `:focus-visible` outline. Put a wide table in a locally scrollable container with a useful accessible name and keyboard focus, rather than allowing it to widen the page.
- **Responsive layout:** build mobile-first and use the existing 48 rem shell breakpoint. Check 375, 600, 768, 1024 and 1440 CSS-pixel widths, portrait and landscape where relevant, and long synthetic labels. Check reflow at a half-width CSS viewport as a repeatable approximation of 200% browser zoom; verify the page itself does not scroll horizontally.
- **Preferences and recovery:** retain Auto, Light and Dark themes and reduced-motion behavior. Explain when a write is pending, saved, failed or unconfirmed. An unconfirmed write keeps its original operation identity for retry; do not replace it with a fresh action or silently discard entered data.

### Regression checklist

For a UI change, record which journey and view were exercised and check:

1. The required viewport sizes and relevant rotated view; no clipped controls or page-level horizontal overflow, including the half-width reflow approximation.
2. Touch target size, visible keyboard focus, logical tab order, keyboard activation, and focus restoration after dialogs or disclosures.
3. Accessible names for landmarks, headings, form controls and actions; status/error announcement roles; no state communicated by color alone.
4. Loading, stale, failed, offline and unconfirmed states when the changed workflow can reach them; verify that retry/refresh does not lose or duplicate a write.
5. Role-visible controls and server-side authorization remain aligned; status, deficiency, check outcome, and audit history keep their separate meanings.

`tools/browser_smoke.mjs` applies this checklist to synthetic rendered fixtures for the representative shell, inspection, readiness, deficiency, administrator role-change and supervisor status-request journeys. Its half-width check approximates browser zoom by reducing the CSS viewport; it is not a browser zoom or screen-reader certification. Use an actual screen reader and device/browser before claiming field-pilot accessibility.

### Manual screen-reader pass

For owner testing with VoiceOver or NVDA, use a synthetic account and vehicle. Record the screen reader, browser, operating system, and date, then check these announcements and actions:

1. **Shared shell:** navigate by landmark and heading; the skip link reaches the main heading, the menu reports expanded/collapsed state, the current navigation item is identified, and the theme selector has the name “Theme” and its Auto/Light/Dark choices.
2. **Inspection:** locate the vehicle and compartment headings; hear the item name, the recorded result separately from the available outcome actions, obligation state, deficiency state and vehicle status. Activate an outcome and confirm its saved or unconfirmed message is announced without confusing it with the button action.
3. **Supervisor status:** navigate the pending requests heading and each request's status, requester and reason. Confirm the Limited Service restrictions field is announced as required, Approve and Deny are distinct named actions, and the saved result or stale/error message is announced after the decision.
4. **Administrator roles:** find the user search, target, role and action fields by their labels; hear the preview summary before confirming; verify the confirmation button names the intended role/action and the result appears in the status region. Navigate the role-history region and hear its table headers with row values.
5. **Recovery and focus:** use only the keyboard to traverse each journey, operate disclosures and tables, and return from a confirmation/error to the expected control. Check that a live refresh does not silently move focus or replace a form containing an unresolved write.

This is a manual acceptance checklist, not a report that those screen-reader checks have already been performed.

## Stylesheet and script are inline

CSS and JS are included in the page (`shell.css`, `shell.js`) so the shell needs no static-file serving, which is a deployment decision not yet made (SPEC §17). If a Content-Security-Policy is added later, these need a nonce or a move to served files.

## Verification

- `cya/tests/test_shell.py` covers navigation per role and for a role union, server-side denial of hidden sections, fail-closed role lookup, escaping, markup and the indicator states. Focused Django tests cover the role-change and vehicle-status server workflows.
- `tools/browser_smoke.mjs` checks synthetic rendered fixtures in Chromium at the master issue's viewport widths, keyboard journeys, accessible role/name queries and half-width reflow. The status decision fixture also runs the production `status.js`; role administration exercises its preview and explicit confirmation form flow. These fixtures do not replace testing the actual server-rendered responses.
- Layout was previously checked in headless Firefox at 390 px (phone) and 1280 px (desktop) wide, on a rendered copy of the page. No current real-phone, actual 200% browser-zoom, physical keyboard, screen-reader, or contrast-tool verification is recorded. Complete those before claiming WCAG 2.2 AA or field-pilot accessibility.
