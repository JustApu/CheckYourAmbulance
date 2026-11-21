#!/usr/bin/env node
// Deterministic browser smoke against real shell/inspection CSS and synthetic markup.

import assert from "node:assert/strict";
import { mkdirSync, readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { captureBrowserEvidence } from "./capture_browser_evidence.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const cssPaths = [
  "cya/shell/templates/shell/shell.css",
  "cya/inspections/templates/inspections/inspection.css",
  "cya/attention/templates/attention/readiness.css",
];
const css = cssPaths.map((file) => readFileSync(path.join(root, file), "utf8")).join("\n");
const shellScript = readFileSync(
  path.join(root, "cya/shell/templates/shell/shell.js"),
  "utf8",
);
const statusScript = readFileSync(
  path.join(root, "cya/operations/templates/operations/status.js"),
  "utf8",
);
const checklistScript = readFileSync(
  path.join(root, "cya/inspections/templates/inspections/checklist.js"),
  "utf8",
);
const navIcon = '<svg class="app-nav-icon" viewBox="0 0 24 24" width="24" height="24" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6.5h16M4 12h16M4 17.5h16"/></svg>';
const themeBootstrap = readFileSync(
  path.join(root, "cya/shell/templates/shell/theme_bootstrap.js"),
  "utf8",
);
const timezoneScript = readFileSync(
  path.join(root, "cya/shell/templates/shell/timezone.js"),
  "utf8",
);
const html = `<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Synthetic vehicle inspection</title><script>${themeBootstrap}</script><script>window.__themeBeforeStyles = document.documentElement.getAttribute("data-theme");</script><style>${css}</style></head>
<body class="has-app-nav">
<a class="skip-link" href="#main">Skip to main content</a>
<header class="site-header"><a class="brand" href="#main">Check Your Ambulance</a><div class="header-tools"><div class="account"><span class="account-name">Sam Example</span></div></div></header>
<nav class="app-nav" aria-label="Main"><ul class="app-nav-tabs"><li><a class="app-nav-link" href="#home">${navIcon}<span class="app-nav-label">Home</span></a></li><li><a class="app-nav-link" href="#main" aria-current="page">${navIcon}<span class="app-nav-label">Inspect</span></a></li><li><a class="app-nav-link" href="#deficiency" aria-label="Problems, 57 open">${navIcon}<span class="app-nav-label">Problems</span><span class="app-nav-badge" aria-hidden="true">57</span></a></li><li><a class="app-nav-link" href="#status">${navIcon}<span class="app-nav-label">Vehicles</span></a></li><li class="app-nav-more-tab"><button id="app-nav-more" class="app-nav-link app-nav-more" type="button" aria-label="More menu" aria-controls="app-nav-more-panel" aria-expanded="false" hidden>${navIcon}<span class="app-nav-label">More</span></button></li></ul><div id="app-nav-more-panel" class="app-nav-more-panel"><ul class="app-nav-more-list"><li><a href="#kits">${navIcon}<span>Kits</span></a></li><li><a href="#administration">${navIcon}<span>Administration</span></a></li></ul><div class="app-nav-settings"><fieldset class="theme-control"><legend>Theme</legend><div class="theme-choices"><label class="theme-choice"><input type="radio" name="theme" value="auto" checked><span>Auto</span></label><label class="theme-choice"><input type="radio" name="theme" value="light"><span>Light</span></label><label class="theme-choice"><input type="radio" name="theme" value="dark"><span>Dark</span></label></div></fieldset><div class="nav-account"><span class="nav-account-name">Sam Example</span><form><button class="link-button" type="button">Sign out</button></form></div></div></div></nav>
<div id="connection-banner" class="banner banner-error" role="alert" hidden><span class="indicator-icon" aria-hidden="true">&#9888;</span><strong>Connection lost.</strong> Changes are not being saved. An item marked Unconfirmed may have been recorded.</div>
<main id="main" tabindex="-1"><h1>Unit 12 inspection</h1>
<section id="status" class="status-line" aria-label="Vehicle status"><span class="pill status-pill" data-status="IN_SERVICE">In service</span><span class="pill status-pill" data-status="OUT_OF_SERVICE">Out of service</span><span class="pill pill-attention">Open problem</span></section>
<ul class="vehicle-list"><li><a class="vehicle-link" href="#status"><span class="vehicle-name">Unit 12 — Synthetic Regional Response Ambulance</span><span class="vehicle-badges"><span class="pill pending-pill" data-count="2">2 pending requests</span><span class="pill status-pill" data-status="OUT_OF_SERVICE">Out of service</span></span></a></li></ul>
<section id="deficiency" class="item item-attention" aria-labelledby="item-title"><div class="item-head"><h2 id="item-title">Synthetic oxygen cylinder</h2><span class="pill pill-attention">Known problem</span></div><p>Pressure below the approved range.</p><div class="item-controls" aria-label="Inspection actions"><button class="button primary" type="button" aria-label="Record passed">Passed</button><button class="button" type="button" aria-label="Record unable to inspect">Unable to inspect</button></div><label class="panel">Inspection note<input aria-label="Inspection note" type="text" value="Synthetic check"></label><p id="save-state" class="save-state" role="status"><span class="indicator-icon" aria-hidden="true">?</span><span class="save-state-text">Unconfirmed</span></p></section>
<section class="panel deficiency-overview" aria-labelledby="deficiency-overview-title"><h2 id="deficiency-overview-title">Synthetic oxygen cylinder pressure issue</h2><p class="deficiency-state"><strong>In Progress</strong> · High severity</p><p>Synthetic Ambulance 12</p><dl class="deficiency-facts"><dt>Affected scope</dt><dd>Synthetic oxygen cylinder</dd><dt>Assignee</dt><dd>Sam Example</dd><dt>Governing deadline</dt><dd>2026-11-15 12:00 UTC · Overdue</dd></dl></section>
<section class="panel deficiency-next-action" aria-labelledby="next-action-title"><h2 id="next-action-title">Next action</h2><form class="workflow-write"><p>Record that the repair is complete and request verification.</p><button class="button primary" type="button">Record repair</button></form></section>
<details class="panel deficiency-details" id="deficiency-work-updates"><summary>Work updates and dependencies</summary><form class="panel workflow-write"><h2>Add progress note</h2><label>Progress note<textarea aria-label="Progress note"></textarea></label><button class="button" type="button">Add note</button></form></details>

</main><script>${shellScript}</script></body></html>`;
const viewports = [
  { name: "phone-375", width: 375, height: 812 },
  { name: "large-phone-600", width: 600, height: 900 },
  { name: "tablet-portrait-768", width: 768, height: 1024 },
  { name: "tablet-landscape-1024", width: 1024, height: 768 },
  { name: "desktop-1440", width: 1440, height: 900 },
];
const sampleRows = [
  { item_id: "item-oxygen", item_label: "Oxygen", kit_id: null, kit_label: null, compartment_id: "comp-cab", compartment_label: "Cab", system_id: null, system_label: null, checklist_version_id: "version-a", kind: "PRESENCE", instructions: "", display_order: 1, obligation_state: "SATISFIED", obligation_outcome: "PASSED", due_at: null, item_attention: "CLEAR", needs_action: false, check_suspended: false, deficiencies: [], latest_check_id: null, latest_check_outcome: null, latest_check_at: null, latest_check_by: null, expected_quantity: null, unit: null, allowed_outcomes: ["PASSED", "FAILED", "UNABLE_TO_INSPECT"] },
  { item_id: "item-mask", item_label: "Oxygen mask", kit_id: "kit-one", kit_label: "Airway Kit", compartment_id: "comp-cab", compartment_label: "Cab", system_id: null, system_label: null, checklist_version_id: "version-a", kind: "PRESENCE", instructions: "", display_order: 2, obligation_state: "DUE", obligation_outcome: null, due_at: null, item_attention: "ATTENTION", needs_action: true, check_suspended: false, deficiencies: [{ id: "def-a", severity_label: "Medium", status: "OPEN", summary: "Synthetic issue" }], latest_check_id: null, latest_check_outcome: null, latest_check_at: null, latest_check_by: null, expected_quantity: null, unit: null, allowed_outcomes: ["PASSED", "FAILED", "UNABLE_TO_INSPECT"] },
  { item_id: "item-mask", item_label: "Oxygen mask", kit_id: "kit-two", kit_label: "Airway Kit", compartment_id: "comp-cab", compartment_label: "Cab", system_id: null, system_label: null, checklist_version_id: "version-a", kind: "PRESENCE", instructions: "", display_order: 3, obligation_state: "OVERDUE", obligation_outcome: null, due_at: null, item_attention: "CLEAR", needs_action: true, check_suspended: false, deficiencies: [], latest_check_id: null, latest_check_outcome: null, latest_check_at: null, latest_check_by: null, expected_quantity: null, unit: null, allowed_outcomes: ["PASSED", "FAILED", "UNABLE_TO_INSPECT"] },
  { item_id: "item-radio", item_label: "Radio", kit_id: null, kit_label: null, compartment_id: "comp-cab", compartment_label: "Cab", system_id: "sys-radio", system_label: "Communications", checklist_version_id: "version-a", kind: "PRESENCE", instructions: "", display_order: 4, obligation_state: "SUSPENDED", obligation_outcome: null, due_at: null, item_attention: "ATTENTION", needs_action: false, check_suspended: true, deficiencies: [], latest_check_id: null, latest_check_outcome: null, latest_check_at: null, latest_check_by: null, expected_quantity: null, unit: null, allowed_outcomes: ["PASSED", "FAILED", "UNABLE_TO_INSPECT"] },
];
const outcomeRows = [
  { item_id: "item-fail", item_label: "Brake light", kit_id: null, kit_label: null, compartment_id: "comp-cab", compartment_label: "Cab", system_id: null, system_label: null, checklist_version_id: "version-a", kind: "PRESENCE", instructions: "", display_order: 1, obligation_state: "DUE", obligation_outcome: null, due_at: "2026-06-01T06:00:00Z", item_attention: "ATTENTION", needs_action: true, check_suspended: false, deficiencies: [{ id: "def-fail", severity_label: "High", status: "OPEN", summary: "Synthetic brake issue" }], latest_check_id: "check-fail", latest_check_outcome: "FAILED", latest_check_at: "2026-06-01T00:00:00Z", latest_check_by: "Synthetic EMT", expected_quantity: null, unit: null, allowed_outcomes: ["PASSED", "FAILED", "UNABLE_TO_INSPECT"] },
  { item_id: "item-pass-open", item_label: "Oxygen regulator", kit_id: null, kit_label: null, compartment_id: "comp-cab", compartment_label: "Cab", system_id: null, system_label: null, checklist_version_id: "version-a", kind: "PRESENCE", instructions: "", display_order: 2, obligation_state: "SATISFIED", obligation_outcome: "PASSED", due_at: "2026-06-01T06:00:00Z", item_attention: "ATTENTION", needs_action: false, check_suspended: false, deficiencies: [{ id: "def-pass", severity_label: "Medium", status: "OPEN", summary: "Synthetic regulator issue" }], latest_check_id: "check-pass", latest_check_outcome: "PASSED", latest_check_at: "2026-06-01T00:05:00Z", latest_check_by: "Synthetic EMT", expected_quantity: null, unit: null, allowed_outcomes: ["PASSED", "FAILED", "UNABLE_TO_INSPECT"] },
  { item_id: "item-prior-pass", item_label: "Suction unit", kit_id: null, kit_label: null, compartment_id: "comp-cab", compartment_label: "Cab", system_id: null, system_label: null, checklist_version_id: "version-a", kind: "PRESENCE", instructions: "", display_order: 3, obligation_state: "DUE", obligation_outcome: null, due_at: "2026-06-01T06:00:00Z", item_attention: "CLEAR", needs_action: true, check_suspended: false, deficiencies: [], latest_check_id: "check-prior", latest_check_outcome: "PASSED", latest_check_at: "2026-05-31T00:00:00Z", latest_check_by: "Synthetic EMT", expected_quantity: null, unit: null, allowed_outcomes: ["PASSED", "FAILED", "UNABLE_TO_INSPECT"] },
  { item_id: "item-unable", item_label: "Portable monitor", kit_id: null, kit_label: null, compartment_id: "comp-cab", compartment_label: "Cab", system_id: null, system_label: null, checklist_version_id: "version-a", kind: "PRESENCE", instructions: "", display_order: 4, obligation_state: "DUE", obligation_outcome: null, due_at: "2026-06-01T06:00:00Z", item_attention: "ATTENTION", needs_action: true, check_suspended: false, deficiencies: [{ id: "def-unable", severity_label: "Low", status: "PENDING_VERIFICATION", summary: "Synthetic monitor issue" }], latest_check_id: "check-unable", latest_check_outcome: "UNABLE_TO_INSPECT", latest_check_at: "2026-06-01T00:10:00Z", latest_check_by: "Synthetic EMT", expected_quantity: null, unit: null, allowed_outcomes: ["PASSED", "FAILED", "UNABLE_TO_INSPECT"] },
];
function inspectionMarkup(userId, sessionId, timeZone = "America/New_York") {
  const config = { user_id: userId, session_id: sessionId, vehicle_id: "vehicle-a", checklist_version_id: "version-a", time_zone: timeZone, state: "OPEN", view: "NORMAL", needs_attention_only: false, urls: { checklist: "/api/checklist", checks: "/api/checks", odometer: "/api/odometer", preferences: "/api/preferences", reopen: "/api/reopen", vehicle: "/vehicle", socket: "/socket", suggestions: "/suggestions", deficiency_detail: "/deficiency/DEFICIENCY/" } };
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>${css}</style></head><body>
  <main><section><p id="op-status">In service</p><p id="op-restrictions"></p></section>
  <div id="live-banner" hidden><span id="live-detail"></span></div><div id="stale" hidden></div><p id="session-message" role="status"></p>
  <form id="csrf-holder"><input name="csrfmiddlewaretoken" value="synthetic"></form>
  <section aria-labelledby="odo-heading" class="panel vehicle-readings"><h2 id="odo-heading">Vehicle readings</h2><form id="odometer-form" novalidate><div class="vehicle-readings-fields"><label class="vehicle-reading-field" for="odo-value"><span>Mileage <span class="muted">(miles)</span></span><input id="odo-value" class="number-input" type="text" inputmode="decimal" autocomplete="off" required></label><input id="odo-unit" type="hidden" value="mi"><label class="vehicle-reading-field" for="odo-hours"><span>Engine hours (optional)</span><input id="odo-hours" class="number-input" type="text" inputmode="decimal" autocomplete="off"></label><button type="submit" class="button primary" aria-describedby="readings-save-help">Record readings</button></div></form><p id="readings-save-help" class="muted readings-save-note">Audited history: select Record after entering complete values.</p><div id="odo-confirm" hidden><label for="odo-why">Why is this reading correct? (optional)</label><textarea id="odo-why" rows="2" maxlength="1000"></textarea><button id="odo-confirm-button" class="button" type="button">The reading is correct. Save it.</button></div><p id="odometer-state" class="save-state" role="status" hidden><span class="indicator-icon"></span><span class="save-state-text"></span></p></section>
  <section><h2 id="checklist-heading" tabindex="-1">Checklist</h2><div class="view-controls"><button id="view-normal" class="button" type="button">Normal</button><button id="view-priority" class="button" type="button">Priority</button><label><input id="only-attention" type="checkbox"> Needs Attention Only</label></div><p id="pref-state" role="status"></p>
  <div id="layout-controls" class="view-controls" role="group" aria-label="Inspection layout"><button id="layout-compartments" class="button" type="button">Compartments</button><button id="layout-flat" class="button" type="button">All items</button></div>
  <div id="checklist-body" class="checklist-body"><section id="area-overview"><h3 id="area-overview-heading" tabindex="-1">Choose an area or compartment</h3><p class="muted">Progress counts checks for the current inspection window. Known problems remain open until resolved.</p><div id="area-cards" class="area-cards"></div></section>
  <section id="area-detail" hidden><button id="area-overview-button" class="button" type="button">All areas and compartments</button><h3 id="area-heading" tabindex="-1"></h3></section><p id="screen-notice" hidden></p><ol id="items" class="items"></ol></div></section>
  <dialog id="conflict-dialog"><p id="conflict-text"></p><button id="conflict-cancel" type="button">Cancel</button><button id="conflict-proceed" type="button">Proceed</button></dialog>
  <dialog id="outcome-dialog"><h2 id="outcome-title"></h2><form id="outcome-form"><textarea name="outcome_explanation"></textarea><select id="outcome-existing"></select><p id="outcome-matches"></p><p id="outcome-error"></p><fieldset id="outcome-create-fields"></fieldset><button id="outcome-cancel" type="button">Cancel</button></form></dialog>
  </main><script id="cya-config" type="application/json">${JSON.stringify(config)}</script>
  <script>${timezoneScript}</script><script>window.WebSocket = class { constructor() { setTimeout(() => { if (this.onopen) this.onopen(); }, 0); } close() {} };</script><script>${checklistScript}</script></body></html>`;
}
async function testChecklistTimezoneFallback(viewport) {
  const page = await browser.newPage({ viewport, timezoneId: "Pacific/Auckland" });
  await page.route("http://inspection.test/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith("/api/")) {
      const body = url.pathname === "/api/checklist"
        ? { status: "OK", items: [outcomeRows[0]], group_progress: [
          { key: "compartment:comp-cab", label: "Cab", total: 1, checked: 0, due: 1, due_soon: 0, overdue: 0, not_due: 0, no_obligation: 0, exceptions: 0, open_deficiencies: 1 },
        ], vehicle_status: { status: "IN_SERVICE", restrictions: "" } }
        : { status: "COMMITTED", result: {} };
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
      return;
    }
    await route.fulfill({ status: 200, contentType: "text/html", body: inspectionMarkup("emt-a", "session-a", "Invalid/Zone") });
  });
  try {
    await page.goto("http://inspection.test/fallback", { waitUntil: "load" });
    await page.getByRole("button", { name: /^Cab / }).click();
    const row = page.locator('#items > li[data-item-key="item-fail|"]');
    await row.waitFor();
    assert.match(await row.innerText(), /2026-06-01 00:00:00 UTC/, `${viewport.name}: timestamp fallback did not state UTC`);
  } finally {
    await page.close();
  }
}
async function testEmptyAndLoadingStates(viewport) {
  // Issue #194 item 9: an empty list explains itself with a 48px next step; loading placeholders
  // shimmer only without reduced motion and are hidden from assistive technology.
  const page = await browser.newPage({ viewport });
  const markup = `<!doctype html><html lang="en" class="js"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1"><title>Empty</title><style>${css}</style></head>
    <body><main id="main"><h1>Problems</h1><ul class="plain"><li class="panel"><div class="empty-state"><p class="empty-state-message">No open problems right now.</p><p class="empty-state-hint">When a check fails or someone reports a problem, it is listed here. To report one, open the vehicle in Inspect.</p><p class="empty-state-action"><a class="button" href="#inspect">Go to Inspect</a></p></div></li></ul>
    <div id="area-cards" class="area-cards"><div class="skeleton skeleton-card" aria-hidden="true"></div><div class="skeleton skeleton-card" aria-hidden="true"></div></div></main></body></html>`;
  try {
    await page.route("http://empty.test/**", (route) => route.fulfill({ status: 200, contentType: "text/html", body: markup }));
    await page.goto("http://empty.test/", { waitUntil: "load" });
    const action = page.getByRole("link", { name: "Go to Inspect" });
    assert.ok((await action.boundingBox()).height >= 48, `${viewport.name}: the empty-state action is shorter than 48 CSS pixels`);
    const skeleton = page.locator(".skeleton").first();
    assert.ok((await skeleton.boundingBox()).height >= 48, `${viewport.name}: the loading placeholder collapsed`);
    assert.equal(await skeleton.evaluate((node) => getComputedStyle(node).animationName), "cya-skeleton");
    assert.equal(await page.getByRole("main").getByText("No open problems right now.").count(), 1);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(overflow <= 1, `${viewport.name}: the empty state overflows horizontally`);
    await page.emulateMedia({ reducedMotion: "reduce" });
    assert.equal(await skeleton.evaluate((node) => getComputedStyle(node).animationName), "none", `${viewport.name}: placeholders shimmer under reduced motion`);
    console.log(`${viewport.name}: empty-state next step and loading placeholders passed`);
  } finally {
    await page.close();
  }
}

async function testKeyboardShortcuts(viewport) {
  // Issue #194 item 12: keyboard shortcuts with visible hints and an off switch (WCAG 2.1.4).
  const page = await browser.newPage({ viewport });
  const link = (key, label) => `<li><a class="app-nav-link" href="#${label.toLowerCase()}" data-shortcut-go="${key}" aria-keyshortcuts="g ${key}">${navIcon}<span class="app-nav-label">${label}</span><kbd class="shortcut-hint" aria-hidden="true">G ${key.toUpperCase()}</kbd></a></li>`;
  const markup = `<!doctype html><html lang="en" class="js"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Shortcuts</title><style>${css}</style></head>
    <body class="has-app-nav"><header class="site-header"><a class="brand" href="#">Check Your Ambulance</a></header>
    <nav class="app-nav" aria-label="Main"><ul class="app-nav-tabs">${link("h", "Home")}${link("p", "Problems")}<li class="app-nav-more-tab"><button id="app-nav-more" class="app-nav-link app-nav-more" type="button" aria-label="More menu" aria-controls="app-nav-more-panel" aria-expanded="false" hidden>${navIcon}<span class="app-nav-label">More</span></button></li></ul>
    <div id="app-nav-more-panel" class="app-nav-more-panel"><div class="app-nav-settings"><button type="button" id="shortcuts-open" class="nav-quiet shortcuts-open" aria-keyshortcuts="?">Keyboard shortcuts</button></div></div></nav>
    <dialog id="shortcuts-dialog" class="shortcuts-dialog" aria-labelledby="shortcuts-title"><h2 id="shortcuts-title">Keyboard shortcuts</h2><dl class="shortcuts-list"><dt><kbd>?</kbd></dt><dd>Show this list</dd></dl>
    <p><label class="check"><input type="checkbox" id="shortcuts-enabled" checked> Use keyboard shortcuts</label></p><p><button type="button" id="shortcuts-close" class="button">Close</button></p></dialog>
    <main id="main"><h1>Shortcuts</h1><label for="q">Search</label><input id="q" type="search">
    <p><button type="button" id="next" class="button primary" data-shortcut-key="n" aria-keyshortcuts="n">Next unchecked item<kbd class="shortcut-hint" aria-hidden="true">N</kbd></button></p></main>
    <script>window.nextPresses = 0; document.getElementById("next").addEventListener("click", function () { window.nextPresses += 1; });</script>
    <script>${shellScript}</script></body></html>`;
  try {
    await page.route("http://keys.test/**", (route) => route.fulfill({ status: 200, contentType: "text/html", body: markup }));
    await page.goto("http://keys.test/", { waitUntil: "load" });
    const dialog = page.locator("#shortcuts-dialog");
    const next = page.getByRole("button", { name: "Next unchecked item" });
    assert.equal(await next.getAttribute("aria-keyshortcuts"), "n");
    await next.focus();
    await page.keyboard.press("Shift+Slash");
    assert.equal(await dialog.evaluate((node) => node.open), true, `${viewport.name}: ? did not open the shortcut list`);
    await page.keyboard.press("Escape");
    assert.equal(await dialog.evaluate((node) => node.open), false);
    assert.equal(await page.evaluate(() => document.activeElement.id), "next", `${viewport.name}: closing the list did not return focus`);
    await page.keyboard.press("n");
    assert.equal(await page.evaluate(() => window.nextPresses), 1, `${viewport.name}: N did not press Next unchecked item`);
    await page.keyboard.press("Shift+KeyN");
    assert.equal(await page.evaluate(() => window.nextPresses), 1, `${viewport.name}: Shift+N pressed Next unchecked item`);
    await page.keyboard.press("g");
    await page.keyboard.press("Shift+KeyP");
    assert.equal(await page.evaluate(() => location.hash), "", `${viewport.name}: G then Shift+P went to a place`);
    await page.keyboard.press("Slash");
    assert.equal(await page.evaluate(() => document.activeElement.id), "q", `${viewport.name}: / did not go to the search box`);
    await page.keyboard.type("gp");
    assert.equal(await page.evaluate(() => location.hash), "", `${viewport.name}: typing in a field triggered a shortcut`);
    assert.equal(await page.locator("#q").inputValue(), "gp");
    await next.focus();
    await page.keyboard.press("Control+g");
    await page.keyboard.press("p");
    assert.equal(await page.evaluate(() => location.hash), "", `${viewport.name}: Ctrl+G started a place shortcut`);
    await page.keyboard.press("g");
    await page.keyboard.press("p");
    assert.equal(await page.evaluate(() => location.hash), "#problems", `${viewport.name}: G then P did not go to Problems`);
    const hint = page.locator(".app-nav-link .shortcut-hint").first();
    const wide = viewport.width >= 1024;
    assert.equal(await hint.isVisible(), wide, `${viewport.name}: sidebar shortcut hints ${wide ? "missing" : "shown on a narrow screen"}`);
    assert.equal(await next.locator(".shortcut-hint").isVisible(), true, `${viewport.name}: the Next hint is missing with a mouse and keyboard`);
    await page.keyboard.press("Shift+Slash");
    await page.getByLabel("Use keyboard shortcuts").uncheck();
    await page.getByRole("button", { name: "Close" }).click();
    await page.evaluate(() => { location.hash = ""; });
    await next.focus();
    await page.keyboard.press("g");
    await page.keyboard.press("h");
    await page.keyboard.press("n");
    await page.keyboard.press("Shift+Slash");
    assert.equal(await page.evaluate(() => location.hash), "", `${viewport.name}: shortcuts still worked when switched off`);
    assert.equal(await page.evaluate(() => window.nextPresses), 1);
    assert.equal(await dialog.evaluate((node) => node.open), false, `${viewport.name}: ? still opened the list when switched off`);
    assert.equal(await next.locator(".shortcut-hint").isVisible(), false, `${viewport.name}: hints still shown when switched off`);
    assert.equal(await page.evaluate(() => localStorage.getItem("cya.shortcuts")), "off");
    await page.reload({ waitUntil: "load" });
    assert.equal(await page.evaluate(() => document.documentElement.classList.contains("shortcuts-off")), true, `${viewport.name}: the off switch was not remembered`);
    await page.evaluate(() => localStorage.removeItem("cya.shortcuts"));
    console.log(`${viewport.name}: keyboard shortcuts, hints and the off switch passed`);
  } finally {
    await page.close();
  }
}

async function testTapFeedback(viewport) {
  // Issue #194 item 8: a pressed state, a Saved toast that never takes focus or blocks taps and
  // clears itself, and a saved check mark that only animates without reduced motion.
  const page = await browser.newPage({ viewport });
  const markup = `<!doctype html><html lang="en" class="js"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1"><title>Feedback</title><style>${css}</style></head>
    <body class="has-app-nav"><nav class="app-nav" aria-label="Main"><ul class="app-nav-tabs"><li><a class="app-nav-link" href="#home">Home</a></li><li><a class="app-nav-link" href="#inspect">Inspect</a></li></ul></nav>
    <main id="main"><h1>Feedback</h1><p><button type="button" class="button" id="pass">Pass</button> <button type="button" class="button primary" id="save">Record readings</button></p>
    <p><span class="outcome-mark outcome-mark-new" id="mark">✓</span></p></main><script>${shellScript}</script></body></html>`;
  try {
    await page.route("http://feedback.test/**", (route) => route.fulfill({ status: 200, contentType: "text/html", body: markup }));
    await page.goto("http://feedback.test/", { waitUntil: "load" });
    const pass = page.getByRole("button", { name: "Pass" });
    const box = await pass.boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    const pressed = await pass.evaluate((node) => ({ transform: getComputedStyle(node).transform, shadow: getComputedStyle(node).boxShadow }));
    await page.mouse.up();
    assert.ok(pressed.transform !== "none" && pressed.shadow !== "none", `${viewport.name}: a pressed button shows no pressed state ${JSON.stringify(pressed)}`);
    assert.equal(await page.evaluate(() => document.activeElement.id), "pass");
    await page.evaluate(() => window.cyaShell.toast("Saved: Pass for Oxygen."));
    const toast = page.locator(".toast");
    assert.equal(await toast.getAttribute("role"), "status");
    assert.equal(await toast.getAttribute("aria-hidden"), null);
    assert.equal(await toast.textContent(), "✓Saved: Pass for Oxygen.", `${viewport.name}: toast text`);
    await page.waitForFunction(() => getComputedStyle(document.querySelector(".toast")).opacity === "1");
    assert.equal(await page.evaluate(() => document.activeElement.id), "pass", `${viewport.name}: the toast took focus`);
    const place = await page.evaluate(() => {
      const t = document.querySelector(".toast").getBoundingClientRect(), nav = document.querySelector(".app-nav");
      const hit = document.elementFromPoint(t.left + t.width / 2, t.top + t.height / 2);
      return { left: t.left, right: t.right, bottom: t.bottom, navTop: nav.getBoundingClientRect().top, navFixed: getComputedStyle(nav).position === "fixed", height: innerHeight, width: innerWidth, blocks: !!hit && hit.closest(".toast") !== null };
    });
    assert.ok(place.left >= 0 && place.right <= place.width && place.bottom <= place.height, `${viewport.name}: the toast is off screen ${JSON.stringify(place)}`);
    if (place.navFixed) { assert.ok(place.bottom <= place.navTop, `${viewport.name}: the toast covers the tab bar ${JSON.stringify(place)}`); }
    assert.equal(place.blocks, false, `${viewport.name}: the toast blocks taps on what is beneath it`);
    await page.evaluate(() => window.cyaShell.toast("Readings saved.", { announce: false }));
    assert.equal(await toast.getAttribute("aria-hidden"), "true", `${viewport.name}: a visual-only toast is still announced`);
    await page.waitForFunction(() => !document.querySelector(".toast").classList.contains("is-shown"), null, { timeout: 4500 });
    await page.waitForFunction(() => document.querySelector(".toast").textContent === "", null, { timeout: 1500 });
    assert.equal(await page.locator("#mark").evaluate((node) => getComputedStyle(node).animationName), "cya-saved-pop");
    await page.emulateMedia({ reducedMotion: "reduce" });
    assert.equal(await page.locator("#mark").evaluate((node) => getComputedStyle(node).animationName), "none", `${viewport.name}: the check mark animates under reduced motion`);
    console.log(`${viewport.name}: pressed state, Saved toast and reduced-motion check mark passed`);
  } finally {
    await page.close();
  }
}

async function testChecklistAids(viewport) {
  // Issue #194: the checklist bar stays above the phone tab bar, shows progress, moves through
  // unchecked items across areas, and jumps to a section.
  const page = await browser.newPage({ viewport });
  const base = sampleRows[0];
  const make = (id, compartment, state) => ({ ...base, item_id: id, item_label: `Item ${id}`, compartment_id: compartment, compartment_label: compartment === "comp-cab" ? "Cab" : "Rear", obligation_state: state, obligation_outcome: state === "SATISFIED" ? "PASSED" : null, item_attention: "CLEAR", needs_action: state !== "SATISFIED" });
  const cabStates = ["SATISFIED", "SATISFIED", "SATISFIED", "SATISFIED", "DUE", "UPCOMING", "UPCOMING", "UPCOMING", "UPCOMING", "OVERDUE", "SATISFIED", "SATISFIED", "SATISFIED", "SATISFIED", "SATISFIED", "SATISFIED"];
  const rows = cabStates.map((state, index) => make(`c${index + 1}`, "comp-cab", state)).concat([make("r1", "comp-rear", "DUE"), make("r2", "comp-rear", "SATISFIED")]);
  const groups = [
    { key: "compartment:comp-cab", label: "Cab", total: 16, checked: 10, due: 1, due_soon: 0, overdue: 1, not_due: 4, no_obligation: 0, exceptions: 0, open_deficiencies: 0 },
    { key: "compartment:comp-rear", label: "Rear", total: 2, checked: 1, due: 1, due_soon: 0, overdue: 0, not_due: 0, no_obligation: 0, exceptions: 0, open_deficiencies: 0 },
  ];
  const bar = `<div id="checklist-bar" class="checklist-bar" role="group" aria-label="Checklist progress and navigation" hidden><div class="checklist-bar-progress"><span id="checklist-progress-text" class="checklist-progress-text"></span><progress id="checklist-progress" max="1" value="0" aria-label="Checklist progress"></progress></div><div class="checklist-bar-actions"><button type="button" id="next-unchecked" class="button primary">Next unchecked item</button><button type="button" id="jump-sections" class="button" aria-expanded="false" aria-controls="jump-panel">Sections</button></div><div id="jump-panel" class="jump-panel" hidden><h3 id="jump-heading" class="jump-heading">Jump to a section</h3><ul id="jump-list" class="jump-list" aria-labelledby="jump-heading"></ul></div><p id="checklist-bar-status" class="visually-hidden" role="status"></p></div>`;
  const markup = inspectionMarkup("emt-aids", `session-aids-${viewport.name}`)
    .replace('<html lang="en">', '<html lang="en" class="js">')
    .replace("<body>", '<body class="has-app-nav"><nav class="app-nav" aria-label="Main"><ul class="app-nav-tabs"><li><a class="app-nav-link" href="#home">Home</a></li><li><a class="app-nav-link" href="#inspect" aria-current="page">Inspect</a></li><li><a class="app-nav-link" href="#problems">Problems</a></li><li><a class="app-nav-link" href="#vehicles">Vehicles</a></li></ul></nav>')
    .replace('<ol id="items" class="items"></ol>', `<ol id="items" class="items"></ol>${bar}`);
  await page.route("http://aids.test/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith("/api/")) {
      const body = url.pathname === "/api/checklist"
        ? { status: "OK", items: rows, group_progress: groups, vehicle_status: { status: "IN_SERVICE", restrictions: "" } }
        : { status: "COMMITTED", result: {} };
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
      return;
    }
    await route.fulfill({ status: 200, contentType: "text/html", body: markup });
  });
  try {
    await page.goto("http://aids.test/", { waitUntil: "load" });
    const checklistBar = page.getByRole("group", { name: "Checklist progress and navigation" });
    await checklistBar.waitFor();
    assert.equal(await page.locator("#checklist-progress-text").textContent(), "11 of 14 checked · 3 still due");
    const next = page.getByRole("button", { name: "Next unchecked item" });
    const focusedKey = () => page.evaluate(() => document.activeElement.dataset.itemKey || null);
    const expectFocus = async (itemKey, message) => {
      await page.waitForFunction((k) => document.activeElement && document.activeElement.dataset.itemKey === k, itemKey, { timeout: 3000 }).catch(() => {});
      assert.equal(await focusedKey(), itemKey, `${viewport.name}: ${message}`);
      const place = await page.evaluate(() => {
        const item = document.activeElement.getBoundingClientRect(), stuck = document.getElementById("checklist-bar").getBoundingClientRect();
        return { top: item.top, barTop: stuck.top };
      });
      assert.ok(place.top >= 0 && place.top < place.barTop, `${viewport.name}: the item Next moved to is hidden behind the bar ${JSON.stringify(place)}`);
    };
    await next.click();
    await expectFocus("c5|", "Next did not open the first area at its first due item");
    assert.equal(await page.locator("#area-heading").textContent(), "Cab");
    const layout = await page.evaluate(() => {
      const stuck = document.getElementById("checklist-bar").getBoundingClientRect();
      const nav = document.querySelector(".app-nav").getBoundingClientRect();
      return { barBottom: stuck.bottom, barTop: stuck.top, navTop: nav.top, navFixed: getComputedStyle(document.querySelector(".app-nav")).position === "fixed", height: innerHeight, scrollWidth: document.documentElement.scrollWidth };
    });
    assert.ok(layout.barBottom <= layout.height + 0.5, `${viewport.name}: the checklist bar is below the screen ${JSON.stringify(layout)}`);
    if (layout.navFixed) { assert.ok(layout.barBottom <= layout.navTop + 0.5, `${viewport.name}: the checklist bar is under the tab bar ${JSON.stringify(layout)}`); }
    assert.ok(layout.scrollWidth <= viewport.width, `${viewport.name}: the checklist bar causes horizontal overflow ${JSON.stringify(layout)}`);
    for (const target of [next, page.getByRole("button", { name: "Sections" })]) {
      assert.ok((await target.boundingBox()).height >= 48, `${viewport.name}: a checklist bar button is shorter than 48 CSS pixels`);
    }
    await next.click();
    await expectFocus("c10|", "Next did not continue to the overdue item");
    await next.click();
    await expectFocus("r1|", "Next did not cross into the next area");
    assert.equal(await page.locator("#area-heading").textContent(), "Rear");
    await next.click();
    await expectFocus("c5|", "Next did not wrap to the first unchecked item");
    const sections = page.getByRole("button", { name: "Sections" });
    await sections.click();
    assert.equal(await sections.getAttribute("aria-expanded"), "true");
    const rear = page.locator("#jump-panel").getByRole("button", { name: /^Rear/ });
    assert.match(await rear.innerText(), /1 still due/);
    assert.ok((await rear.boundingBox()).height >= 48, `${viewport.name}: a section button is shorter than 48 CSS pixels`);
    await rear.click();
    assert.equal(await page.locator("#jump-panel").isVisible(), false, `${viewport.name}: choosing a section left the panel open`);
    assert.equal(await page.evaluate(() => document.activeElement.id), "area-heading", `${viewport.name}: the section jump did not move focus to its heading`);
    assert.equal(await page.locator("#area-heading").textContent(), "Rear");
    await sections.click();
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#jump-panel").isVisible(), false, `${viewport.name}: Escape did not close the section list`);
    assert.equal(await page.evaluate(() => document.activeElement.id), "jump-sections", `${viewport.name}: Escape did not return focus to Sections`);
    console.log(`${viewport.name}: checklist bar progress, Next unchecked item and section jump passed`);
  } finally {
    await page.close();
  }
}
async function testDateTimeFocus(viewport) {
  // Issue #194 evidence: Tab inside a date-time field's parts must keep a visible focus ring.
  const page = await browser.newPage({ viewport });
  const markup = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Date</title><style>${css}</style></head>
    <body><main id="main"><h1>Readiness</h1><label for="as-of">Show the picture as of</label> <input id="as-of" name="as_of" type="datetime-local"> <button type="button" class="button">Show</button></main></body></html>`;
  try {
    await page.route("http://date.test/**", (route) => route.fulfill({ status: 200, contentType: "text/html", body: markup }));
    await page.goto("http://date.test/", { waitUntil: "load" });
    await page.locator("#as-of").focus();
    for (let part = 0; part < 3; part++) {
      const ring = await page.locator("#as-of").evaluate((el) => document.activeElement === el ? getComputedStyle(el).outlineStyle + " " + getComputedStyle(el).outlineWidth : "left");
      if (ring === "left") break;
      assert.equal(ring, "solid 3px", `${viewport.name}: no focus ring inside the date-time field (part ${part + 1})`);
      await page.keyboard.press("Tab");
    }
    console.log(`${viewport.name}: date-time field keeps its focus ring passed`);
  } finally {
    await page.close();
  }
}

async function testInspectionNavigation(viewport) {
  const page = await browser.newPage({ viewport });
  const configForPath = (pathname) => pathname === "/another-user"
    ? { user: "emt-b", session: "session-a" }
    : pathname === "/new-session"
      ? { user: "emt-a", session: "session-b" }
      : { user: "emt-a", session: "session-a" };
  await page.route("http://inspection.test/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith("/api/")) {
      const body = url.pathname === "/api/checklist"
        ? {
          status: "OK",
          items: url.searchParams.get("needs_attention_only") === "true"
            ? sampleRows.filter((row) => !row.check_suspended && (row.item_attention === "ATTENTION" || row.needs_action))
            : sampleRows,
          group_progress: [
            { key: "compartment:comp-cab", label: "Cab", total: 4, checked: 1, due: 1, due_soon: 0, overdue: 1, not_due: 0, no_obligation: 0, exceptions: 1, open_deficiencies: 1 },
          ],
          vehicle_status: { status: "IN_SERVICE", restrictions: "" },
        }
        : { status: "COMMITTED", result: {} };
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
      return;
    }
    const identity = configForPath(url.pathname);
    await route.fulfill({ status: 200, contentType: "text/html", body: inspectionMarkup(identity.user, identity.session) });
  });
  try {
    await page.goto("http://inspection.test/", { waitUntil: "load" });
    const overview = page.getByRole("heading", { name: "Choose an area or compartment" });
    await overview.waitFor();
    assert.equal(await page.locator("#area-overview").isVisible(), true, `${viewport.name}: new session did not open the compartment overview`);
    assert.equal(await page.locator("#items").isVisible(), false, `${viewport.name}: overview also rendered the flat item list`);
    const cab = page.getByRole("button", { name: /^Cab / });
    assert.match(await cab.innerText(), /1 of 4 checked · 1 due · 1 overdue · 1 open problem$/m);
    const cabDetails = page.locator(".area-card-item", { has: cab }).locator("details");
    assert.equal(await cabDetails.locator(".area-card-breakdown").isVisible(), false, `${viewport.name}: Details starts closed`);
    await cabDetails.locator("summary").click();
    assert.match(await cabDetails.locator(".area-card-breakdown").innerText(), /0 due soon · 0 not due · 0 without an obligation · 1 exception$/);
    const metrics = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth, cardTarget: document.querySelector(".area-card").getBoundingClientRect().height }));
    assert.ok(metrics.document <= viewport.width, `${viewport.name}: inspection overview overflows horizontally ${JSON.stringify(metrics)}`);
    assert.ok(metrics.body <= viewport.width, `${viewport.name}: inspection body overflows horizontally ${JSON.stringify(metrics)}`);
    assert.ok(metrics.cardTarget >= 48, `${viewport.name}: compartment target is shorter than 48px`);
    await cab.click();
    await page.getByRole("heading", { name: "Cab", exact: true }).waitFor();
    // Issue #194 split view: at 64rem (1024px) and wider the area list stays beside the open area.
    const split = viewport.width >= 1024;
    assert.equal(await page.locator("#area-overview").isVisible(), split, `${viewport.name}: split view ${split ? "missing" : "shown on a narrow screen"}`);
    if (split) {
      const columns = await page.evaluate(() => {
        const list = document.getElementById("area-overview").getBoundingClientRect();
        const items = document.getElementById("items").getBoundingClientRect();
        return { listRight: list.right, itemsLeft: items.left, listTop: list.top, itemsBottom: items.bottom };
      });
      assert.ok(columns.listRight <= columns.itemsLeft, `${viewport.name}: the area list is not beside the items ${JSON.stringify(columns)}`);
      assert.equal(await page.getByRole("button", { name: /^Cab / }).getAttribute("aria-current"), "true", `${viewport.name}: the open area is not marked`);
      assert.match(await page.getByRole("button", { name: /^Cab / }).innerText(), /2 still due/);
    }
    assert.equal(await page.getByRole("heading", { name: "Kit instance: Airway Kit (1)" }).count(), 1);
    assert.equal(await page.getByRole("heading", { name: "Kit instance: Airway Kit (2)" }).count(), 1);
    const compartmentItems = await page.locator("#items > li.item").evaluateAll((items) => items.map((item) => item.dataset.itemKey));
    assert.equal(compartmentItems.length, 4, `${viewport.name}: compartment drill-down did not contain its four checklist rows`);
    assert.ok(compartmentItems.includes("item-mask|kit-one"), `${viewport.name}: first physical kit instance was not nested under the compartment`);
    assert.ok(compartmentItems.includes("item-mask|kit-two"), `${viewport.name}: second physical kit instance was not nested under the compartment`);
    await page.evaluate(() => {
      const item = document.querySelector('#items [data-item-key]');
      item.scrollIntoView({ block: "start" });
      document.dispatchEvent(new Event("scroll"));
    });
    await page.waitForTimeout(200);
    const savedPosition = await page.evaluate(() => JSON.parse(localStorage.getItem("cya.inspection.navigation.v1:emt-a:session-a")));
    assert.ok(savedPosition && savedPosition.anchorKey, `${viewport.name}: item position was not saved for resume`);
    await page.reload({ waitUntil: "load" });
    await page.getByRole("heading", { name: "Cab", exact: true }).waitFor();
    assert.equal(await page.getByRole("heading", { name: "Kit instance: Airway Kit (2)" }).count(), 1, `${viewport.name}: the current EMT's compartment did not restore after reload`);
    const radio = page.getByRole("heading", { name: "Radio", exact: true });
    const filteredChecklist = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === "/api/checklist" && url.searchParams.get("needs_attention_only") === "true";
    });
    await page.getByRole("checkbox", { name: "Needs Attention Only" }).check();
    await filteredChecklist;
    await radio.waitFor({ state: "detached" });
    assert.equal(await radio.count(), 0, `${viewport.name}: suspended red row escaped the attention filter`);
    await page.getByRole("button", { name: "All areas and compartments" }).click();
    await page.locator("#area-overview").waitFor({ state: "visible" });
    await page.getByRole("checkbox", { name: "Needs Attention Only" }).uncheck();
    await page.getByRole("button", { name: "All items" }).click();
    assert.equal(await page.getByText("Radio", { exact: true }).count(), 1, `${viewport.name}: flat view did not show unfiltered groups`);
    await page.goto("http://inspection.test/another-user", { waitUntil: "load" });
    await page.locator("#area-overview").waitFor({ state: "visible" });
    assert.equal(await page.locator("#area-detail").isVisible(), false, `${viewport.name}: another EMT inherited navigation state`);
    await page.goto("http://inspection.test/new-session", { waitUntil: "load" });
    await page.locator("#area-overview").waitFor({ state: "visible" });
    assert.equal(await page.locator("#area-detail").isVisible(), false, `${viewport.name}: a new inspection inherited a prior session's location`);
    console.log(`${viewport.name}: compartment overview, kit grouping and per-user resume passed`);
  } finally {
    await page.close();
  }
}
async function testInspectionOutcomeSemantics(viewport) {
  const page = await browser.newPage({ viewport });
  await page.route("http://inspection.test/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith("/api/")) {
      const body = url.pathname === "/api/checklist"
        ? { status: "OK", items: outcomeRows, group_progress: [
          { key: "compartment:comp-cab", label: "Cab", total: 4, checked: 1, due: 3, due_soon: 0, overdue: 0, not_due: 0, no_obligation: 0, exceptions: 0, open_deficiencies: 3 },
        ], vehicle_status: { status: "IN_SERVICE", restrictions: "" } }
        : { status: "OK", suggestions: [] };
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
      return;
    }
    await route.fulfill({ status: 200, contentType: "text/html", body: inspectionMarkup("emt-a", "session-outcomes") });
  });
  try {
    await page.goto("http://inspection.test/outcomes", { waitUntil: "load" });
    await page.getByRole("button", { name: /^Cab / }).click();
    const failed = page.locator('#items > li[data-item-key="item-fail|"]');
    assert.match(await failed.innerText(), /Due/);
    assert.match(await failed.innerText(), /Latest recorded check: Fail by Synthetic EMT/);
    assert.match(await failed.innerText(), /Problem status: Open\. Severity: High/);
    assert.equal(await failed.locator(".pill-needs-attention").count(), 0, `${viewport.name}: an open deficiency is not repeated as a second attention badge`);
    // Issue 194: chip classes map to the approved status colors (amber for needs attention, red for failed/overdue).
    const chipColors = await page.evaluate(() => {
      const make = (cls, style) => { const s = document.createElement("span"); s.className = cls; if (style) s.style.cssText = style; document.body.appendChild(s); const c = getComputedStyle(s); const r = [c.backgroundColor, c.color]; s.remove(); return r; };
      return {
        needs: make("pill pill-needs-attention"), wait: make("", "background: var(--wait-bg); color: var(--wait-fg)"),
        failed: make("pill pill-attention"), bad: make("", "background: var(--bad-bg); color: var(--bad-fg)"),
      };
    });
    assert.deepEqual(chipColors.needs, chipColors.wait, `${viewport.name}: Needs attention uses the amber status pair`);
    assert.deepEqual(chipColors.failed, chipColors.bad, `${viewport.name}: failed and overdue counts keep the red status pair`);
    assert.notDeepEqual(chipColors.wait, chipColors.bad, `${viewport.name}: amber and red status pairs differ`);
    const passedWithOpenDeficiency = page.locator('#items > li[data-item-key="item-pass-open|"]');
    assert.match(await passedWithOpenDeficiency.innerText(), /Passed/);
    assert.match(await passedWithOpenDeficiency.innerText(), /Latest recorded check: Pass by Synthetic EMT/);
    assert.match(await passedWithOpenDeficiency.innerText(), /Problem status: Open\. Severity: Medium/);
    const priorPass = page.locator('#items > li[data-item-key="item-prior-pass|"]');
    assert.match(await priorPass.innerText(), /Due/);
    assert.match(await priorPass.innerText(), /Latest recorded check: Pass by Synthetic EMT/);
    const unable = page.locator('#items > li[data-item-key="item-unable|"]');
    assert.match(await unable.innerText(), /Latest recorded check: Unable to inspect by Synthetic EMT/);
    assert.match(await unable.innerText(), /Problem status: Pending verification/);
    const readings = page.getByRole("region", { name: "Vehicle readings" });
    assert.equal(await readings.getByRole("button", { name: "Record readings" }).count(), 1, `${viewport.name}: checklist evidence does not show the current audited readings action`);
    assert.equal(await readings.getByText("Audited history: select Record after entering complete values.").count(), 1, `${viewport.name}: checklist evidence does not explain explicit readings history`);
    await captureBrowserEvidence(page, `checklist-${viewport.name}.png`);
    const actionButtons = page.locator("#items .item-controls button");
    const count = await actionButtons.count();
    for (let index = 0; index < count; index += 1) {
      const button = actionButtons.nth(index);
      assert.ok(!(await button.getAttribute("class")).split(/\s+/).includes("primary"), `${viewport.name}: outcome action looks selected`);
      assert.match(await button.getAttribute("aria-label"), /^Record (Pass|Fail|Unable to inspect) for /, `${viewport.name}: action name does not distinguish recording from result`);
    }
    // Issue #140: only the current window's recorded result is marked, with a check mark and a name, not colour alone.
    const marks = await page.locator("#items .outcome-recorded").evaluateAll((nodes) => nodes.map((node) => node.closest("li").dataset.itemKey + ":" + node.dataset.action));
    assert.deepEqual(marks, ["item-pass-open|:pass"], `${viewport.name}: wrong recorded-result marks ${JSON.stringify(marks)}`);
    const recordedPass = passedWithOpenDeficiency.locator(".outcome-recorded");
    assert.equal((await recordedPass.innerText()).trim(), "✓ Pass", `${viewport.name}: recorded result has no visible check mark`);
    assert.match(await recordedPass.getAttribute("aria-label"), /\(recorded result: Pass\)$/, `${viewport.name}: recorded result is not named for screen readers`);
    const markStyle = await recordedPass.evaluate((element) => {
      const plain = element.parentElement.querySelector("button:not(.outcome-recorded)");
      const style = getComputedStyle(element);
      return { background: style.backgroundColor, plain: getComputedStyle(plain).backgroundColor, border: style.borderTopWidth, enabled: !element.disabled, height: element.getBoundingClientRect().height };
    });
    assert.notEqual(markStyle.background, markStyle.plain, `${viewport.name}: recorded result is not visibly filled`);
    assert.equal(markStyle.border, "3px", `${viewport.name}: recorded result border is not emphasized`);
    assert.ok(markStyle.enabled && markStyle.height >= 48, `${viewport.name}: recorded result stopped being a 48px action`);
    await recordedPass.focus();
    assert.equal(await recordedPass.evaluate((element) => getComputedStyle(element).outlineWidth), "3px", `${viewport.name}: focus is not visible on a recorded result`);
    await page.emulateMedia({ colorScheme: "dark" });
    const dark = await recordedPass.evaluate((element) => [getComputedStyle(element).backgroundColor, getComputedStyle(element.parentElement.querySelector("button:not(.outcome-recorded)")).backgroundColor]);
    assert.notEqual(dark[0], dark[1], `${viewport.name}: recorded result is not visibly filled in dark mode`);
    assert.notEqual(dark[0], markStyle.background, `${viewport.name}: recorded result does not use the dark-theme colours`);
    await page.emulateMedia({ colorScheme: "light" });
    const failAction = failed.getByRole("button", { name: "Record Fail for Brake light" });
    await failAction.focus();
    assert.equal(await failAction.evaluate((element) => element === document.activeElement), true, `${viewport.name}: Fail action is not keyboard-focusable`);
    await page.keyboard.press("Tab");
    const unableAction = failed.getByRole("button", { name: "Record Unable to inspect for Brake light" });
    assert.equal(await unableAction.evaluate((element) => element === document.activeElement), true, `${viewport.name}: keyboard order skips the next outcome action`);
    assert.equal(await unableAction.evaluate((element) => getComputedStyle(element).outlineWidth), "3px", `${viewport.name}: keyboard action focus indicator is missing`);
    await page.keyboard.press("Shift+Tab");
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog");
    await dialog.waitFor({ state: "visible" });
    assert.equal(await page.getByRole("heading", { name: "Fail: Brake light" }).count(), 1, `${viewport.name}: keyboard activation did not announce the chosen action`);
    await page.keyboard.press("Escape");
    await dialog.waitFor({ state: "hidden" });
    assert.equal(await failAction.evaluate((element) => element === document.activeElement), true, `${viewport.name}: cancel did not restore focus to the Fail action`);
    console.log(`${viewport.name}: recorded outcome, window status, deficiency, recorded-result marks and keyboard actions passed`);
  } finally {
    await page.close();
  }
}
const artifacts = process.env.BROWSER_SMOKE_OUTPUT_DIR || path.join(os.tmpdir(), `cya-browser-smoke-${process.pid}`);
const browser = await chromium.launch({ headless: true });
const failures = [];
async function testRoleHistoryResponsive(page, viewport) {
  const region = page.getByRole("region", { name: "Role history for sam@example.test" });
  assert.equal(await region.count(), 1, `${viewport.name}: role history region is not exposed accessibly`);
  const metrics = await region.evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
    tabIndex: element.tabIndex,
    documentWidth: document.documentElement.scrollWidth,
    viewportWidth: innerWidth,
    historyText: element.innerText,
  }));
  assert.equal(metrics.tabIndex, 0, `${viewport.name}: role history region is not keyboard-focusable`);
  assert.ok(metrics.documentWidth <= metrics.viewportWidth, `${viewport.name}: role history makes the page overflow horizontally`);
  assert.match(metrics.historyText, /2026-11-15 12:34 UTC/);
  assert.match(metrics.historyText, /sam@example\.test/);
  await region.focus();
  assert.equal(await region.evaluate((element) => element === document.activeElement), true, `${viewport.name}: role history region did not receive focus`);
  assert.equal(await region.evaluate((element) => getComputedStyle(element).outlineWidth), "3px", `${viewport.name}: role history focus indicator is missing`);
  if (viewport.width <= 768) {
    assert.ok(metrics.scrollWidth > metrics.clientWidth, `${viewport.name}: long history row does not scroll horizontally`);
    await page.keyboard.press("ArrowRight");
    await page.waitForFunction(
      () => document.querySelector(".role-history-scroll").scrollLeft > 0,
      null,
      { timeout: 2000 },
    );
    assert.ok(await region.evaluate((element) => element.scrollLeft > 0), `${viewport.name}: keyboard cannot scroll the history region`);
  }
}
async function testAgencyTimezoneConversion() {
  for (const deviceZone of ["UTC", "America/Los_Angeles", "Pacific/Auckland"]) {
    const context = await browser.newContext({timezoneId: deviceZone});
    const page = await context.newPage();
    try {
      await page.setContent(`<script>${timezoneScript}</script>`);
      const actual = await page.evaluate(() => ({
        regular: window.CyaTimezones.toUtc("2026-06-01T12:00", "America/New_York"),
        fold: window.CyaTimezones.toUtc("2026-11-01T01:30", "America/New_York"),
        gap: window.CyaTimezones.toUtc("2026-03-08T02:30", "America/New_York"),
        southernFold: window.CyaTimezones.toUtc("2026-04-05T01:45", "Australia/Lord_Howe"),
        southernGap: window.CyaTimezones.toUtc("2026-10-04T02:15", "Australia/Lord_Howe"),
        savedCheck: window.CyaTimezones.formatUtc("2026-06-01T16:00:00Z", "America/New_York"),
        auditCheck: window.CyaTimezones.formatUtc("2026-06-01T16:00:00Z", "America/New_York", "seconds"),
        foldFirst: window.CyaTimezones.formatUtc("2026-11-01T05:30:00Z", "America/New_York"),
        foldSecond: window.CyaTimezones.formatUtc("2026-11-01T06:30:00Z", "America/New_York"),
        afterFold: window.CyaTimezones.formatUtc("2026-11-01T07:00:00Z", "America/New_York"),
        southernFoldDistinct: (function () {
          var a = window.CyaTimezones.formatUtc("2026-04-04T14:45:00Z", "Australia/Lord_Howe");
          var b = window.CyaTimezones.formatUtc("2026-04-04T15:15:00Z", "Australia/Lord_Howe");
          return a !== b && a.indexOf("2026-04-05 01:45 ") === 0 && b.indexOf("2026-04-05 01:45 ") === 0;
        })(),
        utc: window.CyaTimezones.formatUtc("2026-11-01T05:30:00Z", "UTC"),
        longFoldFirst: window.CyaTimezones.formatUtc("1969-09-30T01:00:00Z", "Pacific/Kwajalein"),
        longFoldSecond: window.CyaTimezones.formatUtc("1969-10-01T00:00:00Z", "Pacific/Kwajalein"),
        afterLongFold: window.CyaTimezones.formatUtc("1969-10-02T00:00:00Z", "Pacific/Kwajalein"),
      }));
      assert.deepEqual(actual, {
        regular: "2026-06-01T16:00:00.000Z",
        fold: "2026-11-01T05:30:00.000Z",
        gap: "2026-03-08T07:00:00.000Z",
        southernFold: "2026-04-04T14:45:00.000Z",
        southernGap: "2026-10-03T15:30:00.000Z",
        savedCheck: "2026-06-01 12:00",
        auditCheck: "2026-06-01 12:00:00",
        foldFirst: "2026-11-01 01:30 EDT",
        foldSecond: "2026-11-01 01:30 EST",
        afterFold: "2026-11-01 02:00",
        southernFoldDistinct: true,
        utc: "2026-11-01 05:30",
        longFoldFirst: "1969-09-30 12:00 +11",
        longFoldSecond: "1969-09-30 12:00 -12",
        afterLongFold: "1969-10-01 12:00",
      }, `device timezone ${deviceZone}: agency-zone conversion changed`);
      const invalid = await page.evaluate(() => {
        const errors = [];
        for (const [value, zone] of [["not-a-date", "America/New_York"], ["2026-06-01T12:00", "Invalid/Zone"]]) {
          try { window.CyaTimezones.toUtc(value, zone); }
          catch (error) { errors.push(error.message); }
        }
        return errors;
      });
      assert.match(invalid[0], /valid agency-local date and time/);
      assert.match(invalid[1], /agency time zone is unavailable/);
      console.log(`device ${deviceZone}: station-local normal, DST fold and DST gap conversion passed`);
    } finally {
      await context.close();
    }
  }
}
async function waitForNavLayout(page, wide) {
  await page.waitForFunction((isWide) => {
    const more = document.getElementById("app-nav-more");
    const panel = document.getElementById("app-nav-more-panel");
    return window.matchMedia("(min-width: 64rem)").matches === isWide
      && more.hidden === isWide
      && panel.hidden === (!isWide && more.getAttribute("aria-expanded") !== "true");
  }, wide);
}

async function openMore(page) {
  const more = page.locator("#app-nav-more");
  if (await more.isVisible() && (await more.getAttribute("aria-expanded")) !== "true") { await more.click(); }
}

async function testSortableTables(viewport) {
  // Issue #194: admin tables sort by column (aria-sort) and filter, as a script enhancement.
  const page = await browser.newPage({ viewport });
  const markup = `<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1"><title>Vehicles</title>
    <style>${css}</style></head><body><main id="main"><h1>Vehicles</h1>
    <div class="table-scroll" role="region" aria-label="Vehicles" tabindex="0">
    <table data-sortable data-filter-label="Filter this list">
      <thead><tr><th scope="col">Vehicle</th><th scope="col">Number</th><th scope="col" data-nosort>Action</th></tr></thead>
      <tbody>
        <tr><td><a href="#m10">Medic 10</a></td><td>10</td><td><button type="button">Duplicate</button></td></tr>
        <tr><td><a href="#e1">Engine 1</a></td><td>1</td><td><button type="button">Duplicate</button></td></tr>
        <tr><td><a href="#m2">Medic 2</a></td><td>2</td><td><button type="button">Duplicate</button></td></tr>
      </tbody>
    </table></div></main><script>${shellScript}</script></body></html>`;
  try {
    await page.route("http://tables.test/**", (route) => route.fulfill({ status: 200, contentType: "text/html", body: markup }));
    await page.goto("http://tables.test/", { waitUntil: "load" });
    const names = () => page.locator("tbody tr:not([hidden]) td:first-child").allInnerTexts();
    const vehicle = page.getByRole("button", { name: "Vehicle", exact: true });
    const header = page.locator("th").first();
    assert.equal(await page.locator("th[aria-sort]").count(), 0, `${viewport.name}: no header carries aria-sort before a sort`);
    assert.equal(await page.locator('th[data-nosort] button').count(), 0, `${viewport.name}: a no-sort column got a sort button`);
    const box = await vehicle.boundingBox();
    assert.ok(box.height >= 48, `${viewport.name}: sort button is shorter than 48 CSS pixels`);
    await vehicle.click();
    assert.equal(await header.getAttribute("aria-sort"), "ascending");
    assert.deepEqual(await names(), ["Engine 1", "Medic 2", "Medic 10"], `${viewport.name}: ascending sort is not in natural order`);
    await vehicle.press("Enter");
    assert.equal(await header.getAttribute("aria-sort"), "descending", `${viewport.name}: keyboard did not reverse the sort`);
    assert.deepEqual(await names(), ["Medic 10", "Medic 2", "Engine 1"]);
    await page.getByRole("button", { name: "Number", exact: true }).click();
    assert.equal(await header.getAttribute("aria-sort"), null, `${viewport.name}: the previous column kept its sort state`);
    assert.equal(await page.locator("th[aria-sort]").count(), 1, `${viewport.name}: more than one header carries aria-sort`);
    assert.equal(await page.locator("th").nth(1).getAttribute("aria-sort"), "ascending");
    assert.deepEqual(await names(), ["Engine 1", "Medic 2", "Medic 10"], `${viewport.name}: numbers are not sorted numerically`);
    const filter = page.getByLabel("Filter this list");
    const filterBox = await filter.boundingBox();
    assert.ok(filterBox.height >= 48, `${viewport.name}: filter box is shorter than 48 CSS pixels`);
    await filter.fill("medic");
    assert.deepEqual(await names(), ["Medic 2", "Medic 10"], `${viewport.name}: filter did not hide non-matching rows`);
    assert.equal(await page.locator(".table-status").textContent(), "Showing 2 of 3.");
    await filter.fill("");
    assert.equal((await names()).length, 3, `${viewport.name}: clearing the filter did not restore every row`);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(overflow <= 1, `${viewport.name}: sortable table page overflows horizontally`);
    console.log(`${viewport.name}: sortable and filterable table passed`);
  } finally {
    await page.close();
  }
}

async function testReadinessSummary(viewport) {
  const page = await browser.newPage({ viewport });
  const readiness = `<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1"><title>Fleet readiness</title>
    <style>${css}</style></head><body><main id="main"><h1>Fleet readiness</h1>
    <div class="readiness-filter"><input class="readiness-filter-input" id="readiness-attention-only" type="checkbox" aria-describedby="readiness-filter-help"><div class="readiness-filter-copy"><label for="readiness-attention-only">Show vehicles needing attention</label><p id="readiness-filter-help" class="readiness-filter-help">Shows vehicles with something to act on: not In Service; a check that failed, is overdue or was missed; an open Critical / High problem; a suspended check; or more problems than this page can list.</p></div>
    <div id="readiness-panel"><p>Each state is counted on its own; nothing here is a single &quot;compliant&quot; flag.</p>
      <section class="panel readiness-vehicle" data-vehicle-id="vehicle-a" data-attention="true" aria-labelledby="vehicle-a-heading">
        <div class="readiness-overview"><div class="readiness-identity"><h2 id="vehicle-a-heading">Medic 6</h2><span class="pill status-pill" data-status="IN_SERVICE">In Service</span></div>
        <div class="readiness-measure"><h3>Inspection progress</h3><p>8 of 10 required checks recorded</p></div>
        <ul class="readiness-signals" aria-label="Independent attention counts"><li class="pill pill-attention">Failed: 1</li><li class="pill pill-attention">Overdue: 2</li><li class="pill">Missed: 0</li><li class="pill pill-attention">Critical / High shown: 1</li><li class="pill">Suspensions: 0</li></ul>
        <nav class="readiness-actions" aria-label="Medic 6 workflows"><a href="/inspection/vehicles/vehicle-a/">Open vehicle inspection</a><a href="/status/vehicles/vehicle-a/">Current status workflow</a><a href="/problems/def-a/">Open oldest active problem</a></nav></div>
        <details class="readiness-details" data-vehicle-id="vehicle-a"><summary>Required checks, problems and suspensions for Medic 6</summary><div class="readiness-detail-content"><h3>Required checks by state</h3><ul><li>Failed: 1</li><li>Overdue: 2</li></ul></div></details>
      </section>
      <section class="panel readiness-vehicle" data-vehicle-id="vehicle-b" data-attention="false"><div class="readiness-overview"><div class="readiness-identity"><h2>Medic 7</h2><span class="pill status-pill" data-status="IN_SERVICE">In Service</span></div><div class="readiness-measure"><h3>Inspection progress</h3><p>10 of 10 required checks recorded</p></div><ul class="readiness-signals" aria-label="Independent attention counts"><li class="pill">Failed: 0</li><li class="pill">Overdue: 0</li><li class="pill">Missed: 0</li><li class="pill">Critical / High shown: 0</li><li class="pill">Suspensions: 0</li></ul></div></section>
      <section class="panel readiness-vehicle" data-vehicle-id="vehicle-c" data-attention="true"><div class="readiness-overview"><div class="readiness-identity"><h2>Medic 8</h2><span class="pill status-pill" data-status="OUT_OF_SERVICE">Out of Service</span></div><div class="readiness-measure"><h3>Inspection progress</h3><p>10 of 10 required checks recorded</p></div><ul class="readiness-signals" aria-label="Independent attention counts"><li class="pill">Failed: 0</li><li class="pill">Overdue: 0</li><li class="pill">Missed: 0</li><li class="pill">Critical / High shown: 0</li><li class="pill">Suspensions: 0</li></ul></div></section>
    </div></div></main></body></html>`;
  try {
    await page.route("http://readiness.test/**", (route) => route.fulfill({ status: 200, contentType: "text/html", body: readiness }));
    await page.goto("http://readiness.test/", { waitUntil: "load" });
    assert.equal(await page.getByRole("heading", { name: "Fleet readiness" }).count(), 1);
    assert.match(await page.locator('.readiness-vehicle[data-vehicle-id="vehicle-a"]').innerText(), /In Service[\s\S]*Failed: 1[\s\S]*Overdue: 2[\s\S]*Missed: 0[\s\S]*Critical \/ High shown: 1[\s\S]*Suspensions: 0/);
    assert.equal(await page.getByRole("link", { name: "Open vehicle inspection" }).getAttribute("href"), "/inspection/vehicles/vehicle-a/");
    assert.equal(await page.getByRole("link", { name: "Current status workflow" }).getAttribute("href"), "/status/vehicles/vehicle-a/");
    assert.equal(await page.getByRole("link", { name: "Open oldest active problem" }).getAttribute("href"), "/problems/def-a/");
    const filter = page.getByRole("checkbox", { name: "Show vehicles needing attention" });
    await filter.focus();
    assert.equal(await filter.evaluate((element) => element === document.activeElement), true, `${viewport.name}: attention filter is not keyboard-focusable`);
    await page.keyboard.press("Space");
    assert.equal(await page.locator('.readiness-vehicle[data-vehicle-id="vehicle-a"]').isVisible(), true, `${viewport.name}: attention vehicle was hidden by the filter`);
    assert.equal(await page.locator('.readiness-vehicle[data-vehicle-id="vehicle-c"]').isVisible(), true, `${viewport.name}: non-In-Service status was not filtered as an independent signal`);
    assert.equal(await page.locator('.readiness-vehicle[data-vehicle-id="vehicle-b"]').isVisible(), false, `${viewport.name}: vehicle without attention signals remained in the filtered view`);
    const summary = page.getByText("Required checks, problems and suspensions for Medic 6");
    const targetHeight = await summary.evaluate((element) => element.getBoundingClientRect().height);
    assert.ok(targetHeight >= 48, `${viewport.name}: details target is shorter than 48 CSS pixels`);
    await summary.focus();
    await page.keyboard.press("Enter");
    assert.equal(await summary.evaluate((element) => element.parentElement.open), true, `${viewport.name}: details disclosure did not open from the keyboard`);
    const metrics = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth }));
    assert.ok(metrics.document <= viewport.width, `${viewport.name}: readiness page overflows horizontally ${JSON.stringify(metrics)}`);
    assert.ok(metrics.body <= viewport.width, `${viewport.name}: readiness body overflows horizontally ${JSON.stringify(metrics)}`);
    await page.setViewportSize({ width: Math.max(320, Math.floor(viewport.width / 2)), height: viewport.height });
    const zoom = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth }));
    assert.ok(zoom.document <= zoom.viewport, `${viewport.name} at 200%: readiness page overflows ${JSON.stringify(zoom)}`);
    assert.ok(zoom.body <= zoom.viewport, `${viewport.name} at 200%: readiness body overflows ${JSON.stringify(zoom)}`);
    console.log(`${viewport.name}: readiness summaries, filter, links, disclosure and responsive layout passed`);
  } finally {
    await page.close();
  }
}

async function testDeficiencyFilters(viewport) {
  const page = await browser.newPage({ viewport });
  const listing = `<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1"><title>Problems</title>
    <style>${css}</style></head><body><main id="main"><h1>Problems</h1>
    <form class="panel deficiency-filter-form" method="get" action="/problems/">
      <div class="deficiency-filter-grid">
        <label class="deficiency-filter-wide" for="deficiency-search">Problem text<input id="deficiency-search" type="search" name="q" value="brake" maxlength="100"></label>
        <label for="deficiency-vehicle">Vehicle name or number<input id="deficiency-vehicle" type="search" name="vehicle" value="Medic 6" maxlength="100"></label>
        <label for="deficiency-severity">Severity<select id="deficiency-severity" name="severity"><option value="">Any severity</option><option value="URGENT" selected>Urgent (URGENT)</option><option value="RETIRED">Legacy (RETIRED, retired)</option></select></label>
        <label for="deficiency-assignee">Assigned to<input id="deficiency-assignee" type="search" name="assignee" value="Sam Example" maxlength="100"></label>
        <label for="deficiency-status">Lifecycle status<select id="deficiency-status" name="status"><option value="active">Outstanding only</option><option value="all">All statuses, including closed and Reported in Error</option><option value="OPEN">Open</option><option value="REPORTED_IN_ERROR">Reported in Error</option></select></label>
        <label class="check" for="deficiency-overdue"><input id="deficiency-overdue" type="checkbox" name="overdue" value="1"> Overdue only</label>
      </div><div class="deficiency-filter-actions"><button class="button primary" type="submit">Apply filters</button><a class="button" href="/problems/">Clear filters</a></div>
    </form><p class="deficiency-applied-filters"><strong>Applied filters:</strong> Text: brake · Vehicle: Medic 6</p>
    <p class="deficiency-results-count">42 matching problems; showing 25 on this page.</p>
    <ul class="plain"><li class="panel"><a href="/problems/one/">Synthetic brake warning</a> — Medic 6 (North County Regional Ambulance)</li></ul>
    <nav class="deficiency-pagination" aria-label="Problem result pages"><a class="button" href="/problems/?q=brake&amp;results_page=1">Newer results</a><a class="button" href="/problems/?q=brake&amp;results_page=2">Older results</a></nav>
    </main></body></html>`;
  try {
    await page.route("http://deficiencies.test/**", (route) => route.fulfill({ status: 200, contentType: "text/html", body: listing }));
    await page.goto("http://deficiencies.test/", { waitUntil: "load" });
    assert.equal(await page.getByRole("heading", { name: "Problems" }).count(), 1);
    assert.equal(await page.getByRole("searchbox", { name: "Problem text" }).inputValue(), "brake");
    assert.equal(await page.getByLabel("Lifecycle status").locator('option[value="REPORTED_IN_ERROR"]').textContent(), "Reported in Error");
    assert.equal(await page.getByLabel("Severity").inputValue(), "URGENT");
    assert.equal(await page.getByRole("link", { name: "Older results" }).getAttribute("href"), "/problems/?q=brake&results_page=2");
    assert.match(await page.getByText("42 matching problems").innerText(), /showing 25/);
    for (const label of ["Problem text", "Vehicle name or number", "Severity", "Assigned to", "Lifecycle status"]) {
      const control = page.getByLabel(label);
      const box = await control.boundingBox();
      assert.ok(box && box.height >= 48, `${viewport.name}: ${label} target is shorter than 48 CSS pixels`);
    }
    // Issue #238: Problem text spans the row; two filters per row from 40rem, one below.
    const text = await page.getByLabel("Problem text").boundingBox();
    const vehicle = await page.getByLabel("Vehicle name or number").boundingBox();
    const severity = await page.getByLabel("Severity").boundingBox();
    // Codex review of PR #252: below 40rem every filter is full width, so Problem text only
    // matches the vehicle filter there; from 40rem it spans both columns and is wider.
    if (viewport.width >= 640) {
      assert.ok(text.width > vehicle.width, `${viewport.name}: Problem text does not span both columns`);
      assert.ok(Math.abs(vehicle.y - severity.y) < 2, `${viewport.name}: vehicle and severity are not on one row`);
    } else {
      assert.ok(Math.abs(text.width - vehicle.width) < 2, `${viewport.name}: narrow filters are not all full width`);
      assert.ok(severity.y > vehicle.y + vehicle.height - 1, `${viewport.name}: narrow filters do not stack`);
    }
    const metrics = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth }));
    assert.ok(metrics.document <= viewport.width, `${viewport.name}: deficiency list overflows horizontally ${JSON.stringify(metrics)}`);
    assert.ok(metrics.body <= viewport.width, `${viewport.name}: deficiency body overflows horizontally ${JSON.stringify(metrics)}`);
    await page.setViewportSize({ width: Math.max(320, Math.floor(viewport.width / 2)), height: viewport.height });
    const zoom = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth }));
    assert.ok(zoom.document <= zoom.viewport, `${viewport.name} at 200%: deficiency list overflows ${JSON.stringify(zoom)}`);
    assert.ok(zoom.body <= zoom.viewport, `${viewport.name} at 200%: deficiency body overflows ${JSON.stringify(zoom)}`);
    console.log(`${viewport.name}: deficiency filters, result count, pagination and responsive layout passed`);
  } finally {
    await page.close();
  }
}

async function testAdministratorRoleChange(viewport) {
  const page = await browser.newPage({ viewport });
  const history = '<div class="role-history-scroll" role="region" aria-label="Role history for sam@example.test" tabindex="0"><table class="role-history-table"><thead><tr><th scope="col">When</th><th scope="col">User</th><th scope="col">Action</th><th scope="col">Role</th><th scope="col">Changed by</th></tr></thead><tbody><tr><td>2026-11-15 12:34 UTC</td><td>Sam Example (sam@example.test)</td><td>GRANT</td><td>SUPERVISOR</td><td>Alex Example (alex@example.test)</td></tr></tbody></table></div>';
  const rolesScript = readFileSync(
    path.join(root, "cya/authorization/templates/authorization/roles.js"),
    "utf8",
  );
  const roleHistoryPage = () => '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Role history</title><style>' + css + '</style></head><body><main><h1>Role history</h1><h2>Changes to roles for Sam Example (sam@example.test)</h2><p><a href="/administration/roles/">Back to organization users</a></p>' + history + '</main></body></html>';
  const roleChoice = (email, code, label, checked) =>
    '<label class="role-option' + (checked ? ' is-assigned' : '') + '"><input type="checkbox" name="roles" value="' + code + '" data-role-choice data-role-label="' + label + '" aria-label="' + label + ' role for ' + email + '"' + (checked ? ' checked' : '') + '><span>' + label + '</span><span class="role-option-pending" data-role-pending hidden></span></label>';
  const roleRow = (user, query, message) =>
    '<li class="role-user-row" data-role-user-row="' + user.id + '" data-role-search-text="' + user.name.toLowerCase() + ' ' + user.email + '"><div class="role-user-details"><h3>' + user.name + '</h3><p class="role-user-email">' + user.email + '</p><a href="/administration/roles/' + user.id + '/history/" aria-label="View role history for ' + user.email + '">View role history</a></div>'
    + '<form method="post" action="/administration/roles/" class="role-change-form admin-form" data-role-change-form' + (user.self ? ' data-role-self' : '') + '><input type="hidden" name="csrfmiddlewaretoken" value="synthetic-csrf"><input type="hidden" name="stage" value="save"><input type="hidden" name="role_selection_present" value="1"><input type="hidden" name="operation_id" value="synthetic-operation-' + user.id + '"><input type="hidden" name="target" value="' + user.id + '">'
    + user.roles.map((code) => '<input type="hidden" name="current_roles" value="' + code + '">').join("")
    + (query ? '<input type="hidden" name="q" value="' + query + '">' : "")
    + '<fieldset class="role-options"><legend>Roles</legend>' + roleChoice(user.email, "CREW", "Crew", user.roles.includes("CREW")) + roleChoice(user.email, "SUPERVISOR", "Supervisor", user.roles.includes("SUPERVISOR")) + roleChoice(user.email, "SUPPLY_OFFICER", "Supply Officer", user.roles.includes("SUPPLY_OFFICER")) + roleChoice(user.email, "ADMINISTRATOR", "Administrator", user.roles.includes("ADMINISTRATOR")) + '</fieldset>'
    + (user.self ? '<p class="banner banner-error role-self-warning" data-role-self-warning hidden><span class="indicator-icon" aria-hidden="true">&#9888;</span> You are removing your own Administrator role. You will lose access to Administration as soon as you save.</p>' : "")
    + '<div class="role-change-actions"><p class="role-change-status" role="status" aria-live="polite" data-role-change-status hidden></p><button type="submit" class="button primary" data-role-save-button>Save</button><button type="reset" class="button" data-role-reset-button>Reset</button></div></form>'
    + (message ? '<p class="save-state save-state-saved role-row-message" role="status" tabindex="-1" data-focus-on-load><span class="indicator-icon" aria-hidden="true">&#10003;</span> <span class="save-state-text">' + message + '</span></p>' : "")
    + '</li>';
  const rolePage = (stage, query = "") => {
    const users = [
      { id: "user-1", name: "Sam Example", email: "sam@example.test", roles: stage === "saved" ? ["CREW", "SUPERVISOR", "SUPPLY_OFFICER"] : ["CREW"] },
      { id: "user-2", name: "Alex Example", email: "alex@example.test", roles: ["CREW", "ADMINISTRATOR"], self: true },
    ].filter((user) => !query || (user.name.toLowerCase() + " " + user.email).includes(query.toLowerCase()));
    const result = query
      ? users.length + " matching active user" + (users.length === 1 ? "" : "s") + "."
      : "Showing the first 50 active users. Search to find someone else.";
    const message = stage === "saved" ? "Saved for Sam Example: added Supervisor, Supply Officer." : "";
    const content = '<section class="role-administration"><h2>Organization users</h2><form method="get" action="/administration/roles/" role="search" data-role-search-form><label for="role-user-search">Find an active user by name or email</label><input id="role-user-search" name="q" type="search" value="' + query + '" maxlength="100" aria-describedby="role-search-result" data-role-search><button type="submit" class="button" data-role-search-button>Search users</button></form>'
      + '<div data-role-results><p id="role-search-result" class="role-search-result" aria-live="polite" data-role-search-result>' + result + '</p>'
      + (users.length ? '<ul class="role-user-list" aria-label="Organization users and roles" data-role-user-list>' + users.map((user) => roleRow(user, query, user.id === "user-1" ? message : "")).join("") + '</ul>' : '<p>No active users match that search.</p>')
      + '</div></section>';
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>' + (query ? "Role administration (" + users.length + " matching)" : "Role administration") + '</title><style>' + css + '</style></head><body><main><h1>Role administration</h1>' + content + '<script>' + rolesScript + '</script></main></body></html>';
  };
  let saved = false;
  await page.route("http://roles.test/administration/roles/**", async (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() !== "POST") {
      if (url.pathname.endsWith("/history/")) {
        await route.fulfill({ status: 200, contentType: "text/html", body: roleHistoryPage() });
        return;
      }
      await route.fulfill({ status: 200, contentType: "text/html", body: rolePage("edit", url.searchParams.get("q") || "") });
      return;
    }
    const form = new URLSearchParams(route.request().postData() || "");
    assert.equal(form.get("stage"), "save");
    assert.equal(form.get("role_selection_present"), "1");
    assert.equal(form.get("operation_id"), "synthetic-operation-user-1");
    assert.equal(form.get("target"), "user-1");
    assert.equal(form.get("q"), "sam");
    assert.deepEqual(form.getAll("current_roles"), ["CREW"]);
    assert.deepEqual(form.getAll("roles"), ["CREW", "SUPERVISOR", "SUPPLY_OFFICER"]);
    saved = true;
    await route.fulfill({ status: 200, contentType: "text/html", body: rolePage("saved", "sam") });
  });
  try {
    await page.goto("http://roles.test/administration/roles/", { waitUntil: "load" });
    await page.keyboard.press("Tab");
    const search = page.getByRole("searchbox", { name: "Find an active user by name or email" });
    assert.equal(await search.evaluate((element) => element === document.activeElement), true, viewport.name + ": role search is not first in keyboard order");
    // Issue #236 follow-up: the list follows the search box as the person types, without a reload.
    await Promise.all([
      page.waitForResponse((response) => new URL(response.url()).searchParams.get("q") === "sam"),
      page.keyboard.type("sam"),
    ]);
    await page.waitForFunction(() => document.querySelector("#role-search-result").textContent === "1 matching active user.");
    assert.equal(await search.evaluate((element) => element === document.activeElement), true, viewport.name + ": typing a search moved focus");
    assert.equal(new URL(page.url()).searchParams.get("q"), "sam", viewport.name + ": the address does not follow the search");
    assert.equal(await page.title(), "Role administration (1 matching)", viewport.name + ": the title does not follow the search");
    assert.equal(await page.getByRole("checkbox", { name: "Crew role for alex@example.test" }).count(), 0, viewport.name + ": a non-matching row is still listed");
    await page.keyboard.press("Tab"); // Search users
    await page.keyboard.press("Tab"); // Role history link
    const historyLink = page.getByRole("link", { name: "View role history for sam@example.test" });
    assert.equal(await historyLink.evaluate((element) => element === document.activeElement), true, viewport.name + ": role-history link is not keyboard reachable");
    await page.keyboard.press("Tab"); // Crew role
    const crewRole = page.getByRole("checkbox", { name: "Crew role for sam@example.test" });
    assert.equal(await crewRole.evaluate((element) => element === document.activeElement), true, viewport.name + ": Crew role checkbox is not keyboard reachable");
    assert.equal(await crewRole.locator("xpath=..").evaluate((element) => element.classList.contains("is-assigned")), true, viewport.name + ": committed Crew role is not marked assigned");
    await page.keyboard.press("Tab"); // Supervisor role
    const supervisorRole = page.getByRole("checkbox", { name: "Supervisor role for sam@example.test" });
    assert.equal(await supervisorRole.evaluate((element) => element === document.activeElement), true, viewport.name + ": Supervisor role checkbox is not keyboard reachable");
    await page.keyboard.press("Space");
    const samRow = page.locator('[data-role-user-row="user-1"]');
    const samStatus = samRow.locator("[data-role-change-status]");
    assert.equal(await samStatus.innerText(), "Not saved yet: add Supervisor. Select Save to apply this change.", viewport.name + ": staged change is not announced");
    const supervisorOption = supervisorRole.locator("xpath=..");
    assert.equal(await supervisorOption.evaluate((element) => element.classList.contains("is-assigned")), false, viewport.name + ": pending role is styled as already assigned");
    assert.equal(await supervisorOption.evaluate((element) => element.classList.contains("is-pending")), true, viewport.name + ": pending role lacks its separate state");
    assert.equal(await supervisorOption.getByText("Adding", { exact: true }).isVisible(), true, viewport.name + ": pending grant is not labeled Adding");
    await crewRole.uncheck();
    const crewOption = crewRole.locator("xpath=..");
    assert.equal(await crewOption.evaluate((element) => element.classList.contains("is-assigned")), true, viewport.name + ": pending removal no longer shows the committed role");
    assert.equal(await crewOption.getByText("Removing", { exact: true }).isVisible(), true, viewport.name + ": pending removal is not labeled Removing");
    assert.equal(await samStatus.innerText(), "Not saved yet: add Supervisor; remove Crew. Select Save to apply these 2 changes.", viewport.name + ": several staged changes are not all described");
    await samRow.getByRole("button", { name: "Reset" }).click();
    await page.waitForFunction(() => !document.querySelector(".role-option.is-pending"));
    assert.equal(await crewRole.isChecked(), true, viewport.name + ": reset did not restore the committed role");
    assert.equal(await crewOption.evaluate((element) => element.classList.contains("is-pending")), false, viewport.name + ": reset retained the pending state");
    await supervisorRole.check();
    await page.getByRole("checkbox", { name: "Supply Officer role for sam@example.test" }).check();
    assert.equal(await samStatus.innerText(), "Not saved yet: add Supervisor and Supply Officer. Select Save to apply these 2 changes.", viewport.name + ": two additions are not described");
    const save = samRow.getByRole("button", { name: "Save" });
    assert.equal(await save.isVisible(), true, viewport.name + ": Save did not appear after a change");
    for (const label of ["Find an active user by name or email"]) {
      const box = await page.getByLabel(label, { exact: true }).boundingBox();
      assert.ok(box && box.height >= 48, viewport.name + ": " + label + " is shorter than 48 CSS pixels");
    }
    for (const label of ["Crew", "Supervisor", "Supply Officer", "Administrator"]) {
      const box = await page.getByRole("checkbox", { name: label + " role for sam@example.test" }).locator("xpath=..").boundingBox();
      assert.ok(box && box.height >= 44, viewport.name + ": " + label + " role target is shorter than 44 CSS pixels");
    }
    await page.getByRole("checkbox", { name: "Supply Officer role for sam@example.test" }).focus();
    await page.keyboard.press("Tab"); // Administrator
    await page.keyboard.press("Tab"); // Save
    assert.equal(await save.evaluate((element) => element === document.activeElement), true, viewport.name + ": keyboard skips Save");
    await Promise.all([page.waitForNavigation(), page.keyboard.press("Enter")]);
    const savedMessage = page.locator(".role-row-message");
    // The icon and text are separate flex items, so read the text span alone.
    assert.equal(await savedMessage.locator(".save-state-text").innerText(), "Saved for Sam Example: added Supervisor, Supply Officer.", viewport.name + ": the saved row does not say what changed");
    assert.equal(await savedMessage.evaluate((element) => element === document.activeElement), true, viewport.name + ": focus did not move to the saved message");
    assert.equal(saved, true, viewport.name + ": the Save request was not submitted");
    assert.equal(await page.getByRole("region", { name: "Role history for sam@example.test" }).count(), 0, viewport.name + ": history belongs on its separate detail page");

    // D1, and staged edits survive a search: the signed-in Administrator's own row.
    await page.goto("http://roles.test/administration/roles/", { waitUntil: "load" });
    const alexRow = page.locator('[data-role-user-row="user-2"]');
    const warning = alexRow.locator("[data-role-self-warning]");
    assert.equal(await warning.isVisible(), false, viewport.name + ": the self-removal warning shows before any change");
    await page.getByRole("checkbox", { name: "Administrator role for alex@example.test" }).uncheck();
    assert.equal(await warning.isVisible(), true, viewport.name + ": unticking your own Administrator role shows no warning");
    assert.match(await alexRow.locator("[data-role-change-status]").innerText(), /You will lose access to Administration when you save\.$/, viewport.name + ": the status does not announce the loss of access");
    await search.fill("");
    await Promise.all([
      page.waitForResponse((response) => new URL(response.url()).searchParams.get("q") === "sam"),
      search.pressSequentially("sam"),
    ]);
    await page.waitForFunction(() => document.querySelector("#role-search-result").textContent === "1 matching active user.");
    assert.equal(await alexRow.isVisible(), true, viewport.name + ": a row with unsaved changes was dropped by the search");
    assert.equal(await page.getByRole("checkbox", { name: "Administrator role for alex@example.test" }).isChecked(), false, viewport.name + ": the search discarded a staged change");
    assert.equal(await samRow.isVisible(), true, viewport.name + ": the matching row is missing after the search");
    await alexRow.getByRole("button", { name: "Reset" }).click();
    await page.waitForFunction(() => !document.querySelector(".role-option.is-pending"));
    assert.equal(await warning.isVisible(), false, viewport.name + ": Reset left the self-removal warning");
    const metrics = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth }));
    assert.ok(metrics.document <= metrics.viewport, viewport.name + ": role page overflows horizontally " + JSON.stringify(metrics));
    await page.setViewportSize({ width: Math.max(320, Math.floor(viewport.width / 2)), height: viewport.height });
    const zoom = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth }));
    assert.ok(zoom.document <= zoom.viewport, viewport.name + " at 200% width: role page overflows " + JSON.stringify(zoom));
    assert.ok(zoom.body <= zoom.viewport, viewport.name + " at 200% width: role page body overflows " + JSON.stringify(zoom));

    await page.emulateMedia({ forcedColors: "active" });
    const forcedColors = await page.evaluate(() => {
      const row = document.querySelector(".role-user-row");
      const option = document.querySelector(".role-option.is-assigned");
      const checkbox = option.querySelector("input[type=checkbox]");
      const searchInput = document.querySelector("#role-user-search");
      const button = row.querySelector(".button");
      const link = row.querySelector("a");
      const probe = document.createElement("span");
      document.body.append(probe);
      const systemColor = (value) => {
        probe.style.color = value;
        return getComputedStyle(probe).color;
      };
      const result = {
        active: matchMedia("(forced-colors: active)").matches,
        rowBorder: getComputedStyle(row).borderBottomColor,
        canvasText: systemColor("CanvasText"),
        optionBorder: getComputedStyle(option).borderTopColor,
        optionBackground: getComputedStyle(option).backgroundColor,
        canvas: systemColor("Canvas"),
        checkboxAccent: getComputedStyle(checkbox).accentColor,
        searchBackground: getComputedStyle(searchInput).backgroundColor,
        field: systemColor("Field"),
        buttonColor: getComputedStyle(button).color,
        buttonText: systemColor("ButtonText"),
        linkColor: getComputedStyle(link).color,
        linkText: systemColor("LinkText"),
      };
      probe.remove();
      return result;
    });
    assert.equal(forcedColors.active, true, viewport.name + ": forced-colors mode did not activate");
    assert.equal(forcedColors.rowBorder, forcedColors.canvasText, viewport.name + ": role row boundary does not use a system color");
    assert.equal(forcedColors.optionBorder, forcedColors.canvasText, viewport.name + ": role option boundary does not use a system color");
    assert.equal(forcedColors.optionBackground, forcedColors.canvas, viewport.name + ": assigned role option does not use Canvas");
    assert.equal(forcedColors.checkboxAccent, "auto", viewport.name + ": role checkbox did not retain native system-color treatment");
    assert.equal(forcedColors.searchBackground, forcedColors.field, viewport.name + ": search field does not use Field");
    assert.equal(forcedColors.buttonColor, forcedColors.buttonText, viewport.name + ": role action does not use ButtonText");
    assert.equal(forcedColors.linkColor, forcedColors.linkText, viewport.name + ": role history link does not use LinkText");
    await page.emulateMedia({ forcedColors: "none" });

    await page.goto("http://roles.test/administration/roles/user-1/history/", { waitUntil: "load" });
    const historyRegion = page.getByRole("region", { name: "Role history for sam@example.test" });
    assert.equal(await historyRegion.count(), 1, viewport.name + ": separate history detail is not exposed accessibly");
    await historyRegion.focus();
    assert.equal(await historyRegion.evaluate((element) => element === document.activeElement), true, viewport.name + ": role history is not keyboard focusable");
    assert.equal(await historyRegion.evaluate((element) => getComputedStyle(element).outlineWidth), "3px", viewport.name + ": role history focus is not visible");
    assert.match(await historyRegion.innerText(), /Sam Example \(sam@example\.test\)[\s\S]*GRANT[\s\S]*SUPERVISOR/);
    console.log(viewport.name + ": compact role matrix, staged review/confirmation and separate history passed");
  } finally {
    await page.close();
  }
}

async function testSupervisorStatusDecisions(viewport) {
  const page = await browser.newPage({ viewport });
  const pending = new Set(["request-limited", "request-out-of-service"]);
  const decisions = [];
  const csrf = "synthetic-csrf";
  const panel = () => {
    const cards = [...pending].map((request) => {
      const limited = request === "request-limited";
      const status = limited ? "Limited Service" : "Out of Service";
      return `<article class="panel"><h3>${status} requested by Sam Example</h3><p>Synthetic request reason.</p><form class="status-write" data-kind="decide" data-decision="APPROVE" data-request="${request}"><input type="hidden" name="csrfmiddlewaretoken" value="${csrf}">${limited ? '<label>Restrictions <input name="restrictions" required></label>' : ""}<label>Explanation <textarea name="explanation" required></textarea></label><button type="submit" class="button primary">Approve</button><p class="status-result" role="status"></p></form><form class="status-write" data-kind="decide" data-decision="DENY" data-request="${request}"><input type="hidden" name="csrfmiddlewaretoken" value="${csrf}"><label>Reason for denying <textarea name="explanation" required></textarea></label><button type="submit" class="button">Deny</button><p class="status-result" role="status"></p></form></article>`;
    }).join("");
    return `<section class="panel" aria-labelledby="pending-heading"><h2 id="pending-heading">Pending requests</h2>${cards || "<p>No requests are waiting.</p>"}</section>`;
  };
  const config = { panel_url: "/api/status/panel", set_url: "/api/status/set", request_url: "/api/status/request", decision_url_template: "/api/status/requests/REQUEST/decision", socket: "/ws/status" };
  const markup = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Vehicle status requests</title><style>${css}</style></head><body><main><h1>Vehicle status requests</h1><p id="status-live-banner" role="status" hidden>Live updates are paused.</p><p id="status-stale" role="alert" hidden>This screen may be out of date.</p><p id="status-update" role="status" hidden>New status update. <button type="button" id="status-update-show">Show the update</button></p><div id="status-live">${panel()}</div></main><script id="status-config" type="application/json">${JSON.stringify(config)}</script><script>window.WebSocket = class { constructor() { setTimeout(() => { if (this.onopen) this.onopen(); }, 0); } close() {} };</script><script>${statusScript}</script></body></html>`;
  await page.route("http://status.test/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/status/panel") {
      await route.fulfill({ status: 200, contentType: "text/html", body: panel() });
      return;
    }
    if (url.pathname.startsWith("/api/status/requests/") && route.request().method() === "POST") {
      const requestId = url.pathname.split("/").at(-2);
      const body = route.request().postDataJSON();
      assert.equal(route.request().headers()["x-csrftoken"], csrf);
      assert.equal(body.operation_id.length > 0, true);
      assert.equal(body.explanation.length > 0, true);
      decisions.push({ requestId, ...body });
      pending.delete(requestId);
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "COMMITTED" }) });
      return;
    }
    await route.fulfill({ status: 200, contentType: "text/html", body: markup });
  });
  try {
    await page.goto("http://status.test/vehicles/synthetic-1/", { waitUntil: "load" });
    assert.equal(await page.getByRole("heading", { name: "Pending requests" }).count(), 1);
    assert.equal(await page.getByRole("heading", { name: "Limited Service requested by Sam Example" }).count(), 1);
    assert.equal(await page.getByRole("heading", { name: "Out of Service requested by Sam Example" }).count(), 1);
    const restrictionBox = await page.getByLabel("Restrictions", { exact: true }).boundingBox();
    assert.ok(restrictionBox && restrictionBox.height >= 48, `${viewport.name}: required restrictions field is shorter than 48 CSS pixels`);
    await page.keyboard.press("Tab");
    assert.equal(await page.getByLabel("Restrictions", { exact: true }).evaluate((element) => element === document.activeElement), true, `${viewport.name}: restricted-service field is not first in keyboard order`);
    await page.keyboard.type("Synthetic restricted-service limit");
    await page.keyboard.press("Tab");
    assert.equal(await page.getByLabel("Explanation", { exact: true }).first().evaluate((element) => element === document.activeElement), true, `${viewport.name}: approval explanation is not keyboard reachable`);
    await page.keyboard.type("Synthetic supervisor approval");
    await page.keyboard.press("Tab");
    assert.equal(await page.getByRole("button", { name: "Approve" }).first().evaluate((element) => element === document.activeElement), true, `${viewport.name}: Approve is not keyboard reachable`);
    const approved = page.waitForResponse((response) => response.url().includes("request-limited/decision") && response.request().method() === "POST");
    await page.keyboard.press("Enter");
    await approved;
    assert.equal(decisions[0].requestId, "request-limited", `${viewport.name}: wrong status request was approved`);
    assert.equal(decisions[0].decision, "APPROVE");
    assert.equal(decisions[0].restrictions, "Synthetic restricted-service limit");
    await page.getByRole("heading", { name: "Limited Service requested by Sam Example" }).waitFor({ state: "detached" });
    await page.keyboard.press("Tab");
    assert.equal(await page.getByLabel("Explanation", { exact: true }).evaluate((element) => element === document.activeElement), true, `${viewport.name}: refreshed request explanation is not first in keyboard order`);
    await page.keyboard.press("Tab"); // Approve
    await page.keyboard.press("Tab"); // Deny reason
    assert.equal(await page.getByLabel("Reason for denying", { exact: true }).evaluate((element) => element === document.activeElement), true, `${viewport.name}: denial reason is not keyboard reachable`);
    await page.keyboard.type("Synthetic denial reason");
    await page.keyboard.press("Tab");
    assert.equal(await page.getByRole("button", { name: "Deny" }).evaluate((element) => element === document.activeElement), true, `${viewport.name}: Deny is not keyboard reachable`);
    const denied = page.waitForResponse((response) => response.url().includes("request-out-of-service/decision") && response.request().method() === "POST");
    await page.keyboard.press("Enter");
    await denied;
    await page.getByText("No requests are waiting.").waitFor();
    assert.equal(decisions[1].requestId, "request-out-of-service");
    assert.equal(decisions[1].decision, "DENY");
    assert.equal(decisions[1].explanation, "Synthetic denial reason");
    const metrics = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth }));
    assert.ok(metrics.document <= metrics.viewport, `${viewport.name}: status page overflows horizontally ${JSON.stringify(metrics)}`);
    await page.setViewportSize({ width: Math.max(320, Math.floor(viewport.width / 2)), height: viewport.height });
    const zoom = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth }));
    assert.ok(zoom.document <= zoom.viewport, `${viewport.name} at 200% width: status page overflows ${JSON.stringify(zoom)}`);
    assert.ok(zoom.body <= zoom.viewport, `${viewport.name} at 200% width: status page body overflows ${JSON.stringify(zoom)}`);
    console.log(`${viewport.name}: supervisor approval/denial, required fields and live refresh passed`);
  } finally {
    await page.close();
  }
}

try {
  await testAgencyTimezoneConversion();
  await testChecklistTimezoneFallback(viewports[0]);
  for (const viewport of viewports) {
    const page = await browser.newPage({ viewport });
    try {
      await testReadinessSummary(viewport);
      await testSortableTables(viewport);
      await testDeficiencyFilters(viewport);
      await testAdministratorRoleChange(viewport);
      await testSupervisorStatusDecisions(viewport);
      await page.route("http://synthetic.test/**", (route) => route.fulfill({
        status: 200,
        contentType: "text/html",
        body: html,
      }));
      await page.goto("http://synthetic.test/", { waitUntil: "load" });
      assert.equal(await page.title(), "Synthetic vehicle inspection");
      assert.equal(await page.getByRole("heading", { name: "Unit 12 inspection" }).count(), 1);
      assert.equal(await page.getByRole("navigation", { name: "Main" }).count(), 1);
      const skipLink = page.getByRole("link", { name: "Skip to main content" });
      await page.keyboard.press("Tab");
      assert.equal(await skipLink.evaluate((element) => element === document.activeElement), true, `${viewport.name}: first Tab did not focus the skip link`);
      const skipBox = await skipLink.boundingBox();
      assert.ok(skipBox && skipBox.y >= 0, `${viewport.name}: focused skip link is not visible`);
      assert.equal(await skipLink.evaluate((element) => getComputedStyle(element).outlineWidth), "3px", `${viewport.name}: skip link focus indicator is missing`);
      await page.keyboard.press("Enter");
      const main = page.locator("#main");
      assert.equal(await main.evaluate((element) => element === document.activeElement), true, `${viewport.name}: skip link did not move focus to main`);
      const mainOutline = await main.evaluate((element) => ({
        width: getComputedStyle(element).outlineWidth,
        style: getComputedStyle(element).outlineStyle,
      }));
      assert.equal(mainOutline.width, "3px", `${viewport.name}: skip-link destination has no visible focus indicator`);
      assert.equal(mainOutline.style, "solid", `${viewport.name}: skip-link destination focus indicator is not solid`);
      await page.emulateMedia({ forcedColors: "active" });
      const forcedInspection = await page.evaluate(() => {
        const probe = document.createElement("span");
        document.body.append(probe);
        const systemColor = (value) => {
          probe.style.color = value;
          return getComputedStyle(probe).color;
        };
        const status = document.querySelector('.status-pill[data-status="OUT_OF_SERVICE"]');
        const action = document.querySelector(".item-controls .button.primary");
        const result = {
          active: matchMedia("(forced-colors: active)").matches,
          focus: getComputedStyle(document.activeElement).outlineColor,
          highlight: systemColor("Highlight"),
          statusText: getComputedStyle(status).color,
          canvasText: systemColor("CanvasText"),
          statusBackground: getComputedStyle(status).backgroundColor,
          canvas: systemColor("Canvas"),
          actionText: getComputedStyle(action).color,
          buttonText: systemColor("ButtonText"),
          statusLabel: status.textContent.trim(),
        };
        probe.remove();
        return result;
      });
      assert.equal(forcedInspection.active, true, `${viewport.name}: forced-colors mode did not activate`);
      assert.equal(forcedInspection.focus, forcedInspection.highlight, `${viewport.name}: main focus is not visible with the system highlight color`);
      assert.equal(forcedInspection.statusText, forcedInspection.canvasText, `${viewport.name}: inspection status text does not use a system color`);
      assert.equal(forcedInspection.statusBackground, forcedInspection.canvas, `${viewport.name}: inspection status background does not use Canvas`);
      assert.equal(forcedInspection.actionText, forcedInspection.buttonText, `${viewport.name}: inspection action does not use ButtonText`);
      assert.equal(forcedInspection.statusLabel, "Out of service", `${viewport.name}: status wording disappeared in forced-colors mode`);
      await page.emulateMedia({ forcedColors: "none" });
      // Navigation (issue #194): phone bottom bar, tablet rail, desktop sidebar; More uses a hamburger icon.
      const nav = page.locator("nav.app-nav");
      const more = page.locator("#app-nav-more");
      const morePanel = page.locator("#app-nav-more-panel");
      const inspectTab = page.getByRole("link", { name: "Inspect" });
      assert.equal(await inspectTab.getAttribute("aria-current"), "page", `${viewport.name}: current tab is not marked`);
      for (const label of ["Home", "Inspect", "Problems", "Vehicles"]) {
        // Problems carries its badge count in its name ("Problems, 57 open").
        const tab = page.getByRole("link", { name: label === "Problems" ? "Problems, 57 open" : label, exact: true });
        assert.equal(await tab.isVisible(), true, `${viewport.name}: ${label} tab is not visible`);
        const box = await tab.boundingBox();
        assert.ok(box.height >= 48 && box.width >= 48, `${viewport.name}: ${label} tab is smaller than 48 CSS pixels ${JSON.stringify(box)}`);
      }
      const navBox = await nav.boundingBox();
      await captureBrowserEvidence(page, `shell-${viewport.name}.png`);
      if (viewport.width < 1024) {
        assert.equal(await more.isVisible(), true, `${viewport.name}: More is not visible`);
        assert.equal(await more.textContent(), "More", `${viewport.name}: More has no visible name`);
        assert.equal(await page.getByRole("button", { name: "More menu", exact: true }).count(), 1, `${viewport.name}: More's accessible name is not "More menu"`);
        assert.equal(await morePanel.isVisible(), false, `${viewport.name}: More panel is not initially closed`);
        if (viewport.width < 768) {
          assert.ok(Math.abs(navBox.y + navBox.height - viewport.height) <= 1, `${viewport.name}: tab bar is not at the bottom ${JSON.stringify(navBox)}`);
          const clearance = await page.evaluate(() => parseFloat(getComputedStyle(document.body).paddingBottom));
          assert.ok(clearance >= navBox.height, `${viewport.name}: page content can be hidden under the tab bar (${clearance} < ${navBox.height})`);
        } else {
          assert.ok(navBox.x === 0 && navBox.width <= 100, `${viewport.name}: navigation rail is not on the left ${JSON.stringify(navBox)}`);
        }
        await more.click();
        assert.equal(await more.getAttribute("aria-expanded"), "true", `${viewport.name}: opening More did not update aria-expanded`);
        assert.equal(await page.getByRole("link", { name: "Kits" }).isVisible(), true, `${viewport.name}: More did not show Kits`);
        assert.equal(await page.getByRole("button", { name: "Sign out" }).isVisible(), true, `${viewport.name}: More did not show Sign out`);
        const panelBox = await morePanel.boundingBox();
        assert.ok(panelBox.x >= 0 && panelBox.x + panelBox.width <= viewport.width + 1 && panelBox.y >= 0, `${viewport.name}: More panel is off screen ${JSON.stringify(panelBox)}`);
        await page.keyboard.press("Escape");
        assert.equal(await more.evaluate((element) => element === document.activeElement), true, `${viewport.name}: Escape did not return focus to More`);
        assert.equal(await more.getAttribute("aria-expanded"), "false", `${viewport.name}: Escape did not close More`);
        assert.equal(await morePanel.isVisible(), false, `${viewport.name}: More panel remains visible after Escape`);
        await more.click();
        await page.locator("main h1").click();
        assert.equal(await more.getAttribute("aria-expanded"), "false", `${viewport.name}: a click outside did not close More`);
        await more.focus();
        await page.setViewportSize({ width: 1024, height: viewport.height });
        await waitForNavLayout(page, true);
        assert.equal(await morePanel.isVisible(), true, `${viewport.name}: sidebar does not list More's places at desktop width`);
        assert.equal(await page.getByRole("link", { name: "Kits" }).evaluate((element) => element === document.activeElement), true, `${viewport.name}: focus did not move from More into the sidebar`);
        await page.setViewportSize({ width: viewport.width, height: viewport.height });
        await waitForNavLayout(page, false);
        assert.equal(await morePanel.isVisible(), false, `${viewport.name}: More panel did not close after resizing back`);
      } else {
        assert.equal(await more.isVisible(), false, `${viewport.name}: More should be hidden in the sidebar layout`);
        assert.equal(await morePanel.isVisible(), true, `${viewport.name}: sidebar should list More's places`);
        assert.ok(navBox.x === 0 && navBox.width >= 200, `${viewport.name}: sidebar is not on the left ${JSON.stringify(navBox)}`);
        const mainBox = await page.locator("main").boundingBox();
        assert.ok(mainBox.x >= navBox.x + navBox.width, `${viewport.name}: page content overlaps the sidebar`);
      }
      await testInspectionNavigation(viewport);
      await testDateTimeFocus(viewport);
      await testChecklistAids(viewport);
      await testTapFeedback(viewport);
      await testKeyboardShortcuts(viewport);
      await testEmptyAndLoadingStates(viewport);
      await testInspectionOutcomeSemantics(viewport);
      assert.equal(await page.getByRole("button", { name: "Record passed" }).count(), 1);
      assert.equal(await page.getByLabel("Inspection note").count(), 1);
      // The page save line plus the shell toast region (issue #194), which stays empty until a save.
      assert.equal(await page.getByRole("status").count(), 2);
      assert.equal(await page.locator(".toast").textContent(), "");
      // The theme radios sit in the closed More panel below 1024 px, so read the input's state
      // directly rather than by role, which skips hidden elements (PR #253 CI).
      assert.equal(await page.locator('input[name="theme"][value="auto"]').isChecked(), true);
      assert.equal(await page.getByText("2 pending requests").count(), 1);
      assert.equal(await page.locator('.pending-pill[data-count="2"]').isVisible(), true);
      assert.equal(await page.getByRole("heading", { name: "Next action" }).isVisible(), true);
      assert.equal(await page.getByRole("button", { name: "Record repair" }).isVisible(), true);
      const deficiencyDetails = page.locator("#deficiency-work-updates");
      const deficiencySummary = deficiencyDetails.locator("summary");
      assert.equal(await deficiencyDetails.evaluate((element) => element.open), false, `${viewport.name}: secondary deficiency actions should start collapsed`);
      assert.equal(await deficiencySummary.count(), 1, `${viewport.name}: disclosure summary is missing`);
      const disclosureBox = await deficiencySummary.boundingBox();
      assert.ok(disclosureBox && disclosureBox.height >= 48, `${viewport.name}: disclosure target is shorter than 48 CSS pixels`);
      await deficiencySummary.focus();
      assert.equal(
        await deficiencySummary.evaluate((element) => document.activeElement === element),
        true,
        `${viewport.name}: disclosure summary is not keyboard reachable`,
      );
      await page.keyboard.press("Enter");
      assert.equal(await deficiencyDetails.evaluate((element) => element.open), true, `${viewport.name}: Enter did not expand secondary actions`);
      assert.equal(await page.getByLabel("Progress note").isVisible(), true, `${viewport.name}: expanded disclosure content is not visible`);
      await page.keyboard.press("Space");
      assert.equal(await deficiencyDetails.evaluate((element) => element.open), false, `${viewport.name}: Space did not collapse secondary actions`);
      assert.equal(await page.getByLabel("Progress note").isVisible(), false, `${viewport.name}: collapsed disclosure content remains visible`);

      const metrics = await page.evaluate(() => {
        const root = document.documentElement;
        const passed = document.querySelector('[data-status="IN_SERVICE"]');
        const unavailable = document.querySelector('[data-status="OUT_OF_SERVICE"]');
        const action = document.querySelector(".item-controls .button");
        return {
          viewport: innerWidth,
          documentWidth: root.scrollWidth,
          bodyWidth: document.body.scrollWidth,
          statusVisible: passed.getBoundingClientRect().width > 0 && unavailable.getBoundingClientRect().width > 0,
          statusColorsDiffer: getComputedStyle(passed).backgroundColor !== getComputedStyle(unavailable).backgroundColor,
          actionHeight: action.getBoundingClientRect().height,
        };
      });
      assert.ok(metrics.documentWidth <= viewport.width, `${viewport.name}: horizontal document overflow ${JSON.stringify(metrics)}`);
      assert.ok(metrics.bodyWidth <= viewport.width, `${viewport.name}: horizontal body overflow ${JSON.stringify(metrics)}`);
      assert.ok(metrics.statusVisible, `${viewport.name}: status indicators are not visible`);
      assert.ok(metrics.statusColorsDiffer, `${viewport.name}: service states have indistinguishable backgrounds`);
      assert.ok(metrics.actionHeight >= 48, `${viewport.name}: action target is shorter than 48 CSS pixels`);

      // Issue #194: the Problems badge stays inside its tab and never covers the label.
      const badge = await page.evaluate(() => {
        const node = document.querySelector(".app-nav-badge"), link = node.closest("a"), label = link.querySelector(".app-nav-label");
        const b = node.getBoundingClientRect(), l = link.getBoundingClientRect(), t = label.getBoundingClientRect();
        return {
          visible: b.width > 0 && b.height > 0,
          inside: b.left >= l.left - 0.5 && b.right <= l.right + 0.5 && b.top >= l.top - 0.5 && b.bottom <= l.bottom + 0.5,
          coversLabel: !(b.right <= t.left || b.left >= t.right || b.bottom <= t.top || b.top >= t.bottom),
        };
      });
      assert.ok(badge.visible && badge.inside && !badge.coversLabel, `${viewport.name}: Problems badge misplaced ${JSON.stringify(badge)}`);
      assert.equal(await page.getByRole("link", { name: "Problems, 57 open" }).count(), 1, `${viewport.name}: the badge count is not in the link's name`);
      await page.emulateMedia({ colorScheme: "dark" });
      await openMore(page);
      if (await page.locator("#app-nav-more").isVisible()) {
        // Owner phone check (#194): the open More carries the highlight; the page's tab steps back
        // visually but keeps aria-current="page".
        const marks = await page.evaluate(() => {
          const more = getComputedStyle(document.getElementById("app-nav-more"));
          const node = document.querySelector('.app-nav-link[aria-current="page"]');
          const current = node ? getComputedStyle(node) : null;
          return {
            moreEdge: more.borderTopColor === more.color || more.borderLeftColor === more.color,
            moreColor: more.color,
            currentColor: current && current.color,
            currentEdge: current && (current.borderTopColor + " " + current.borderLeftColor),
            currentAria: node && node.getAttribute("aria-current"),
          };
        });
        assert.ok(marks.moreEdge, `${viewport.name}: the open More is not highlighted ${JSON.stringify(marks)}`);
        // Its places look like the app's cards: bordered, rounded and at least 48px tall.
        const place = await page.locator(".app-nav-more-list a").first().evaluate((node) => {
          const style = getComputedStyle(node);
          return { border: parseFloat(style.borderTopWidth), radius: parseFloat(style.borderTopLeftRadius), height: node.getBoundingClientRect().height };
        });
        assert.ok(place.border >= 1 && place.radius > 0 && place.height >= 48, `${viewport.name}: More places are not styled as cards ${JSON.stringify(place)}`);
        if (marks.currentAria) {
          assert.equal(marks.currentAria, "page");
          assert.notEqual(marks.currentColor, marks.moreColor, `${viewport.name}: the page tab still looks selected while More is open ${JSON.stringify(marks)}`);
        }
      }
      const themeChoice = (name) => page.getByRole("radio", { name, exact: true });
      // Issue #238: each theme choice is a 48 px target inside one named group.
      assert.equal(await page.getByRole("group", { name: "Theme" }).count(), 1, `${viewport.name}: theme choices are not one named group`);
      for (const name of ["Auto", "Light", "Dark"]) {
        const segment = await page.locator(".theme-choice", { hasText: name }).boundingBox();
        assert.ok(segment && segment.height >= 48, `${viewport.name}: theme choice ${name} is shorter than 48 CSS pixels`);
      }
      const darkBackground = await page.locator("body").evaluate((element) => getComputedStyle(element).backgroundColor);
      assert.equal(darkBackground, "rgb(11, 20, 38)", `${viewport.name}: Auto did not follow dark system preference`);
      await themeChoice("Light").check();
      assert.equal(await page.locator("body").evaluate((element) => getComputedStyle(element).backgroundColor), "rgb(245, 247, 250)", `${viewport.name}: Light override did not apply`);
      await themeChoice("Dark").check();
      assert.equal(await page.locator("body").evaluate((element) => getComputedStyle(element).backgroundColor), "rgb(11, 20, 38)", `${viewport.name}: Dark override did not apply`);
      await page.reload({ waitUntil: "load" });
      assert.equal(await page.locator('input[name="theme"][value="dark"]').isChecked(), true, `${viewport.name}: saved theme was not restored`);
      assert.equal(await page.evaluate(() => window.__themeBeforeStyles), "dark", `${viewport.name}: saved theme was not applied before the page stylesheet`);
      assert.equal(await page.locator("body").evaluate((element) => getComputedStyle(element).backgroundColor), "rgb(11, 20, 38)", `${viewport.name}: restored Dark theme did not apply`);
      await openMore(page);
      await themeChoice("Auto").check();
      await page.emulateMedia({ colorScheme: "light" });
      assert.equal(await page.locator("body").evaluate((element) => getComputedStyle(element).backgroundColor), "rgb(245, 247, 250)", `${viewport.name}: Auto did not follow light system preference`);

      await page.evaluate(() => {
        const state = document.getElementById("save-state");
        window.cyaShell.setSaveState(state, "unconfirmed");
        window.dispatchEvent(new Event("offline"));
      });
      assert.match(await page.locator("#save-state").innerText(), /Unconfirmed\. It may or may not be recorded\./);
      assert.equal(await page.getByRole("alert").isVisible(), true);
      await page.evaluate(() => window.dispatchEvent(new Event("online")));
      assert.equal(await page.getByRole("alert").isVisible(), false);
      // Half width approximates 200% zoom where possible; never test below 320 CSS px.
      const zoomWidth = Math.max(320, Math.floor(viewport.width / 2));
      await page.setViewportSize({ width: zoomWidth, height: viewport.height });
      const zoomMetrics = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth }));
      assert.ok(zoomMetrics.document <= zoomMetrics.viewport, `${viewport.name} at 200%: document overflow ${JSON.stringify(zoomMetrics)}`);
      assert.ok(zoomMetrics.body <= zoomMetrics.viewport, `${viewport.name} at 200%: body overflow ${JSON.stringify(zoomMetrics)}`);
      console.log(`${viewport.name}: passed (${metrics.viewport}px; no horizontal overflow)`);
    } catch (error) {
      mkdirSync(artifacts, { recursive: true });
      const screenshot = path.join(artifacts, `${viewport.name}.png`);
      await page.screenshot({ path: screenshot, fullPage: true }).catch(() => undefined);
      failures.push(`${viewport.name}: ${error.message}; screenshot: ${screenshot}`);
    } finally {
      await page.close();
    }
  }
} finally {
  await browser.close();
}

if (failures.length) {
  console.error(failures.join("\n"));
  process.exitCode = 1;
} else {
  console.log(`Cross-device browser smoke passed at ${viewports.map(({ width }) => width).join(", ")} CSS-pixel widths.`);
}
