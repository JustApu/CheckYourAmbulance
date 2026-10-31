# Application shell

[issue #24](https://github.com/JustApu/CheckYourAmbulance-archive/issues/24). Non-normative implementation notes. The product rules are SPEC §20 (NFR-1 to NFR-3), §16.4 and PRN-7.

## What it provides

- **Layout:** `cya/shell/templates/shell/base.html` is the one page frame (skip link, header with the signed-in user and a CSRF-protected sign-out, main navigation, connection banner, `<main>`). The sign-in, callback, status and sign-out pages use it. Feature pages extend it.
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

## Stylesheet and script are inline

CSS and JS are included in the page (`shell.css`, `shell.js`) so the shell needs no static-file serving, which is a deployment decision not yet made (SPEC §17). If a Content-Security-Policy is added later, these need a nonce or a move to served files.

## Verification

- `cya/tests/test_shell.py` covers navigation per role and for a role union, server-side denial of hidden sections, fail-closed role lookup, escaping, markup and the indicator states.
- Layout was checked in headless Firefox at 390 px (phone) and 1280 px (desktop) wide, on a rendered copy of the page. It was **not** checked on a real phone, with a screen reader, or in other browsers, and no contrast tool was run; those remain for the owner's testing before the accessibility target (WCAG 2.2 AA) is claimed.
