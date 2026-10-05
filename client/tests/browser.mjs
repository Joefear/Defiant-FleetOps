/** Real browser acceptance against the live production API and disposable PostgreSQL.
 * Inputs arrive over stdin so credentials never appear in command arguments or logs.
 */
import { chromium, expect } from "@playwright/test";
let input = "";
for await (const chunk of process.stdin) input += chunk;
const data = JSON.parse(input);
const browser = await chromium.launch({
  executablePath: data.browser,
  headless: true,
  args: [
    "--use-fake-device-for-media-stream",
    "--use-fake-ui-for-media-stream",
    "--use-file-for-fake-video-capture=" + data.video,
  ],
});
const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
const page = await context.newPage();
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
async function scan(label, value) {
  await page.getByRole("textbox", { name: label, exact: true }).fill(value);
  await page.getByRole("textbox", { name: label, exact: true }).press("Enter");
}
async function queue() {
  return page.evaluate(async () => {
    const db = await new Promise((resolve, reject) => {
      const req = indexedDB.open("fleetops-capture-v1");
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    const value = await new Promise((resolve) => {
      const req = db.transaction("queue").objectStore("queue").getAll();
      req.onsuccess = () => resolve(req.result);
    });
    db.close();
    return value
      .sort((a, b) => a.ordinal - b.ordinal)
      .map((row) => ({
        id: row.id,
        ordinal: row.ordinal,
        status: row.status,
        envelope: row.envelope,
        response: row.response,
        evidence_done: row.evidence_done,
        evidence_error: row.evidence_error,
        photo: !!row.photo,
        evidence_operation: row.evidence_operation,
      }));
  });
}
async function allApplied() {
  await expect
    .poll(
      async () =>
        (await queue()).every(
          (row) => row.status === "APPLIED" && (!row.photo || row.evidence_done),
        ),
      { timeout: 60_000 },
    )
    .toBe(true);
}
async function clickTab(name) {
  await page
    .getByRole("button", { name: new RegExp("^\\d*\\s*" + name + "(?:\\s*\\d+)?$") })
    .click();
}
try {
  // Exercise the actual startup/sign-in UI when the browser denies its storage
  // property, including recovery cleanup. A separate tab keeps the main offline
  // replay scenario's session-storage and reload behavior unmodified.
  const deniedPage = await context.newPage();
  const deniedErrors = [];
  deniedPage.on("pageerror", (error) => deniedErrors.push(error.message));
  try {
    await deniedPage.addInitScript(() => {
      Object.defineProperty(window, "sessionStorage", {
        configurable: true,
        get() {
          throw new DOMException("Browser storage blocked", "SecurityError");
        },
      });
    });
    await deniedPage.goto(data.url);
    await expect(deniedPage.getByRole("button", { name: "Sign in", exact: true })).toBeVisible();
    await deniedPage.getByRole("textbox", { name: "Username", exact: true }).fill(data.username);
    await deniedPage.getByRole("textbox", { name: "Password", exact: true }).fill(data.password);
    await deniedPage.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(
      deniedPage.getByText(
        "Browser storage is unavailable. Allow storage for this site, then sign in again.",
        { exact: true },
      ),
    ).toBeVisible();
    await expect(deniedPage.getByText("Signed in as", { exact: false })).toHaveCount(0);
    expect(deniedErrors).toEqual([]);
  } finally {
    await deniedPage.close();
  }
  await page.goto(data.url);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole("textbox", { name: "Username", exact: true }).fill(data.username);
  await page.getByRole("textbox", { name: "Password", exact: true }).fill(data.password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByText("Signed in as", { exact: false })).toBeVisible();
  await expect(page.getByText("✓ Offline shell ready", { exact: true })).toBeVisible({
    timeout: 30_000,
  });
  // Only raw UUIDs may resolve. A URL barcode must fail before a network lookup.
  await scan("PO or receipt label", "https://example.invalid/" + data.order);
  await expect(page.getByText("This label must contain only a FleetOps UUID")).toBeVisible();
  const crossOrigin = await context.request.post(data.url + "/api/auth/login", {
    headers: { Origin: "https://other.invalid" },
    data: {},
  });
  expect(crossOrigin.status()).toBe(403);
  await page.getByRole("button", { name: "Use camera", exact: true }).click();
  await expect(
    page.getByRole("textbox", { name: "Receiving dock label", exact: true }),
  ).toBeVisible({ timeout: 30_000 });
  await scan("Receiving dock label", data.location);
  await page.getByRole("button", { name: "Open receipt", exact: true }).click();
  await expect(page.getByText("Capture this delivery")).toBeVisible();
  await page.getByLabel("Owner of received units", { exact: true }).selectOption(data.owner);
  for (let index = 0; index < data.delivery.length; index++) {
    const unit = data.delivery[index];
    await scan("Item label", unit.item);
    await page.getByLabel("Expected PO line", { exact: true }).selectOption(unit.comparator ?? "");
    if (unit.unreadable) {
      await page.getByLabel("Serial is unreadable", { exact: true }).check();
      await page.getByLabel("Unreadable reason", { exact: true }).fill("Label abraded");
    } else if (unit.serial) {
      // The wedge enters an observation into its explicit scan control, never a
      // typed serial field or natural-key resolver.
      await scan("Manufacturer serial", unit.serial);
      await expect(
        page.getByText("✓ Serial scanned: " + unit.serial, { exact: true }),
      ).toBeVisible();
    }
    await page.getByLabel("Observed condition", { exact: true }).selectOption("GOOD");
    if (index === 0)
      await page.getByLabel("Receipt photo", { exact: true }).setInputFiles({
        name: "receiving.png",
        mimeType: "image/png",
        buffer: Buffer.from(
          "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jp1sAAAAASUVORK5CYII=",
          "base64",
        ),
      });
    await page.getByRole("button", { name: "Save receiving capture", exact: true }).click();
    await expect(
      page.getByText("Unit captured. Scan the next Item.", { exact: true }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Sync now", exact: true }).click();
    await allApplied();
  }
  const receivedRows = await queue();
  const receiptId = receivedRows.find((row) => row.envelope.operation === "RECEIVE_SCAN").envelope
    .entity_id;
  await page.getByRole("button", { name: "Finish receipt", exact: true }).click();
  await expect(
    page.getByText("Receipt finished. 10 lines · 4 exceptions recorded.", { exact: true }),
  ).toBeVisible({ timeout: 30_000 });
  // Prime three asset observations, then make the API unreachable. Shell cache
  // and IDB facts must support a full reload without caching any bearer/API data.
  for (const asset of data.assets) {
    await clickTab("Move");
    await scan("Asset label", asset);
    await scan("Destination label", data.location);
  }
  await clickTab("Assign");
  await scan("Asset label", data.assets[0]);
  await page.getByLabel("Person", { exact: true }).selectOption(data.actor);
  await page.getByLabel("Reason", { exact: true }).fill("Browser assignment proof");
  await page.getByRole("button", { name: "Save assign capture", exact: true }).click();
  await allApplied();
  await clickTab("Transition");
  await scan("Asset label", data.assets[1]);
  await page.getByLabel("Next state", { exact: true }).selectOption("IN_STOCK");
  await page.getByLabel("Reason", { exact: true }).fill("Browser transition proof");
  await page.getByRole("button", { name: "Save transition capture", exact: true }).click();
  await allApplied();
  for (const asset of data.assets) {
    await clickTab("Move");
    await scan("Asset label", asset);
    await scan("Destination label", data.location);
  }
  const before = await queue();
  // An actual API outage affects service-worker fetches too, unlike page-only routing.
  const outage = await context.request.post(data.control + "/stop");
  expect(outage.ok()).toBe(true);
  await context.setOffline(true);
  await page.reload();
  await expect(page.getByText("Keep things moving.", { exact: true })).toBeVisible();
  await expect(page.getByText("Offline", { exact: true })).toBeVisible();
  await clickTab("Move");
  for (const asset of data.assets) {
    await scan("Asset label", asset);
    await scan("Destination label", data.location);
    await page.getByLabel("Reason", { exact: true }).fill("Captured during outage");
    await page.getByRole("button", { name: "Save move capture", exact: true }).click();
    await expect(page.getByRole("textbox", { name: "Asset label", exact: true })).toBeVisible();
  }
  const queued = (await queue()).filter((row) => !before.some((old) => old.id === row.id));
  expect(queued).toHaveLength(3);
  expect(queued.map((row) => row.status)).toEqual(["QUEUED", "QUEUED", "QUEUED"]);
  // Reconnect the API and independently advance one target before the queue wakes.
  await context.setOffline(false);
  const restart = await context.request.post(data.control + "/start");
  expect(restart.ok()).toBe(true);
  await page.getByRole("button", { name: "Sync now", exact: true }).click();
  await expect
    .poll(
      async () =>
        (await queue())
          .filter((row) => queued.some((old) => old.id === row.id))
          .map((row) => row.status),
      { timeout: 60_000 },
    )
    .toEqual(["APPLIED", "REJECTED", "APPLIED"]);
  const replayed = (await queue()).filter((row) => queued.some((old) => old.id === row.id));
  expect(replayed.map((row) => row.envelope)).toEqual(queued.map((row) => row.envelope));
  await clickTab("Inbox");
  await expect(page.getByText("SYNC_CONFLICT", { exact: true })).toBeVisible();
  await expect(page.getByText("Captured expectation", { exact: true })).toBeVisible();
  await expect(page.getByText("Server facts", { exact: true })).toBeVisible();
  const cachedUrls = await page.evaluate(async () => {
    const urls = [];
    for (const name of await caches.keys())
      for (const req of await (await caches.open(name)).keys()) urls.push(req.url);
    return urls;
  });
  expect(cachedUrls.some((url) => new URL(url).pathname.startsWith("/api/"))).toBe(false);
  await page.screenshot({ path: data.screenshot, fullPage: true });
  expect(errors).toEqual([]);
  console.log(
    JSON.stringify({
      receipt_id: receiptId,
      offline_operations: queued.map((row) => row.id),
      replay_order: replayed.map((row) => row.id),
      conflict_id: replayed[1].response.result.exception_id,
      photo_link_id: receivedRows.find((row) => row.evidence_operation)?.evidence_operation
        .operation_id,
      cache_count: cachedUrls.length,
      console_errors: errors,
    }),
  );
} catch (error) {
  console.error(
    "Capture buffer:",
    JSON.stringify(
      (await queue().catch(() => [])).map((row) => ({
        id: row.id,
        ordinal: row.ordinal,
        status: row.status,
        kind: row.envelope.operation,
        seq: row.envelope.client_seq,
        code: row.response?.result.code,
      })),
    ),
  );
  await page.screenshot({ path: data.screenshot, fullPage: true }).catch(() => {});
  throw error;
} finally {
  await context.close();
  await browser.close();
}
