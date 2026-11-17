// Render the #194 icon SVGs to PNG at exact sizes (local build step; outputs are committed).
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
const here = path.dirname(fileURLToPath(import.meta.url));
const jobs = [
  ["icon.svg", "icon-192.png", 192, true],
  ["icon.svg", "icon-512.png", 512, true],
  ["maskable.svg", "maskable-512.png", 512, false],
  ["maskable.svg", "apple-touch-icon.png", 180, false],
  ["favicon.svg", "favicon-32.png", 32, true],
];
const browser = await chromium.launch({ channel: "msedge" });
for (const [src, out, size, transparent] of jobs) {
  const svg = readFileSync(path.join(here, src), "utf8");
  const page = await browser.newPage({ viewport: { width: size, height: size }, deviceScaleFactor: 1 });
  await page.setContent(`<!doctype html><html><body style="margin:0;background:transparent">${svg.replace("<svg ", `<svg width="${size}" height="${size}" `)}</body></html>`);
  await page.screenshot({ path: path.join(here, out), omitBackground: transparent, clip: { x: 0, y: 0, width: size, height: size } });
  await page.close();
  console.log(out, size);
}
await browser.close();
