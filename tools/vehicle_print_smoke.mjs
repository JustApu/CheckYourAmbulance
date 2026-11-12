#!/usr/bin/env node
// Phone-width and US Letter print smoke for the paper outage checklist and the kit reference
// (issue #9 Part A). Uses the real print CSS with synthetic markup; Django-rendered structure is
// asserted in cya/inspections/tests/test_print.py.

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
const cols = '<colgroup><col class="col-item"><col class="col-result"><col class="col-value"><col class="col-initials"><col class="col-time"></colgroup>';
const tick = (label) => `<span class="tick"><span class="box" aria-hidden="true"></span>${label}</span>`;

function head(heading, columns) {
  return `<thead><tr class="repeat-identity"><th colspan="5">Synthetic Medic 6 · ${versions} <span class="version-id">synthetic-version-id</span></th></tr>
    <tr class="section-heading"><th colspan="5" scope="colgroup">${heading}</th></tr>
    <tr class="column-heads">${columns.map((c) => `<th scope="col">${c}</th>`).join("")}</tr></thead>`;
}

function itemRow(index) {
  const quantity = index % 3 === 0;
  return `<tr><th scope="row" class="item">Synthetic item ${index} with a longer descriptive label${quantity ? ' <span class="expected">Expected: 6 masks</span>' : ""}<span class="instructions">Synthetic instruction text for item ${index}.</span></th>
    <td class="outcomes" data-label="Result">${["Pass", "Fail", "Unable", "N/A"].map(tick).join("")}</td>
    <td class="value" data-label="Count or value">${quantity ? '<span class="blank"></span> masks' : ""}</td>
    <td class="initials" data-label="Initials"><span class="blank short"></span></td>
    <td class="time" data-label="Time"><span class="blank short"></span></td></tr>`;
}

const sealCells = (contents) => `<td class="outcomes" data-label="Seal or contents">${tick("Seal intact")}<span class="tick wrap"><span class="box" aria-hidden="true"></span>No seal or broken: ${contents}</span><span class="exceptions">Exceptions <span class="blank wide"></span></span></td>
    <td class="value" data-label="Written on seal"><span class="seal-field">Crew ID <span class="blank short"></span></span><span class="seal-field">Earliest exp. <span class="blank short"></span></span></td>`;

// Kits 1 and 2 are designated sealed; kit 3 is not, so it is checked by its contents (issue #131).
function kitRow(index) {
  const cells = index < 3 ? sealCells("contents checked") : `<td class="outcomes" data-label="Contents"><span class="tick wrap"><span class="box" aria-hidden="true"></span>Contents checked against the kit reference</span><span class="exceptions">Exceptions <span class="blank wide"></span></span></td>
    <td class="value" data-label="Written on seal">Not a sealed kit</td>`;
  return `<tr${index < 3 ? ' class="sealed-kit"' : ""}><th scope="row" class="item">Synthetic trauma kit ${index}<span class="instructions">Synthetic cabinet ${index}</span></th>
    ${cells}
    <td class="initials" data-label="Initials"><span class="blank short"></span></td>
    <td class="time" data-label="Time"><span class="blank short"></span></td></tr>`;
}

function page(title, body, pageHeader = "") {
  const running = pageHeader ? `@page { @top-left { content: "${pageHeader}"; font-size: 8pt; } }` : "";
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1"><title>${title}</title>
    <style>${css}${running}</style></head><body>
    <div class="screen-only print-tools"><a href="/back">Back</a><button type="button" class="print-button" id="print-sheet">Print</button><p>Prints on US Letter paper.</p></div>
    <main>${body}</main></body></html>`;
}

const items = Array.from({ length: 40 }, (_, offset) => itemRow(offset + 1)).join("");
const kits = [1, 2, 3].map(kitRow).join("");
const outageHeader = `<header class="sheet-header"><h1>Synthetic Medic 6: paper inspection checklist</h1>
  <p class="sheet-meta">Checklist: ${versions}</p><p class="sheet-fill">Date <span class="blank wide"></span> Shift <span class="blank"></span></p>
  <p class="sheet-rule">Use only during an app outage. Keep this sheet until its results are entered into the app, then shred it.</p></header>
  <section class="sheet-section"><table>${cols}${head("Synthetic cab compartment", ["Item", "Result", "Count or value", "Initials", "Time"])}<tbody>${items}</tbody></table></section>
  <section class="sheet-section"><table>${cols}${head("Synthetic sealed drug cabinet", ["Item", "Result", "Count or value", "Initials", "Time"])}<tbody>
  <tr class="sealed-compartment"><th scope="row" class="item">Sealed cabinet<span class="instructions">An intact seal covers every item and kit inside. If the seal is broken or missing, check all contents and reseal.</span></th>
    ${sealCells("all contents checked")}
    <td class="initials" data-label="Initials"><span class="blank short"></span></td>
    <td class="time" data-label="Time"><span class="blank short"></span></td></tr></tbody></table></section>`;
const kitSection = `<section class="sheet-section kits"><table>${cols}${head("Kits: confirm the seal, or check the contents against the kit reference", ["Kit", "Seal or contents", "Written on seal", "Initials", "Time"])}<tbody>${kits}</tbody></table></section>`;
const outage = page("Synthetic Medic 6 paper checklist", outageHeader + kitSection);
// The same sheet without kits: page counts depend on the runner's fonts, so the kit check compares
// against this baseline instead of a fixed number of pages.
const outageWithoutKits = page("Synthetic Medic 6 paper checklist", outageHeader);

const kitTypeBlocks = (count) => Array.from({ length: count }, (_, kit) => `<section class="kit-type"><h3>Synthetic kit type ${kit + 1}</h3><ul>${
  Array.from({ length: 8 }, (_, item) => `<li>Synthetic supply ${item + 1}, size ${kit + 1} (${item + 1})</li>`).join("")
}</ul></section>`).join("");
const referenceHeader = `Kit reference: ${versions} (synthetic-version-id)`;
const referenceOf = (count) => page("Kit reference", `<header class="sheet-header"><h1>Kit reference: what a complete kit contains</h1></header>
  <section class="kit-reference"><h2>${versions} <span class="version-id">synthetic-version-id</span></h2><div class="kit-columns">${kitTypeBlocks(count)}</div></section>`, referenceHeader);
const reference = referenceOf(12);
// A long reference continues on further pages; each carries the running header (checked by
// rendering the kept PDF; Playwright cannot read @page margin boxes).
const longReference = referenceOf(40);

// PDF page objects are "/Type /Page" (the page-tree node is "/Type /Pages").
function pageCount(pdf) {
  return (pdf.toString("latin1").match(/\/Type\s*\/Page(?![s\w])/g) || []).length;
}

async function checkScreen(browser, name, html, viewport) {
  const tab = await browser.newPage({ viewport });
  try {
    await tab.setContent(html, { waitUntil: "load" });
    const screen = await tab.evaluate(() => ({
      documentWidth: document.documentElement.scrollWidth,
      printButton: document.getElementById("print-sheet").getBoundingClientRect().height,
    }));
    assert.ok(screen.documentWidth <= viewport.width, `${name} ${viewport.width}: horizontal overflow ${JSON.stringify(screen)}`);
    assert.ok(screen.printButton >= 48, `${name} ${viewport.width}: print button below 48px`);
  } finally {
    await tab.close();
  }
}

async function printPdf(browser, html, probe) {
  const tab = await browser.newPage({ viewport: { width: 816, height: 1056 } });
  try {
    await tab.setContent(html, { waitUntil: "load" });
    await tab.emulateMedia({ media: "print" });
    const printed = await tab.evaluate(probe);
    return { printed, pdf: await tab.pdf({ preferCSSPageSize: true }) };
  } finally {
    await tab.close();
  }
}

function keep(name, pdf) {
  if (!process.env.BROWSER_SMOKE_OUTPUT_DIR) { return; }
  mkdirSync(artifacts, { recursive: true });
  writeFileSync(path.join(artifacts, name), pdf);
}

const browser = await chromium.launch({ headless: true });
const failures = [];
try {
  for (const viewport of [{ width: 375, height: 812 }, { width: 1440, height: 900 }]) {
    for (const [name, html] of [["outage sheet", outage], ["kit reference", reference]]) {
      try {
        await checkScreen(browser, name, html, viewport);
        console.log(`${name} ${viewport.width}: screen layout passed`);
      } catch (error) {
        failures.push(error.message);
      }
    }
  }
  try {
    const { printed, pdf } = await printPdf(browser, outage, () => ({
      toolsHidden: getComputedStyle(document.querySelector(".print-tools")).display === "none",
      identityShown: getComputedStyle(document.querySelector(".repeat-identity")).display !== "none",
      kitBreak: getComputedStyle(document.querySelector(".kits")).breakBefore,
      headerGroup: getComputedStyle(document.querySelector("thead")).display,
    }));
    assert.equal(printed.toolsHidden, true, "outage print: screen tools are printed");
    assert.equal(printed.identityShown, true, "outage print: vehicle and version header missing");
    assert.equal(printed.headerGroup, "table-header-group", "outage print: header rows will not repeat");
    assert.equal(printed.kitBreak, "auto", "outage print: kits force a new page and waste paper");
    const pages = pageCount(pdf);
    const baseline = pageCount((await printPdf(browser, outageWithoutKits, () => null)).pdf);
    assert.ok(baseline >= 2, `outage print: 40 items should continue onto a second Letter page, got ${baseline}`);
    // Three compact kit rows fit within one more page; a page break per kit would add three.
    assert.ok(pages <= baseline + 1, `outage print: 3 kit rows added ${pages - baseline} pages to ${baseline}`);
    keep("outage-letter.pdf", pdf);
    console.log(`outage print: ${pages} US Letter pages (${baseline} without kits), repeated header, kits without page breaks passed`);
  } catch (error) {
    failures.push(`outage print: ${error.message}`);
  }
  try {
    const { printed, pdf } = await printPdf(browser, reference, () => ({
      columns: getComputedStyle(document.querySelector(".kit-columns")).columnCount,
      keepTogether: getComputedStyle(document.querySelector(".kit-type")).breakInside,
    }));
    assert.equal(printed.columns, "3", "reference print: not three columns");
    assert.equal(printed.keepTogether, "avoid", "reference print: a kit may split across columns");
    const pages = pageCount(pdf);
    assert.equal(pages, 1, `reference print: 12 kits of 8 items should fit one Letter page, got ${pages}`);
    keep("kit-reference-letter.pdf", pdf);
    const long = await printPdf(browser, longReference, () => null);
    const longPages = pageCount(long.pdf);
    assert.ok(longPages >= 2, `reference print: 40 kit types should continue onto a second page, got ${longPages}`);
    keep("kit-reference-long-letter.pdf", long.pdf);
    console.log(`reference print: 12 kit types on one three-column US Letter page, 40 on ${longPages} pages passed`);
  } catch (error) {
    failures.push(`reference print: ${error.message}`);
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
