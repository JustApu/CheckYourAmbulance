import { chmod, mkdir } from "node:fs/promises";
import path from "node:path";

const evidenceDirectory = process.env.CYA_BROWSER_EVIDENCE_DIR;

export async function captureBrowserEvidence(page, name) {
  if (!evidenceDirectory) return;
  if (!path.isAbsolute(evidenceDirectory)) {
    throw new Error("CYA_BROWSER_EVIDENCE_DIR must be an absolute private directory");
  }
  if (!/^[a-z0-9][a-z0-9-]*\.png$/i.test(name)) {
    throw new Error("Browser evidence filename is invalid");
  }
  await mkdir(evidenceDirectory, { recursive: true, mode: 0o700 });
  await chmod(evidenceDirectory, 0o700);
  const target = path.join(evidenceDirectory, name);
  await page.screenshot({ path: target, fullPage: true });
  await chmod(target, 0o600);
}
