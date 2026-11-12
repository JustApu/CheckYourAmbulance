#!/usr/bin/env node
// Phone-width and US Letter print smoke for the paper outage checklist (issue #9 Part A).
// Uses the real print CSS with synthetic markup; Django-rendered structure is asserted in
// cya/inspections/tests/test_print.py.

import assert from "node:assert/strict";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const css = readFileSync(
  path.join(root, "cya/inspections/templates/inspections/vehicle_print.css"),
  "utf8",
);
const artifacts = process.env.BROWSER_SMOKE_OUTPUT_DIR ||
  path.join(os.tmpdir(), `cya-vehicle-print-smoke-${process.pid}`);
const versions = "Synthetic Daily Checklist, revision 3";

function itemRow(index) {
  const quantity = index % 3 === 0;
  return `<tr><th scope="row" class="item">Synthetic item ${index} with a longer descriptive label${quantity ? ' <span class="expected">Expected: 6 masks</span>' : ""}<span class="instructions">Synthetic instruction text for item ${index}.</span></th>
    <td class="outcomes" data-label="Result"><span class="tick"><span class="box" aria-hidden="true"></span>Pass</span><span class="tick"><span class="box" aria-hidden="true"></span>Fail</span><span class="tick"><span class="box" aria-hidden="true"></span>Unable</span><span class="tick"><span class="box" aria-hidden="true"></span>N/A</span></td>
    <td class="value" data-label="Count or value">${quantity ? '<span class="blank"></span> masks' : ""}</td>
    <td class="initials" data-label="Initials"><span class="blank short"></span></td>
    <td class="time" data-label="Time"><span class="blank short"></span></td></tr>`;
}

function section(heading, kit, count, start) {
  const rows = Array.from({ length: count }, (_, offset) => itemRow(start + offset)).join("");
  return `<section class="sheet-section${kit ? " kit-section" : ""}"><table><colgroup><col class="col-item"><col class="col-result"><col class="col-value"><col class="col-initials"><col class="col-time"></colgroup><thead>
    <tr class="repeat-identity"><th colspan="5">Synthetic Medic 6 · ${versions}</th></tr>
    <tr class="section-heading"><th colspan="5" scope="colgroup">${heading}</th></tr>
    <tr class="column-heads"><th scope="col">Item</th><th scope="col">Result</th><th scope="col">Count or value</th><th scope="col">Initials</th><th scope="col">Time</th></tr>
    </thead><tbody>${rows}</tbody></table></section>`;
}

const html = `<!doctype html><html lang="en"><head><meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1"><title>Synthetic Medic 6 paper checklist</title>
  <style>${css}</style></head><body>
  <div class="screen-only print-tools"><a href="/back">Back to Synthetic Medic 6</a><button type="button" class="print-button" id="print-sheet">Print</button><p>Prints on US Letter paper.</p></div>
  <main><header class="sheet-header"><h1>Synthetic Medic 6: paper inspection checklist</h1>
  <p class="sheet-meta">Checklist: ${versions}</p><p class="sheet-fill">Date <span class="blank wide"></span> Shift <span class="blank"></span></p>
  <p class="sheet-rule">Use only during an app outage. Keep this sheet until its results are entered into the app, then shred it.</p></header>
  ${section("Synthetic cab compartment", false, 40, 1)}${section("Kit: Synthetic trauma kit 1", true, 6, 41)}${section("Kit: Synthetic trauma kit 2", true, 6, 47)}
  </main></body></html>`;

// PDF page objects are "/Type /Page" (the page-tree node is "/Type /Pages").
function pageCount(pdf) {
  return (pdf.toString("latin1").match(/\/Type\s*\/Page(?![s\w])/g) || []).length;
}

const browser = await chromium.launch({ headless: true });
const failures = [];
try {
  for (const viewport of [{ name: "phone-375", width: 375, height: 812 }, { name: "desktop-1440", width: 1440, height: 900 }]) {
    const page = await browser.newPage({ viewport });
    try {
      await page.setContent(html, { waitUntil: "load" });
      const screen = await page.evaluate(() => ({
        documentWidth: document.documentElement.scrollWidth,
        printButton: document.getElementById("print-sheet").getBoundingClientRect().height,
        identityShown: getComputedStyle(document.querySelector(".repeat-identity")).display !== "none",
      }));
      assert.ok(screen.documentWidth <= viewport.width, `${viewport.name}: horizontal overflow ${JSON.stringify(screen)}`);
      assert.ok(screen.printButton >= 48, `${viewport.name}: print button below 48px`);
      assert.equal(screen.identityShown, false, `${viewport.name}: repeated print header shown on screen`);
      console.log(`${viewport.name}: screen layout passed`);
    } catch (error) {
      failures.push(`${viewport.name}: ${error.message}`);
    } finally {
      await page.close();
    }
  }

  const page = await browser.newPage({ viewport: { width: 816, height: 1056 } });
  try {
    await page.setContent(html, { waitUntil: "load" });
    await page.emulateMedia({ media: "print" });
    const printed = await page.evaluate(() => ({
      toolsHidden: getComputedStyle(document.querySelector(".print-tools")).display === "none",
      identityShown: getComputedStyle(document.querySelector(".repeat-identity")).display !== "none",
      kitBreak: getComputedStyle(document.querySelector(".kit-section")).breakBefore,
      headerGroup: getComputedStyle(document.querySelector("thead")).display,
    }));
    assert.equal(printed.toolsHidden, true, "print: screen tools are printed");
    assert.equal(printed.identityShown, true, "print: vehicle and version header missing");
    assert.equal(printed.kitBreak, "page", "print: a kit does not start on a new page");
    assert.equal(printed.headerGroup, "table-header-group", "print: header rows will not repeat");
    const pdf = await page.pdf({ preferCSSPageSize: true });
    const pages = pageCount(pdf);
    // 40 cab rows overflow one Letter page, and each of the two kits starts its own page.
    assert.ok(pages >= 4, `print: expected at least 4 Letter pages, got ${pages}`);
    if (process.env.BROWSER_SMOKE_OUTPUT_DIR) {
      mkdirSync(artifacts, { recursive: true });
      writeFileSync(path.join(artifacts, "vehicle-print-letter.pdf"), pdf);
    }
    console.log(`print: ${pages} US Letter pages, repeated header, kit page breaks passed`);
  } catch (error) {
    failures.push(`print: ${error.message}`);
  } finally {
    await page.close();
  }
} finally {
  await browser.close();
}

if (failures.length) {
  console.error(failures.join("\n"));
  process.exitCode = 1;
} else {
  console.log("Vehicle print smoke passed at phone and desktop widths and on US Letter.");
}
