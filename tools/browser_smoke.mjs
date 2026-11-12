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
const checklistScript = readFileSync(
  path.join(root, "cya/inspections/templates/inspections/checklist.js"),
  "utf8",
);
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
<body>
<a class="skip-link" href="#main">Skip to main content</a>
<header class="site-header"><a class="brand" href="#main">Check Your Ambulance</a><button id="site-nav-toggle" class="button site-nav-toggle" type="button" aria-controls="site-nav-list" aria-expanded="false" hidden>Menu</button><div class="header-tools"><div class="account"><span class="account-name">Sam Example</span><form><button class="button" type="button">Sign out</button></form></div><label class="theme-control" for="theme-select">Theme<select id="theme-select"><option value="auto">Auto</option><option value="light">Light</option><option value="dark">Dark</option></select></label></div></header>
<nav class="site-nav" aria-label="Main"><ul id="site-nav-list" class="site-nav-list"><li><a href="#main" aria-current="page">Inspection</a></li><li><a href="#status">Vehicle status</a></li><li><a href="#deficiency">Deficiencies</a></li></ul></nav>
<div id="connection-banner" class="banner banner-error" role="alert" hidden><span class="indicator-icon" aria-hidden="true">&#9888;</span><strong>Connection lost.</strong> Changes are not being saved. An item marked Unconfirmed may have been recorded.</div>
<main id="main" tabindex="-1"><h1>Unit 12 inspection</h1>
<section id="status" class="status-line" aria-label="Vehicle status"><span class="pill status-pill" data-status="IN_SERVICE">In service</span><span class="pill status-pill" data-status="OUT_OF_SERVICE">Out of service</span><span class="pill pill-attention">Open deficiency</span></section>
<ul class="vehicle-list"><li><a class="vehicle-link" href="#status"><span class="vehicle-name">Unit 12 — Synthetic Regional Response Ambulance</span><span class="vehicle-badges"><span class="pill pending-pill" data-count="2">2 pending requests</span><span class="pill status-pill" data-status="OUT_OF_SERVICE">Out of service</span></span></a></li></ul>
<section id="deficiency" class="item item-attention" aria-labelledby="item-title"><div class="item-head"><h2 id="item-title">Synthetic oxygen cylinder</h2><span class="pill pill-attention">Known deficiency</span></div><p>Pressure below the approved range.</p><div class="item-controls" aria-label="Inspection actions"><button class="button primary" type="button" aria-label="Record passed">Passed</button><button class="button" type="button" aria-label="Record unable to inspect">Unable to inspect</button></div><label class="panel">Inspection note<input aria-label="Inspection note" type="text" value="Synthetic check"></label><p id="save-state" class="save-state" role="status"><span class="indicator-icon" aria-hidden="true">?</span><span class="save-state-text">Unconfirmed</span></p></section>
<section class="panel deficiency-overview" aria-labelledby="deficiency-overview-title"><h2 id="deficiency-overview-title">Synthetic oxygen cylinder pressure issue</h2><p class="deficiency-state"><strong>In Progress</strong> · High severity</p><p>Synthetic Ambulance 12</p><dl class="deficiency-facts"><dt>Affected scope</dt><dd>Synthetic oxygen cylinder</dd><dt>Assignee</dt><dd>Sam Example</dd><dt>Governing deadline</dt><dd>2026-11-15 12:00 UTC · Overdue</dd></dl></section>
<section class="panel deficiency-next-action" aria-labelledby="next-action-title"><h2 id="next-action-title">Next action</h2><form class="workflow-write"><p>Record that the repair is complete and request verification.</p><button class="button primary" type="button">Record repair</button></form></section>
<details class="panel deficiency-details" id="deficiency-work-updates"><summary>Work updates and dependencies</summary><form class="panel workflow-write"><h2>Add progress note</h2><label>Progress note<textarea aria-label="Progress note"></textarea></label><button class="button" type="button">Add note</button></form></details>
<section class="role-administration" aria-labelledby="role-history-heading"><h2 id="role-history-heading">Role history</h2><div class="role-history-scroll" role="region" aria-label="Role history rows" tabindex="0"><table class="role-history-table"><thead><tr><th>When (agency local time)</th><th>User</th><th>Action</th><th>Role</th><th>Changed by</th></tr></thead><tbody><tr><td>2026-11-12 12:34:56 UTC</td><td>Synthetic User With A Long Display Name (user@example.test)</td><td>GRANT</td><td>SUPERVISOR</td><td>Synthetic Administrator (admin@example.test)</td></tr></tbody></table></div></section>
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
  <section id="area-overview"><h3 id="area-overview-heading" tabindex="-1">Choose an area or compartment</h3><p class="muted">Progress counts checks for the current inspection window. Known deficiencies remain open until resolved.</p><div id="area-cards" class="area-cards"></div></section>
  <section id="area-detail" hidden><button id="area-overview-button" class="button" type="button">All areas and compartments</button><h3 id="area-heading" tabindex="-1"></h3></section><p id="screen-notice" hidden></p><ol id="items" class="items" aria-live="polite"></ol></section>
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
    assert.match(await cab.innerText(), /1 of 4 items checked in this window/);
    assert.match(await cab.innerText(), /1 due · 0 due soon · 1 overdue · 0 not due · 0 without an obligation · 1 exceptions · 1 open deficiencies/);
    const metrics = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth, cardTarget: document.querySelector(".area-card").getBoundingClientRect().height }));
    assert.ok(metrics.document <= viewport.width, `${viewport.name}: inspection overview overflows horizontally ${JSON.stringify(metrics)}`);
    assert.ok(metrics.body <= viewport.width, `${viewport.name}: inspection body overflows horizontally ${JSON.stringify(metrics)}`);
    assert.ok(metrics.cardTarget >= 48, `${viewport.name}: compartment target is shorter than 48px`);
    await cab.click();
    await page.getByRole("heading", { name: "Cab", exact: true }).waitFor();
    assert.equal(await page.locator("#area-overview").isVisible(), false);
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
    assert.match(await failed.innerText(), /Deficiency status: Open\. Severity: High/);
    assert.equal(await failed.locator(".pill-attention").count(), 0, `${viewport.name}: an open deficiency is not repeated as a second attention badge`);
    const passedWithOpenDeficiency = page.locator('#items > li[data-item-key="item-pass-open|"]');
    assert.match(await passedWithOpenDeficiency.innerText(), /Passed/);
    assert.match(await passedWithOpenDeficiency.innerText(), /Latest recorded check: Pass by Synthetic EMT/);
    assert.match(await passedWithOpenDeficiency.innerText(), /Deficiency status: Open\. Severity: Medium/);
    const priorPass = page.locator('#items > li[data-item-key="item-prior-pass|"]');
    assert.match(await priorPass.innerText(), /Due/);
    assert.match(await priorPass.innerText(), /Latest recorded check: Pass by Synthetic EMT/);
    const unable = page.locator('#items > li[data-item-key="item-unable|"]');
    assert.match(await unable.innerText(), /Latest recorded check: Unable to inspect by Synthetic EMT/);
    assert.match(await unable.innerText(), /Deficiency status: Pending verification/);
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
    console.log(`${viewport.name}: recorded outcome, window status, deficiency and neutral keyboard actions passed`);
  } finally {
    await page.close();
  }
}
const artifacts = process.env.BROWSER_SMOKE_OUTPUT_DIR || path.join(os.tmpdir(), `cya-browser-smoke-${process.pid}`);
const browser = await chromium.launch({ headless: true });
const failures = [];
async function testRoleHistoryResponsive(page, viewport) {
  const region = page.getByRole("region", { name: "Role history rows" });
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
  assert.match(metrics.historyText, /2026-11-12 12:34:56 UTC/);
  assert.match(metrics.historyText, /Synthetic User With A Long Display Name/);
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
      }));
      assert.deepEqual(actual, {
        regular: "2026-06-01T16:00:00.000Z",
        fold: "2026-11-01T05:30:00.000Z",
        gap: "2026-03-08T07:00:00.000Z",
        southernFold: "2026-04-04T14:45:00.000Z",
        southernGap: "2026-10-03T15:30:00.000Z",
        savedCheck: "2026-06-01 12:00 EDT (GMT-4)",
        auditCheck: "2026-06-01 12:00:00 EDT (GMT-4)",
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
async function waitForNavLayout(page, narrow) {
  await page.waitForFunction((isNarrow) => {
    const toggle = document.getElementById("site-nav-toggle");
    const list = document.getElementById("site-nav-list");
    return window.matchMedia("(max-width: 47.999rem)").matches === isNarrow
      && toggle.hidden === !isNarrow
      && list.hidden === (isNarrow && toggle.getAttribute("aria-expanded") !== "true");
  }, narrow);
}

async function testReadinessSummary(viewport) {
  const page = await browser.newPage({ viewport });
  const readiness = `<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1"><title>Fleet readiness</title>
    <style>${css}</style></head><body><main id="main"><h1>Fleet readiness</h1>
    <div class="readiness-filter"><input class="readiness-filter-input" id="readiness-attention-only" type="checkbox" aria-describedby="readiness-filter-help"><div class="readiness-filter-copy"><label for="readiness-attention-only">Show vehicles needing attention</label><p id="readiness-filter-help" class="readiness-filter-help">Includes non-In-Service status, failed, overdue or missed checks, active deficiencies among the top two active configured severity ranks (or any active deficiency if no level is active), suspensions, or a truncated deficiency list. These independent signals do not combine into a compliance or operational status.</p></div>
    <div id="readiness-panel"><p>Each state is counted on its own; nothing here is a single &quot;compliant&quot; flag.</p>
      <section class="panel readiness-vehicle" data-vehicle-id="vehicle-a" data-attention="true" aria-labelledby="vehicle-a-heading">
        <div class="readiness-overview"><div class="readiness-identity"><h2 id="vehicle-a-heading">Medic 6</h2><span class="pill status-pill" data-status="IN_SERVICE">In Service</span></div>
        <div class="readiness-measure"><h3>Inspection progress</h3><p>8 of 10 owed checks recorded</p></div>
        <ul class="readiness-signals" aria-label="Independent attention counts"><li class="pill pill-attention">Failed: 1</li><li class="pill pill-attention">Overdue: 2</li><li class="pill">Missed: 0</li><li class="pill pill-attention">Critical / High shown: 1</li><li class="pill">Suspensions: 0</li></ul>
        <nav class="readiness-actions" aria-label="Medic 6 workflows"><a href="/inspection/vehicles/vehicle-a/">Open vehicle inspection</a><a href="/status/vehicles/vehicle-a/">Current status workflow</a><a href="/deficiencies/def-a/">Open oldest active deficiency</a></nav></div>
        <details class="readiness-details" data-vehicle-id="vehicle-a"><summary>Obligations, deficiencies and suspensions for Medic 6</summary><div class="readiness-detail-content"><h3>Obligations by state</h3><ul><li>Failed: 1</li><li>Overdue: 2</li></ul></div></details>
      </section>
      <section class="panel readiness-vehicle" data-vehicle-id="vehicle-b" data-attention="false"><div class="readiness-overview"><div class="readiness-identity"><h2>Medic 7</h2><span class="pill status-pill" data-status="IN_SERVICE">In Service</span></div><div class="readiness-measure"><h3>Inspection progress</h3><p>10 of 10 owed checks recorded</p></div><ul class="readiness-signals" aria-label="Independent attention counts"><li class="pill">Failed: 0</li><li class="pill">Overdue: 0</li><li class="pill">Missed: 0</li><li class="pill">Critical / High shown: 0</li><li class="pill">Suspensions: 0</li></ul></div></section>
      <section class="panel readiness-vehicle" data-vehicle-id="vehicle-c" data-attention="true"><div class="readiness-overview"><div class="readiness-identity"><h2>Medic 8</h2><span class="pill status-pill" data-status="OUT_OF_SERVICE">Out of Service</span></div><div class="readiness-measure"><h3>Inspection progress</h3><p>10 of 10 owed checks recorded</p></div><ul class="readiness-signals" aria-label="Independent attention counts"><li class="pill">Failed: 0</li><li class="pill">Overdue: 0</li><li class="pill">Missed: 0</li><li class="pill">Critical / High shown: 0</li><li class="pill">Suspensions: 0</li></ul></div></section>
    </div></div></main></body></html>`;
  try {
    await page.route("http://readiness.test/**", (route) => route.fulfill({ status: 200, contentType: "text/html", body: readiness }));
    await page.goto("http://readiness.test/", { waitUntil: "load" });
    assert.equal(await page.getByRole("heading", { name: "Fleet readiness" }).count(), 1);
    assert.match(await page.locator('.readiness-vehicle[data-vehicle-id="vehicle-a"]').innerText(), /In Service[\s\S]*Failed: 1[\s\S]*Overdue: 2[\s\S]*Missed: 0[\s\S]*Critical \/ High shown: 1[\s\S]*Suspensions: 0/);
    assert.equal(await page.getByRole("link", { name: "Open vehicle inspection" }).getAttribute("href"), "/inspection/vehicles/vehicle-a/");
    assert.equal(await page.getByRole("link", { name: "Current status workflow" }).getAttribute("href"), "/status/vehicles/vehicle-a/");
    assert.equal(await page.getByRole("link", { name: "Open oldest active deficiency" }).getAttribute("href"), "/deficiencies/def-a/");
    const filter = page.getByRole("checkbox", { name: "Show vehicles needing attention" });
    await filter.focus();
    assert.equal(await filter.evaluate((element) => element === document.activeElement), true, `${viewport.name}: attention filter is not keyboard-focusable`);
    await page.keyboard.press("Space");
    assert.equal(await page.locator('.readiness-vehicle[data-vehicle-id="vehicle-a"]').isVisible(), true, `${viewport.name}: attention vehicle was hidden by the filter`);
    assert.equal(await page.locator('.readiness-vehicle[data-vehicle-id="vehicle-c"]').isVisible(), true, `${viewport.name}: non-In-Service status was not filtered as an independent signal`);
    assert.equal(await page.locator('.readiness-vehicle[data-vehicle-id="vehicle-b"]').isVisible(), false, `${viewport.name}: vehicle without attention signals remained in the filtered view`);
    const summary = page.getByText("Obligations, deficiencies and suspensions for Medic 6");
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
    <meta name="viewport" content="width=device-width, initial-scale=1"><title>Deficiencies</title>
    <style>${css}</style></head><body><main id="main"><h1>Deficiencies</h1>
    <form class="panel deficiency-filter-form" method="get" action="/deficiencies/">
      <div class="deficiency-filter-grid">
        <label for="deficiency-search">Search deficiency text<input id="deficiency-search" type="search" name="q" value="brake" maxlength="100"></label>
        <label for="deficiency-vehicle">Vehicle name or number<input id="deficiency-vehicle" type="search" name="vehicle" value="Medic 6" maxlength="100"></label>
        <label for="deficiency-severity">Severity<select id="deficiency-severity" name="severity"><option value="">Any severity</option><option value="URGENT" selected>Urgent (URGENT)</option><option value="RETIRED">Legacy (RETIRED, retired)</option></select></label>
        <label for="deficiency-assignee">Assigned to<input id="deficiency-assignee" type="search" name="assignee" value="Sam Example" maxlength="100"></label>
        <label for="deficiency-status">Lifecycle status<select id="deficiency-status" name="status"><option value="active">Outstanding only</option><option value="all">All statuses, including closed and Reported in Error</option><option value="OPEN">Open</option><option value="REPORTED_IN_ERROR">Reported in Error</option></select></label>
        <label class="check" for="deficiency-overdue"><input id="deficiency-overdue" type="checkbox" name="overdue" value="1"> Overdue only</label>
      </div><div class="deficiency-filter-actions"><button class="button primary" type="submit">Apply filters</button><a class="button" href="/deficiencies/">Clear filters</a></div>
    </form><p class="deficiency-applied-filters"><strong>Applied filters:</strong> Text: brake · Vehicle: Medic 6</p>
    <p class="deficiency-results-count">42 matching deficiencies; showing 25 on this page.</p>
    <ul class="plain"><li class="panel"><a href="/deficiencies/one/">Synthetic brake warning</a> — Medic 6 (North County Regional Ambulance)</li></ul>
    <nav class="deficiency-pagination" aria-label="Deficiency result pages"><a class="button" href="/deficiencies/?q=brake&amp;results_page=1">Newer results</a><a class="button" href="/deficiencies/?q=brake&amp;results_page=2">Older results</a></nav>
    </main></body></html>`;
  try {
    await page.route("http://deficiencies.test/**", (route) => route.fulfill({ status: 200, contentType: "text/html", body: listing }));
    await page.goto("http://deficiencies.test/", { waitUntil: "load" });
    assert.equal(await page.getByRole("heading", { name: "Deficiencies" }).count(), 1);
    assert.equal(await page.getByRole("searchbox", { name: "Search deficiency text" }).inputValue(), "brake");
    assert.equal(await page.getByLabel("Lifecycle status").locator('option[value="REPORTED_IN_ERROR"]').textContent(), "Reported in Error");
    assert.equal(await page.getByLabel("Severity").inputValue(), "URGENT");
    assert.equal(await page.getByRole("link", { name: "Older results" }).getAttribute("href"), "/deficiencies/?q=brake&results_page=2");
    assert.match(await page.getByText("42 matching deficiencies").innerText(), /showing 25/);
    for (const label of ["Search deficiency text", "Vehicle name or number", "Severity", "Assigned to", "Lifecycle status"]) {
      const control = page.getByLabel(label);
      const box = await control.boundingBox();
      assert.ok(box && box.height >= 48, `${viewport.name}: ${label} target is shorter than 48 CSS pixels`);
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

try {
  await testAgencyTimezoneConversion();
  await testChecklistTimezoneFallback(viewports[0]);
  for (const viewport of viewports) {
    const page = await browser.newPage({ viewport });
    try {
      await testReadinessSummary(viewport);
      await testDeficiencyFilters(viewport);
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
      await testRoleHistoryResponsive(page, viewport);
      const navToggle = page.locator("#site-nav-toggle");
      const navList = page.locator("#site-nav-list");
      assert.equal(await navToggle.evaluate((element) => element.closest(".site-header") !== null), true, `${viewport.name}: menu is outside the compact header`);
      assert.equal(await navToggle.evaluate((element) => element.closest(".site-nav") === null), true, `${viewport.name}: menu remains in its own navigation row`);
      await captureBrowserEvidence(page, `shell-${viewport.name}.png`);
      if (viewport.width < 768) {
        assert.equal(await navToggle.isVisible(), true, `${viewport.name}: mobile menu toggle is not visible`);
        assert.equal(await navList.isVisible(), false, `${viewport.name}: mobile navigation is not initially collapsed`);
        await navToggle.click();
        assert.equal(await navToggle.getAttribute("aria-expanded"), "true", `${viewport.name}: opening the menu did not update aria-expanded`);
        assert.equal(await page.getByRole("link", { name: "Inspection" }).getAttribute("aria-current"), "page");
        assert.equal(await page.getByRole("link", { name: "Deficiencies" }).isVisible(), true);
        await page.keyboard.press("Escape");
        assert.equal(await navToggle.evaluate((element) => element === document.activeElement), true, `${viewport.name}: Escape did not return focus to the menu toggle`);
        assert.equal(await navToggle.getAttribute("aria-expanded"), "false", `${viewport.name}: Escape did not close the menu`);
        assert.equal(await navList.isVisible(), false, `${viewport.name}: menu remains visible after Escape`);
        await page.setViewportSize({ width: 768, height: viewport.height });
        await waitForNavLayout(page, false);
        assert.equal(await navList.isVisible(), true, `${viewport.name}: navigation is not visible at desktop width`);
        assert.equal(await page.getByRole("link", { name: "Inspection" }).evaluate((element) => element === document.activeElement), true, `${viewport.name}: focus was not returned to the current link at desktop width`);
        await page.setViewportSize({ width: viewport.width, height: viewport.height });
        await waitForNavLayout(page, true);
        assert.equal(await navToggle.isVisible(), true, `${viewport.name}: menu toggle is not visible after resizing to mobile`);
        assert.equal(await navToggle.evaluate((element) => element === document.activeElement), true, `${viewport.name}: focus was not returned to the mobile menu toggle`);
      } else {
        assert.equal(await navToggle.isVisible(), false, `${viewport.name}: desktop menu toggle should be hidden`);
        assert.equal(await navList.isVisible(), true, `${viewport.name}: desktop navigation should be visible`);
        await page.getByRole("link", { name: "Inspection" }).focus();
        await page.setViewportSize({ width: 600, height: viewport.height });
        await waitForNavLayout(page, true);
        assert.equal(await navToggle.isVisible(), true, `${viewport.name}: menu toggle is not visible after resizing to mobile`);
        assert.equal(await navToggle.evaluate((element) => element === document.activeElement), true, `${viewport.name}: focus was not returned to the mobile menu toggle`);
        await page.setViewportSize({ width: viewport.width, height: viewport.height });
        await waitForNavLayout(page, false);
        assert.equal(await navList.isVisible(), true, `${viewport.name}: navigation is not visible at desktop width`);
        assert.equal(await page.getByRole("link", { name: "Inspection" }).evaluate((element) => element === document.activeElement), true, `${viewport.name}: focus was not returned to the current link at desktop width`);
      }
      await testInspectionNavigation(viewport);
      await testInspectionOutcomeSemantics(viewport);
      assert.equal(await page.getByRole("button", { name: "Record passed" }).count(), 1);
      assert.equal(await page.getByLabel("Inspection note").count(), 1);
      assert.equal(await page.getByRole("status").count(), 1);
      assert.equal(await page.getByLabel("Theme").inputValue(), "auto");
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

      await page.emulateMedia({ colorScheme: "dark" });
      const theme = page.getByLabel("Theme");
      const darkBackground = await page.locator("body").evaluate((element) => getComputedStyle(element).backgroundColor);
      assert.equal(darkBackground, "rgb(20, 24, 29)", `${viewport.name}: Auto did not follow dark system preference`);
      await theme.selectOption("light");
      assert.equal(await page.locator("body").evaluate((element) => getComputedStyle(element).backgroundColor), "rgb(255, 255, 255)", `${viewport.name}: Light override did not apply`);
      await theme.selectOption("dark");
      assert.equal(await page.locator("body").evaluate((element) => getComputedStyle(element).backgroundColor), "rgb(20, 24, 29)", `${viewport.name}: Dark override did not apply`);
      await page.reload({ waitUntil: "load" });
      assert.equal(await page.getByLabel("Theme").inputValue(), "dark", `${viewport.name}: saved theme was not restored`);
      assert.equal(await page.evaluate(() => window.__themeBeforeStyles), "dark", `${viewport.name}: saved theme was not applied before the page stylesheet`);
      assert.equal(await page.locator("body").evaluate((element) => getComputedStyle(element).backgroundColor), "rgb(20, 24, 29)", `${viewport.name}: restored Dark theme did not apply`);
      const restoredTheme = page.getByLabel("Theme");
      await restoredTheme.selectOption("auto");
      await page.emulateMedia({ colorScheme: "light" });
      assert.equal(await page.locator("body").evaluate((element) => getComputedStyle(element).backgroundColor), "rgb(255, 255, 255)", `${viewport.name}: Auto did not follow light system preference`);

      await page.evaluate(() => {
        const state = document.getElementById("save-state");
        window.cyaShell.setSaveState(state, "unconfirmed");
        window.dispatchEvent(new Event("offline"));
      });
      assert.match(await page.getByRole("status").innerText(), /Unconfirmed\. It may or may not be recorded\./);
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
