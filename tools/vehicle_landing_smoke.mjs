#!/usr/bin/env node
// Responsive smoke for the inspection landing page, using synthetic sessions and the real client.
// Django-rendered structure and class hooks are asserted in test_crew_ui.py.

import assert from "node:assert/strict";
import { mkdirSync, readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { captureBrowserEvidence } from "./capture_browser_evidence.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const css = [
  "cya/shell/templates/shell/shell.css",
  "cya/inspections/templates/inspections/inspection.css",
].map((file) => readFileSync(path.join(root, file), "utf8")).join("\n");
const script = readFileSync(
  path.join(root, "cya/inspections/templates/inspections/vehicle.js"),
  "utf8",
);
const viewports = [
  { name: "phone-375", width: 375, height: 812 },
  { name: "large-phone-600", width: 600, height: 900 },
  { name: "tablet-portrait-768", width: 768, height: 1024 },
  { name: "tablet-landscape-1024", width: 1024, height: 768 },
  { name: "desktop-1440", width: 1440, height: 900 },
];
const artifacts = process.env.BROWSER_SMOKE_OUTPUT_DIR ||
  path.join(os.tmpdir(), `cya-vehicle-landing-smoke-${process.pid}`);

function markup({ active = false, participant = false, incomplete = false } = {}) {
  const action = active
    ? participant
      ? '<a class="button primary" href="/inspection/sessions/current-session/">Continue Night Shift Vehicle Safety Checklist inspection</a>'
      : '<button type="button" class="button primary join-session" data-template="template-night">Join Night Shift Vehicle Safety Checklist inspection</button>'
    : '<button type="button" class="button primary start-session" data-template="template-night">Start Night Shift Vehicle Safety Checklist inspection</button>';
  const openSummary = active
    ? '<p><strong>Open inspection:</strong> Night Shift Vehicle Safety Checklist · started <time datetime="2026-11-12T20:00:00Z">2026-11-12 15:00 EST (-0500)</time></p>'
    : "";
  const primary = active
    ? `<div class="active-inspection">${openSummary}${action}</div>`
    : incomplete
      ? `<div class="active-inspection"><p><strong>Incomplete inspection:</strong> Night Shift Vehicle Safety Checklist · opened <time datetime="2026-11-11T20:00:00Z">2026-11-11 15:00 EST (-0500)</time></p><a class="button primary" href="/inspection/sessions/incomplete-session/">Resume incomplete Night Shift Vehicle Safety Checklist inspection</a></div><details class="separate-inspection"><summary>Start a new inspection</summary><p>This starts a new inspection. Your incomplete inspection stays available in the list below.</p><ul class="plain inspection-options"><li>${action}</li></ul></details>`
      : action;
  const separate = active
    ? `<details class="separate-inspection"><summary>Start a separate inspection</summary>
      <p>Use this when another workstream needs its own checklist. It creates a second inspection instead of joining the open one.</p>
      <ul class="plain inspection-options"><li><button type="button" class="button start-separate" data-template="template-night" data-template-label="Night Shift Vehicle Safety Checklist">Start separate Night Shift Vehicle Safety Checklist inspection</button></li></ul></details>`
    : "";
  const sessionRow = incomplete
    ? '<li class="session-row"><span class="session-row-status"><strong>Night Shift Vehicle Safety Checklist</strong> · Incomplete · opened <time datetime="2026-11-11T20:00:00Z">2026-11-11 15:00 EST (-0500)</time></span></li>'
    : active
    ? `<li class="session-row"><span class="session-row-status"><strong>Night Shift Vehicle Safety Checklist</strong> · Open · opened <time datetime="2026-11-12T20:00:00Z">2026-11-12 15:00 EST (-0500)</time></span>${participant ? '' : '<span class="muted">Join using the primary action above.</span>'}</li>`
    : "<li>No inspection sessions yet.</li>";
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Medic 6 — North Crew - Check Your Ambulance</title><style>${css}</style></head><body>
    <main id="main"><div class="vehicle-page-heading"><h1>Medic 6 — North Crew</h1>
      <p class="status-line"><span class="visually-hidden">Vehicle operational status: </span><span class="pill status-pill" data-status="IN_SERVICE">In Service</span></p></div>
    <nav class="vehicle-actions" aria-label="Vehicle tasks"><a href="/history">Inspection history</a><a href="/report">Report a deficiency</a></nav>
    <p id="vehicle-message" class="notice" role="status" hidden></p><form id="csrf-holder"><input name="csrfmiddlewaretoken" value="synthetic"></form>
    <script id="cya-config" type="application/json">{"vehicle_id":"vehicle-synthetic","sessions_url":"/api/v1/inspection-sessions"}</script>
      <section class="panel inspection-start" aria-labelledby="start-heading"><h2 id="start-heading">Inspection</h2>
      ${!active ? '<p>Start the configured inspection when you are ready. Other crew members can join it.</p>' : ''}${primary}${separate}</section>
    <section aria-labelledby="sessions-heading"><h2 id="sessions-heading">Active and recent inspections</h2>
      <ul class="plain">${sessionRow}</ul></section></main><script>${script}</script></body></html>`;
}

async function openPage(browser, viewport, options) {
  const page = await browser.newPage({
    viewport: { width: viewport.width, height: viewport.height },
  });
  const requests = [];
  await page.route("http://vehicle.test/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/v1/inspection-sessions") {
      const body = JSON.parse(route.request().postData() || "{}");
      requests.push(body);
      const sessionId = body.separate ? "separate-session" : "joined-session";
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "COMMITTED", result: { session_id: sessionId } }),
      });
      return;
    }
    if (url.pathname.startsWith("/inspection/sessions/")) {
      await route.fulfill({ status: 200, contentType: "text/html", body: "<title>Session</title>" });
      return;
    }
    await route.fulfill({ status: 200, contentType: "text/html", body: markup(options) });
  });
  await page.goto("http://vehicle.test/inspection/vehicles/vehicle-synthetic/", { waitUntil: "load" });
  return { page, requests };
}

async function checkLayout(page, viewport) {
  assert.equal(await page.getByRole("heading", { level: 1, name: "Medic 6 — North Crew" }).count(), 1);
  assert.equal(await page.locator(".status-line .visually-hidden").textContent(), "Vehicle operational status: ");
  assert.equal(await page.getByRole("link", { name: "Inspection history" }).count(), 1);
  assert.equal(await page.getByRole("link", { name: "Report a deficiency" }).count(), 1);
  const metrics = await page.evaluate(() => {
    const heading = document.querySelector(".vehicle-page-heading");
    const title = heading.querySelector("h1").getBoundingClientRect();
    const status = heading.querySelector(".status-line").getBoundingClientRect();
    const targets = [...document.querySelectorAll(".button, .vehicle-actions a, summary")]
      .map((element) => element.getBoundingClientRect().height);
    return {
      scrollWidth: document.documentElement.scrollWidth,
      bodyWidth: document.body.scrollWidth,
      viewport: innerWidth,
      headingRight: heading.getBoundingClientRect().right,
      titleRight: title.right,
      statusRight: status.right,
      statusTop: status.top,
      titleTop: title.top,
      targets,
    };
  });
  assert.ok(metrics.scrollWidth <= viewport.width, `${viewport.name}: horizontal overflow ${JSON.stringify(metrics)}`);
  assert.ok(metrics.bodyWidth <= viewport.width, `${viewport.name}: body overflow ${JSON.stringify(metrics)}`);
  assert.ok(metrics.titleRight <= metrics.headingRight + 1, `${viewport.name}: vehicle name overflowed the heading row`);
  assert.ok(metrics.statusRight <= metrics.headingRight + 1, `${viewport.name}: status overflowed the heading row`);
  if (viewport.width >= 600) {
    assert.ok(Math.abs(metrics.titleTop - metrics.statusTop) <= 4, `${viewport.name}: vehicle name and status did not share the heading row`);
  }
  assert.ok(metrics.targets.every((height) => height >= 48), `${viewport.name}: a task target is below 48px ${JSON.stringify(metrics.targets)}`);
  await page.setViewportSize({ width: Math.max(320, Math.floor(viewport.width / 2)), height: viewport.height });
  const zoom = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth }));
  assert.ok(zoom.document <= zoom.viewport, `${viewport.name} at 200%: document overflow ${JSON.stringify(zoom)}`);
  assert.ok(zoom.body <= zoom.viewport, `${viewport.name} at 200%: body overflow ${JSON.stringify(zoom)}`);
  await page.setViewportSize({ width: viewport.width, height: viewport.height });
}

const browser = await chromium.launch({ headless: true });
const failures = [];
try {
  for (const viewport of viewports) {
    let page;
    try {
      const fresh = await openPage(browser, viewport, { active: false });
      page = fresh.page;
      await checkLayout(page, viewport);
      const start = page.getByRole("button", { name: "Start Night Shift Vehicle Safety Checklist inspection" });
      assert.equal(await start.count(), 1, `${viewport.name}: configured template action is unclear`);
      assert.equal(await page.getByRole("button", { name: /Start a separate/ }).count(), 0, `${viewport.name}: separate action shown with no open session`);
      await page.close();

      const joined = await openPage(browser, viewport, { active: true });
      page = joined.page;
      await checkLayout(page, viewport);
      assert.match(await page.locator(".active-inspection").innerText(), /Night Shift Vehicle Safety Checklist/);
      await captureBrowserEvidence(page, `vehicle-entry-${viewport.name}.png`);
      assert.equal(await page.getByRole("button", { name: "Join Night Shift Vehicle Safety Checklist inspection" }).count(), 1);
      const join = page.getByRole("button", { name: "Join Night Shift Vehicle Safety Checklist inspection" });
      await join.focus();
      await page.keyboard.press("Enter");
      await page.waitForURL("**/inspection/sessions/joined-session/");
      assert.equal(joined.requests[0].separate, false, `${viewport.name}: join made a separate session`);
      assert.ok(joined.requests[0].operation_id, `${viewport.name}: join has no operation id`);
      await page.close();

      const participant = await openPage(browser, viewport, { active: true, participant: true });
      page = participant.page;
      await checkLayout(page, viewport);
      assert.equal(await page.getByRole("link", { name: "Continue Night Shift Vehicle Safety Checklist inspection" }).count(), 1);
      assert.equal(await page.getByRole("button", { name: "Join Night Shift Vehicle Safety Checklist inspection" }).count(), 0);
      const summary = page.getByText("Start a separate inspection", { exact: true });
      await summary.click();
      const separate = page.getByRole("button", { name: "Start separate Night Shift Vehicle Safety Checklist inspection" });
      await separate.focus();
      let prompt = "";
      page.once("dialog", async (dialog) => {
        prompt = dialog.message();
        await dialog.dismiss();
      });
      await page.keyboard.press("Enter");
      assert.match(prompt, /Night Shift Vehicle Safety Checklist/);
      assert.match(prompt, /instead of joining the open one/);
      assert.equal(participant.requests.length, 0, `${viewport.name}: cancel created a separate session`);
      page.once("dialog", async (dialog) => { await dialog.accept(); });
      await separate.click();
      await page.waitForURL("**/inspection/sessions/separate-session/");
      assert.equal(participant.requests[0].separate, true, `${viewport.name}: confirmed separate action joined instead`);
      await page.close();

      const incomplete = await openPage(browser, viewport, { incomplete: true });
      page = incomplete.page;
      await checkLayout(page, viewport);
      assert.equal(await page.getByRole("link", { name: "Resume incomplete Night Shift Vehicle Safety Checklist inspection" }).count(), 1);
      await page.getByText("Start a new inspection", { exact: true }).click();
      const startNew = page.getByRole("button", { name: "Start Night Shift Vehicle Safety Checklist inspection" });
      assert.equal(await startNew.count(), 1);
      assert.equal(await startNew.getAttribute("class").then((value) => value.split(/\s+/).includes("start-session")), true);
      assert.equal(await startNew.getAttribute("class").then((value) => value.split(/\s+/).includes("start-separate")), false);
      assert.equal(await page.getByRole("link", { name: "Resume incomplete inspection" }).count(), 0, `${viewport.name}: incomplete resume action is duplicated`);
      await captureBrowserEvidence(page, `vehicle-resume-${viewport.name}.png`);
      console.log(`${viewport.name}: landing actions, session choice, target size and layout passed`);
    } catch (error) {
      mkdirSync(artifacts, { recursive: true });
      const screenshot = path.join(artifacts, `vehicle-${viewport.name}.png`);
      if (page) await page.screenshot({ path: screenshot, fullPage: true }).catch(() => undefined);
      failures.push(`${viewport.name}: ${error.message}; screenshot: ${screenshot}`);
    } finally {
      if (page && !page.isClosed()) await page.close();
    }
  }
} finally {
  await browser.close();
}

if (failures.length) {
  console.error(failures.join("\n"));
  process.exitCode = 1;
} else {
  console.log(`Vehicle landing smoke passed at ${viewports.map(({ width }) => width).join(", ")} CSS-pixel widths.`);
}
