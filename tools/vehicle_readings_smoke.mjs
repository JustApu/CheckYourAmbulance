#!/usr/bin/env node
// Responsive smoke for compact vehicle readings with a vehicle's established distance unit.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { captureBrowserEvidence } from "./capture_browser_evidence.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const css = [
  "cya/shell/templates/shell/shell.css",
  "cya/inspections/templates/inspections/inspection.css",
].map((file) => readFileSync(path.join(root, file), "utf8")).join("\n");
const viewports = [
  { name: "phone-375", width: 375, height: 812 },
  { name: "large-phone-600", width: 600, height: 900 },
  { name: "tablet-portrait-768", width: 768, height: 1024 },
  { name: "tablet-landscape-1024", width: 1024, height: 768 },
  { name: "desktop-1440", width: 1440, height: 900 },
];
const html = `<!doctype html><html lang="en"><head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <style>${css}</style></head><body><main>
  <section aria-labelledby="odo-heading" class="panel vehicle-readings">
    <h2 id="odo-heading">Vehicle readings</h2>
    <form id="odometer-form" novalidate><div class="vehicle-readings-fields">
      <label class="vehicle-reading-field" for="odo-value">
        <span>Mileage <span class="muted">(kilometres)</span></span>
        <input id="odo-value" class="number-input" type="text" inputmode="decimal" autocomplete="off" required>
      </label>
      <input id="odo-unit" type="hidden" value="km">
      <label class="vehicle-reading-field" for="odo-hours">
        <span>Engine hours (optional)</span>
        <input id="odo-hours" class="number-input" type="text" inputmode="decimal" autocomplete="off">
      </label>
      <button type="submit" class="button primary">Record readings</button>
    </div></form>
    <p class="muted readings-save-note">Audited history: select Record after entering complete values.</p>
  </section></main></body></html>`;

const browser = await chromium.launch({ headless: true });
try {
  for (const viewport of viewports) {
    const page = await browser.newPage({ viewport });
    try {
      await page.setContent(html, { waitUntil: "load" });
      assert.equal(await page.getByRole("heading", { name: "Vehicle readings" }).count(), 1);
      assert.equal(await page.getByLabel("Mileage (kilometres)").getAttribute("inputmode"), "decimal");
      assert.equal(await page.getByLabel("Engine hours (optional)").getAttribute("inputmode"), "decimal");
      assert.equal(await page.locator("#odo-unit").inputValue(), "km");
      assert.equal(await page.locator("select").count(), 0, `${viewport.name}: established unit still requires selection`);
      const metrics = await page.evaluate(() => ({
        documentWidth: document.documentElement.scrollWidth,
        cardHeight: document.querySelector(".vehicle-readings").getBoundingClientRect().height,
        saveHeight: document.querySelector("#odometer-form button").getBoundingClientRect().height,
      }));
      assert.ok(metrics.documentWidth <= viewport.width, `${viewport.name}: horizontal overflow ${JSON.stringify(metrics)}`);
      assert.ok(metrics.cardHeight <= 210, `${viewport.name}: readings card is not compact ${JSON.stringify(metrics)}`);
      assert.ok(metrics.saveHeight >= 48, `${viewport.name}: save target is too small ${JSON.stringify(metrics)}`);
      await captureBrowserEvidence(page, `vehicle-readings-${viewport.name}.png`);
      console.log(`${viewport.name}: compact readings layout passed`);
    } finally {
      await page.close();
    }
  }
} finally {
  await browser.close();
}
