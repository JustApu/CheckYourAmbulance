# Application shell

[issue #24](https://github.com/JustApu/CheckYourAmbulance-archive/issues/24). Non-normative implementation notes. The product rules are SPEC §20 (NFR-1 to NFR-3), §16.4 and PRN-7.

## What it provides

- **Layout:** `cya/shell/templates/shell/base.html` is the one page frame (skip link, header with the brand and the signed-in user, main navigation, connection banner, `<main>`). The sign-in, callback, status and sign-out pages use it. Feature pages extend it.
- **Navigation by device (issue #194):**
  - every role sees the same tabs: **Home · Inspect · Problems · Vehicles · More**;
  - phones (under 48rem) get a bottom tab bar, fixed only when scripting runs, with page padding so nothing hides under it;
  - tablets (48 to 64rem) get a left rail of icons with labels;
  - laptops and desktops (64rem and wider) get a left sidebar.

  **More** shows a hamburger icon with the visible label "More". Its accessible name is "More menu": it keeps "Menu", as #194 asks for the hamburger control, and starts with the visible label (WCAG 2.5.3). Below 64rem it is a button (`aria-expanded`) that opens a panel; Escape, a click outside or choosing a link closes it. The panel holds Kits and Administration (each only for roles that may open them), the theme control and the CSRF-protected Sign out. At 64rem and wider the sidebar lists the panel in full and the button is hidden. Icons are decorative (`aria-hidden`); every place has a visible label.
- **Responsive and touch-friendly:** one stylesheet, mobile first, with a wider layout from 48rem. Every control is at least 48 px tall. It follows the light/dark preference and reduced motion.
- **Status never by color alone:** the current place is bold, has an accent bar and is marked `aria-current="page"`; More is marked the same way, with `aria-current="true"`, when one of its places is current, so the visible button announces it while the panel is closed. Every save state has an icon and words.
- **Places:** `TABS` in `cya/shell/navigation.py` (issue #194). **Problems** is the on-screen word for deficiencies: the SPEC, code, data and audit records keep "deficiency". **Vehicles** opens the vehicle status list; Supervisors and Administrators also get a link to fleet readiness there, and readiness pages mark Vehicles as current. `/fleet/` stays a placeholder that is no longer in the navigation. **Kits** links to the first kit page the user's roles permit. `SECTIONS` stays the access table that section pages check (SPEC §3.3); Inspection is built in [issue #85](https://github.com/JustApu/CheckYourAmbulance-archive/issues/85) (see [crew inspection screens](crew-inspection-ui.md)) and Vehicle status in [issue #148](https://github.com/JustApu/CheckYourAmbulance-archive/issues/148) (see [vehicle status](vehicle-status.md#http-endpoints-and-screens)). Feature pages add `{% block head %}` and `{% block scripts %}` content to the frame.
- **Save and connection indicators (PRN-7, §16.4):**
  - `shell/save_state.html` renders `pending`, `saved` or `failed`; `cyaShell.setSaveState` also knows `unconfirmed` (a request with no definite answer, which may or may not have committed);
  - `cyaShell.setSaveState(element, state)` changes it, and treats an unknown state as `failed`;
  - a connection-lost banner (`role="alert"`) appears when the browser goes offline, and says an item marked Unconfirmed may have been recorded, so use its Retry rather than a new tap.

## Layouts by screen size (issue #194)

- **Width:** pages keep a 60rem reading width. Dashboards and admin tables (Home, fleet readiness, the supervisor queue, and the vehicle, template, kit, position and role pages) set `{% block main_class %} class="main-wide"{% endblock %}` for up to 90rem.
- **Vehicle cards:** the Inspect and Vehicles lists are cards with a chevron, one column on phones, two from 48rem and three from 80rem. The whole card is the link.
- **Dashboards:** the supervisor queue shows its groups two across from 64rem; fleet readiness shows vehicles two across from 80rem, with notices and totals spanning the row.

## Sortable, filterable tables (issue #194)

`shell.js` enhances any `<table data-sortable>`:
- **Sorting:** each column header except those marked `data-nosort` (action columns) becomes a button. It sorts the rows in natural order ("Medic 2" before "Medic 10"), reports the order with `aria-sort` on that header only (WAI-ARIA), and announces "Sorted by …" through a polite status line.
- **Filtering:** `data-filter-label="…"` adds a labelled filter box above the table that hides non-matching rows and announces "Showing N of M".

Without the script the table is unchanged. Only rows already on the page are reordered or hidden; the server still decides what is listed.

These tables are marked: vehicle administration, checklist templates, kit definitions and kit instances. Compartment and system positions are left in their configured order on purpose.

## Home (issue #194)

`/` greets the signed-in user by first name (the first word of their display name) with the day and station time, then shows what to do next. `cya/shell/home.py` reads each part on its own; a part whose reader fails shows a short "unavailable" note and the rest still renders. A user with no role sees the earlier one-line page.

- **Everyone with a role:**
  - **In progress:** up to three of their open sessions, active within the last 6 hours, each with how many items are checked and how many by teammates, and **Continue inspection** (the first one is the primary button). With none, **Start an inspection** is the primary button.
  - **Due:** per vehicle, chips for overdue (and missed), due now, due soon and done, from the fleet-readiness picture.
  - **Problems:** up to five active problems, newest first, with the total, and **Report a problem**.
  - **Last check by you**, in station time.
- **Every vehicle:** Home always covers the whole active fleet (owner decision, 2026-11-17); there is no per-person vehicle list.
- **Supervisors and Administrators also get:**
  - four tiles: vehicles ready (In Service), checks done of those owed, open problems, and items waiting for them;
  - **Needs you:** status-change requests to review, fixes to verify, problems to assign;
  - a **Vehicles** table: status, checks done, overdue, problems.
- **Progress means task completion only.** There are no scores, rankings or comparisons between people.

## Roles and enforcement

Navigation shows a section only if the user holds one of its roles, but the **server enforces every section** (SPEC §3.2): signed-out visitors are redirected to sign-in, and a user without a permitted role gets 403.

Roles come from `settings.CYA_ROLE_LOOKUP`, a dotted path to `lookup(user) -> iterable of role names`. It defaults to `cya.authorization.persistence.current_role_codes`, which reads current database roles on every call. Missing configuration yields no roles and no sections. A lookup that raises is treated the same way, and only the exception class is logged. Denied section requests are durably audited through the authorization backend; audit failure returns 503. Feature handlers must use the [request/command guards](authorization-implementation.md#policy-and-request-guards) and server-owned resource facts when real features arrive.

The Administration page lists its tools in four headed groups of cards (issue #194): **People** (role administration), **Vehicles** (vehicles, compartments and systems, checklist templates, vehicle files), **Kits** (kit definitions, kit lists, kit instances, kit library) and **System** (outgoing mail). `ADMINISTRATION_GROUPS` in `cya/shell/views.py` sets the order; each page appears only if the user may open it, and a group with no such page is hidden. See [role administration](authorization-implementation.md#role-administration-over-http-issue-25). A tab owns URL path prefixes (`Tab.paths`), so a place's sub-pages keep it highlighted.

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
4. **Administrator roles:** find the active organization user list. In each user's row, hear the name, email, role checkboxes and their checked state, then the separate role-history link. Change one role and hear the proposed addition or removal; verify the Review changes button appears only after a change, review the summary, and confirm it. Open role history and hear the table headers and row values.
5. **Recovery and focus:** use only the keyboard to traverse each journey, operate disclosures and tables, and return from a confirmation/error to the expected control. Check that a live refresh does not silently move focus or replace a form containing an unresolved write.

**Scope note (2026-11-16):** The NVDA and Windows Night Sky records below predate the compact role matrix. They document the earlier card and selector controls, not the current checkbox controls. The owner deferred a new NVDA pass; actual screen-reader and Windows contrast checks of the current role matrix remain open.

**Manual pass record (2026-11-16):** NVDA 2026.3.1 Speech Viewer with Chrome 157.0.8103.79 on Windows 10 Pro 26H2 (build 26300.9974), using the real Django-rendered organization-user and role-history templates populated with synthetic users in Codex's ephemeral MariaDB and served on localhost. NVDA exposed the page and target headings, search label, current roles, a distinct selector name for each user, the separate history link, named history region, table headers, event values and actor. After styling, at actual 200% Chrome zoom the user cards and controls reflowed vertically within the viewport; the page scrolled vertically without page-level horizontal overflow. The history table retained its own horizontal scroll region, and NVDA announced the region and table structure. This manual pass did not submit a role change or verify its live status announcement. The Playwright Chromium smoke also uses forced-colors emulation and checks system colors for cards, badges, fields, buttons and links; this simulates the browser media feature and is not an actual Windows high-contrast check. Other screens and field-pilot accessibility have not been checked. This is not a complete accessibility audit or a WCAG conformance claim.

**Theme contrast calculation (2026-11-16):** Ratios were calculated from the literal shell.css sRGB hex tokens using the WCAG relative-luminance formula (not a browser sampling tool). Light theme: body text/background 17.40:1; muted text/background 8.86:1; control border/background 4.54:1; focus/accent against background 8.04:1; primary text/accent 8.04:1; success 9.12:1; pending 8.31:1; error 9.45:1. Dark theme: 15.64:1, 10.22:1, 5.94:1, 8.78:1, 8.52:1, 10.88:1, 10.87:1, and 10.84:1 in the same order. The disabled inspection buttons previously applied 60% opacity, producing primary-label/button ratios of 3.11:1 light and 3.90:1 dark. Replaced that treatment with explicit muted text, page background, and dashed border so disabled buttons preserve theme contrast while retaining a distinct state; disabled controls are not used as evidence of WCAG conformance. This token calculation does not assess every component state or anti-aliasing, and the full application still needs broader accessibility review.

**Manual Windows contrast-theme pass (2026-11-16):** The owner enabled the Windows Night Sky contrast theme on Windows 10 Pro 26H2 (build 26300.9974). In Chrome 157.0.8103.79, the current-source synthetic role-administration fixture using the production shell and inspection styles was visually inspected. Headings and body text, role pills, links, role select/options, review buttons, the disabled confirmation button, and the history table remained visible. The disabled action remained distinct with muted text and a dashed border. Keyboard focus on the role selector showed a clear system-color outline; the focused option remained legible. The history table retained its own horizontal scroll area. This verifies the shared styles in the synthetic fixture under the actual Windows contrast theme, not a Django-rendered production response or every workflow. No role was changed. The owner left Night Sky enabled after the check.

## Design tokens (issue #194)

`shell.css` starts with the owner-approved tokens. Use them, not new hex values:

- **Surfaces.** `--page` is the page background and `--bg` the white card and control surface (deep navy in dark mode). `--fg` is text and `--muted` secondary text.
- **Lines.** `--line` is for form-control borders, at 3:1 or more as WCAG 1.4.11 requires. `--line-soft` is for decorative dividers, cards and table rows.
- **Brand.** `--accent` is navy (`#14305C`), used for links, focus, primary buttons and the brand name. `--brand-red` is used only for the header's top stripe. `--danger` marks destructive actions.
- **Status, always with a word.**
  - `--ok-*`: green, for done or passed;
  - `--wait-*`: amber, for due soon or needs attention;
  - `--bad-*`: red, for overdue, failed or out of service;
  - `--info-*`: blue, for open;
  - `--neutral-*`: gray, for not started.
- **Scale.** `--space-1` to `--space-6`, `--radius-control`, `--radius-card` and `--radius-pill`. `--font-sans` is the system font stack, and `.tabular` gives even-width digits for counts and times.
- **Buttons.**
  - `.button` is navy-outlined, the secondary style;
  - `.button.primary` is filled navy, one per screen;
  - `.button.danger` is red-outlined;
  - a disabled button gets a dashed border.
- **Chips.** `.pill`, plus `.pill-ok`, `.pill-wait`, `.pill-needs-attention`, `.pill-bad`, `.pill-info` or `.pill-neutral`. In forced-colors mode every chip gets a system-color border.

Every text pair measures at least 4.5:1 and every control border at least 3:1, in both themes. The pairs are recorded on issue #194 and its token PR.

## App icon and install (issue #194)

Phones can add the app to the home screen. It then opens full screen with its own icon.

- **Icon.** The approved concept G: a white check above "CYA" inside a navy shield with a red top band. The sources are in `cya/shell/brand/`:
  - `icon.svg`: the app icon, a white shield on a navy tile;
  - `maskable.svg`: Android's full-bleed version, with the mark inside the safe zone;
  - `favicon.svg`: the shield and check, no letters, for browser tabs;
  - `monochrome.svg`: the check cut out of the shield, for systems that use only the icon's shape.

  `render.mjs` renders the PNGs (192, 512, the maskable 512, the 180 Apple touch icon and the 32 favicon) in Edge. Run it again and commit its output whenever an SVG changes. The PNGs fix the "CYA" letters, which in an SVG would depend on the device's fonts.
- **Serving.** The app has no static-file pipeline, so `cya/shell/brand_views.py` serves the icons and the manifest:
  - `/icons/<name>` serves only the files in its fixed `ICONS` table, cached for a year;
  - `/manifest.webmanifest` gives the name "Check Your Ambulance", the short name "CYA", `display: standalone` and theme color navy `#14305C`, cached for a day.

  Both answer without sign-in, because browsers fetch the manifest without credentials.
- **Page head.** `base.html` links the SVG and PNG favicons, the Apple touch icon and the manifest. It sets the theme color (navy in light mode, `#0B1426` in dark mode) and the home-screen title "CYA".
- **No service worker.** The app stays online-only (SPEC).
- **Changing an icon.** Give the new file a new name: browsers keep the old one for a year.

## Stylesheet and script are inline

CSS and JS are included in the page (`shell.css`, `shell.js`) so the shell needs no static-file serving, which is a deployment decision not yet made (SPEC §17). If a Content-Security-Policy is added later, these need a nonce or a move to served files.

## Verification

- `cya/tests/test_shell.py` covers navigation per role and for a role union, server-side denial of hidden sections, fail-closed role lookup, escaping, markup and the indicator states. Focused Django tests cover the role-change and vehicle-status server workflows.
- `tools/browser_smoke.mjs` checks synthetic rendered fixtures in Chromium at the master issue's viewport widths, keyboard journeys, accessible role/name queries and half-width reflow. The status decision fixture also runs the production status.js; the synthetic role fixture mirrors the organization user list, per-user role review and confirmation, and separate role-history page. It checks skip-link focus on the main landmark and checks role and inspection fixtures in Chromium forced-colors emulation against system color keywords. It does not prove the server-rendered Django page or actual Windows high-contrast behavior. These fixtures do not replace testing the actual server-rendered responses.
- Layout was previously checked in headless Firefox at 390 px (phone) and 1280 px (desktop) wide, on a rendered copy of the page. This task also recorded an actual 200% Chrome zoom check and a partial NVDA pass for the styled administrator role list and history detail (see the dated record above). No real-phone or other workflow screen-reader pass, actual Windows forced-colors check on another workflow, or sampled contrast-tool verification was performed; the token contrast calculation is recorded above. Complete the remaining checks before making a WCAG 2.2 AA or field-pilot accessibility claim.
