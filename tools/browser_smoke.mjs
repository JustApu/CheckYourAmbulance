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
      // Half the CSS viewport approximates 200% browser zoom and checks reflow/overflow.
      await page.setViewportSize({ width: Math.floor(viewport.width / 2), height: viewport.height });
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
