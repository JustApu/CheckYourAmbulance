#!/usr/bin/env node
// Deterministic browser smoke against real shell/inspection CSS and synthetic markup.

import assert from "node:assert/strict";
import { mkdirSync, readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const cssPaths = [
  "cya/shell/templates/shell/shell.css",
  "cya/inspections/templates/inspections/inspection.css",
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
const html = `<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Synthetic vehicle inspection</title><script>${themeBootstrap}</script><script>window.__themeBeforeStyles = document.documentElement.getAttribute("data-theme");</script><style>${css}</style></head>
<body>
<a class="skip-link" href="#main">Skip to main content</a>
<header class="site-header"><a class="brand" href="#main">Check Your Ambulance</a><div class="header-tools"><div class="account"><span class="account-name">Sam Example</span><form><button class="button" type="button">Sign out</button></form></div><label class="theme-control" for="theme-select">Theme<select id="theme-select"><option value="auto">Auto</option><option value="light">Light</option><option value="dark">Dark</option></select></label></div></header>
<nav class="site-nav" aria-label="Main"><button id="site-nav-toggle" class="button site-nav-toggle" type="button" aria-controls="site-nav-list" aria-expanded="false" hidden>Menu</button><ul id="site-nav-list" class="site-nav-list"><li><a href="#main" aria-current="page">Inspection</a></li><li><a href="#status">Vehicle status</a></li><li><a href="#deficiency">Deficiencies</a></li></ul></nav>
<div id="connection-banner" class="banner banner-error" role="alert" hidden><span class="indicator-icon" aria-hidden="true">&#9888;</span><strong>Connection lost.</strong> Changes are not being saved. An item marked Unconfirmed may have been recorded.</div>
<main id="main" tabindex="-1"><h1>Unit 12 inspection</h1>
<section id="status" class="status-line" aria-label="Vehicle status"><span class="pill status-pill" data-status="IN_SERVICE">In service</span><span class="pill status-pill" data-status="OUT_OF_SERVICE">Out of service</span><span class="pill pill-attention">Open deficiency</span></section>
<ul class="vehicle-list"><li><a class="vehicle-link" href="#status"><span class="vehicle-name">Unit 12 — Synthetic Regional Response Ambulance</span><span class="vehicle-badges"><span class="pill pending-pill" data-count="2">2 pending requests</span><span class="pill status-pill" data-status="OUT_OF_SERVICE">Out of service</span></span></a></li></ul>
<section id="deficiency" class="item item-attention" aria-labelledby="item-title"><div class="item-head"><h2 id="item-title">Synthetic oxygen cylinder</h2><span class="pill pill-attention">Known deficiency</span></div><p>Pressure below the approved range.</p><div class="item-controls" aria-label="Inspection actions"><button class="button primary" type="button" aria-label="Record passed">Passed</button><button class="button" type="button" aria-label="Record unable to inspect">Unable to inspect</button></div><label class="panel">Inspection note<input aria-label="Inspection note" type="text" value="Synthetic check"></label><p id="save-state" class="save-state" role="status"><span class="indicator-icon" aria-hidden="true">?</span><span class="save-state-text">Unconfirmed</span></p></section>
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
function inspectionMarkup(userId, sessionId) {
  const config = { user_id: userId, session_id: sessionId, vehicle_id: "vehicle-a", checklist_version_id: "version-a", state: "OPEN", view: "NORMAL", needs_attention_only: false, urls: { checklist: "/api/checklist", checks: "/api/checks", odometer: "/api/odometer", preferences: "/api/preferences", reopen: "/api/reopen", vehicle: "/vehicle", socket: "/socket", suggestions: "/suggestions", deficiency_detail: "/deficiency/DEFICIENCY/" } };
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>${css}</style></head><body>
  <main><section><p id="op-status">In service</p><p id="op-restrictions"></p></section>
  <div id="live-banner" hidden><span id="live-detail"></span></div><div id="stale" hidden></div><p id="session-message" role="status"></p>
  <form id="csrf-holder"><input name="csrfmiddlewaretoken" value="synthetic"></form>
  <section class="panel"><h2>Odometer</h2><form id="odometer-form"><input id="odo-value"><select id="odo-unit"><option value="mi">miles</option></select><input id="odo-hours"><button type="submit">Save</button></form><div id="odo-confirm" hidden><textarea id="odo-why"></textarea><button id="odo-confirm-button" type="button">Confirm</button></div><p id="odometer-state" hidden><span class="indicator-icon"></span><span class="save-state-text"></span></p></section>
  <section><h2 id="checklist-heading" tabindex="-1">Checklist</h2><div class="view-controls"><button id="view-normal" class="button" type="button">Normal</button><button id="view-priority" class="button" type="button">Priority</button><label><input id="only-attention" type="checkbox"> Needs Attention Only</label></div><p id="pref-state" role="status"></p>
  <div id="layout-controls" class="view-controls" role="group" aria-label="Inspection layout"><button id="layout-compartments" class="button" type="button">Compartments</button><button id="layout-flat" class="button" type="button">All items</button></div>
  <section id="area-overview"><h3 id="area-overview-heading" tabindex="-1">Choose an area or compartment</h3><p class="muted">Progress counts checks for the current inspection window. Known deficiencies remain open until resolved.</p><div id="area-cards" class="area-cards"></div></section>
  <section id="area-detail" hidden><button id="area-overview-button" class="button" type="button">All areas and compartments</button><h3 id="area-heading" tabindex="-1"></h3></section><p id="screen-notice" hidden></p><ol id="items" class="items" aria-live="polite"></ol></section>
  <dialog id="conflict-dialog"><p id="conflict-text"></p><button id="conflict-cancel" type="button">Cancel</button><button id="conflict-proceed" type="button">Proceed</button></dialog>
  <dialog id="outcome-dialog"><h2 id="outcome-title"></h2><form id="outcome-form"><textarea name="outcome_explanation"></textarea><select id="outcome-existing"></select><p id="outcome-matches"></p><p id="outcome-error"></p><fieldset id="outcome-create-fields"></fieldset><button id="outcome-cancel" type="button">Cancel</button></form></dialog>
  </main><script id="cya-config" type="application/json">${JSON.stringify(config)}</script>
  <script>window.WebSocket = class { constructor() { setTimeout(() => { if (this.onopen) this.onopen(); }, 0); } close() {} };</script><script>${checklistScript}</script></body></html>`;
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
    await page.getByRole("checkbox", { name: "Needs Attention Only" }).check();
    assert.equal(await page.getByRole("heading", { name: "Radio", exact: true }).count(), 0, `${viewport.name}: suspended red row escaped the attention filter`);
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
const artifacts = process.env.BROWSER_SMOKE_OUTPUT_DIR || path.join(os.tmpdir(), `cya-browser-smoke-${process.pid}`);
const browser = await chromium.launch({ headless: true });
const failures = [];
async function waitForNavLayout(page, narrow) {
  await page.waitForFunction((isNarrow) => {
    const toggle = document.getElementById("site-nav-toggle");
    const list = document.getElementById("site-nav-list");
    return window.matchMedia("(max-width: 47.999rem)").matches === isNarrow
      && toggle.hidden === !isNarrow
      && list.hidden === (isNarrow && toggle.getAttribute("aria-expanded") !== "true");
  }, narrow);
}

try {
  for (const viewport of viewports) {
    const page = await browser.newPage({ viewport });
    try {
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
      const navToggle = page.getByRole("button", { name: "Menu" });
      const navList = page.locator("#site-nav-list");
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
      assert.equal(await page.getByRole("button", { name: "Record passed" }).count(), 1);
      assert.equal(await page.getByLabel("Inspection note").count(), 1);
      assert.equal(await page.getByRole("status").count(), 1);
      assert.equal(await page.getByLabel("Theme").inputValue(), "auto");
      assert.equal(await page.getByText("2 pending requests").count(), 1);
      assert.equal(await page.locator('.pending-pill[data-count="2"]').isVisible(), true);

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
