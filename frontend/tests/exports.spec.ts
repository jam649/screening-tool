import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { expect, test, type Page, type Route } from "@playwright/test";

const PARTY_ID = "4f7c1d2e-0000-4000-8000-00000000a001";
const RUN_IDS = ["9b1e0c3a-0000-4000-8000-00000000b001", "9b1e0c3a-0000-4000-8000-00000000b002"];
const sha = (body: Buffer) => createHash("sha256").update(body).digest("hex");

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

function file(route: Route, body: Buffer, contentType: string, disposition?: string) {
  return route.fulfill({
    status: 200,
    body,
    headers: {
      "Content-Type": contentType,
      "Cache-Control": "no-store",
      "X-Export-SHA256": sha(body),
      ...(disposition ? { "Content-Disposition": disposition } : {}),
    },
  });
}

const party = (index: number) => ({
  id: index === 0 ? PARTY_ID : `synthetic-party-${index}`,
  name: `Synthetic Party ${index + 1}`,
  role: "customer",
  country: "US",
  status: "clear",
  source_id: "synthetic-source",
  source_name: "Synthetic source",
  active: true,
  open_hits: 0,
  last_screened_at: "2026-10-01T12:00:00Z",
});

const run = (id: string, day: number) => ({
  id,
  trigger: "scheduled",
  started_at: `2026-10-0${day}T06:00:00Z`,
  finished_at: `2026-10-0${day}T06:01:00Z`,
  csl_version_id: "synthetic-csl-version",
  parties_screened: 3,
  hits_new: 0,
  hits_suppressed: 0,
  auto_cleared: 0,
  model_name: null,
  model_fallbacks: [],
  settings_snapshot: {},
});

// Synthetic app interfaces only; export routes registered later take precedence.
async function mockApp(page: Page, role: "admin" | "reviewer") {
  const user = {
    id: "synthetic-user",
    email: `${role}@example.test`,
    display_name: `Synthetic ${role}`,
    role,
    active: true,
  };
  await page.route("**/api/v1/**", (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^\/api\/v1/, "");
    if (path === "/setup")
      return json(route, {
        needs_setup: false,
        product_name: "Screening tool",
        disclaimer: "Synthetic test notice.",
        disclaimer_url: "https://example.test/notice",
      });
    if (path === "/auth/me") return json(route, { user, csrf_token: "synthetic-csrf" });
    if (path === "/notifications/status") return json(route, { batches: [], health: [] });
    if (path === "/monitoring/status") return json(route, { enabled: true, schedules: [], operations: [], health: [] });
    if (path === "/sources") return json(route, []);
    if (path === "/parties") {
      const offset = Number(url.searchParams.get("offset") ?? 0);
      const count = Math.max(0, Math.min(100, 150 - offset));
      const items = Array.from({ length: count }, (_, i) => party(offset + i));
      return json(route, { items, total: 150, offset, limit: 100 });
    }
    if (path === `/parties/${PARTY_ID}`)
      return json(route, { party: { ...party(0), party_type: "entity", alt_names: [], address_lines: [], suppressed_pairings: 0, stale: false }, source: null, events: [], audit_log: [] });
    if (path === "/runs")
      return json(route, { items: RUN_IDS.map((id, i) => run(id, i + 1)), total: 2, offset: 0, limit: 25 });
    return json(route, { items: [], total: 0, offset: 0, limit: 25 });
  });
}

async function download(page: Page, click: () => Promise<void>) {
  const [event] = await Promise.all([page.waitForEvent("download"), click()]);
  return { name: event.suggestedFilename(), body: await readFile(await event.path()) };
}

test("Reviewer exports a party file as PDF and CSV with safe filenames", async ({ page }) => {
  await mockApp(page, "reviewer");
  const pdf = Buffer.from("%PDF-1.7\nsynthetic party file\n%%EOF\n");
  const csv = Buffer.from("party_id,name\nsynthetic,Synthetic Party 1\n");
  const seen: { path: string; method: string; csrf?: string }[] = [];
  let release!: () => void;
  const gate = new Promise<void>((resolve) => (release = resolve));
  await page.route(/\/api\/v1\/parties\/[^/]+\/export\?/, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    seen.push({ path: url.pathname + url.search, method: request.method(), csrf: request.headers()["x-csrf-token"] });
    if (url.searchParams.get("format") === "pdf") {
      await gate;
      return file(route, pdf, "application/pdf", `attachment; filename="ignored.pdf"; filename*=UTF-8''r%C3%A9sum%C3%A9%2Fparty%0A.pdf`);
    }
    return file(route, csv, "text/csv; charset=utf-8");
  });

  await page.goto(`/parties/${PARTY_ID}`);
  const group = page.getByRole("group", { name: "Party exports" });
  const pdfButton = group.getByRole("button", { name: "Export party PDF" });
  const pending = page.waitForEvent("download");
  await pdfButton.click();
  await expect(group.getByRole("status")).toHaveText("Preparing download…");
  await expect(pdfButton).toBeDisabled();
  await expect(group.getByRole("button", { name: "Export party CSV" })).toBeDisabled();
  release();
  const first = await pending;
  expect(first.suggestedFilename()).toBe("résumé_party.pdf");
  expect(await readFile(await first.path())).toEqual(pdf);
  await expect(group.getByRole("status")).toHaveText("Download started: résumé_party.pdf");
  await group.getByText("Server-reported SHA-256").click();
  await expect(group.getByText(sha(pdf))).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  const second = await download(page, () => group.getByRole("button", { name: "Export party CSV" }).click());
  expect(second.name).toBe(`party-${PARTY_ID}.csv`);
  expect(second.body).toEqual(csv);
  expect(seen.map((r) => r.path)).toEqual([
    `/api/v1/parties/${PARTY_ID}/export?format=pdf`,
    `/api/v1/parties/${PARTY_ID}/export?format=csv`,
  ]);
  expect(seen.every((r) => r.method === "GET" && r.csrf === undefined)).toBe(true);
});

test("Current view CSV sends every list filter without pagination and includes all matches", async ({ page }) => {
  await mockApp(page, "admin");
  const filters: [string, string][] = [
    ["q", "Société Ωmega 東京"],
    ["role", "customer"],
    ["source_id", "synthetic-source"],
    ["country", "FR"],
    ["status", "clear"],
    ["stale", "true"],
    ["auto_cleared", "false"],
    ["include_deleted", "true"],
  ];
  const rows = Array.from({ length: 150 }, (_, i) => `synthetic-party-${i},Synthetic Party ${i + 1}`);
  const csv = Buffer.from(["id,name", ...rows].join("\n") + "\n");
  const requests: URL[] = [];
  await page.route(/\/api\/v1\/exports\/parties\.csv(\?|$)/, (route) => {
    requests.push(new URL(route.request().url()));
    return file(route, csv, "text/csv", 'attachment; filename="parties-view.csv"');
  });

  await page.goto(`/parties?${new URLSearchParams([...filters, ["offset", "100"], ["limit", "100"]])}`);
  const result = await download(page, () =>
    page.getByRole("button", { name: "Export current view CSV" }).click(),
  );
  expect(requests).toHaveLength(1);
  expect([...requests[0].searchParams.entries()]).toEqual(filters);
  expect(requests[0].search).toContain("q=Soci%C3%A9t%C3%A9");
  expect(result.name).toBe("parties-view.csv");
  expect(result.body.toString("utf8").trim().split("\n")).toHaveLength(151);
});

test("Admin exports a specific run card and the full audit ZIP", async ({ page }) => {
  await mockApp(page, "admin");
  const header = Buffer.from("party_id,name,outcome\n");
  const pdf = Buffer.from("%PDF-1.7\nsynthetic run\n%%EOF\n");
  const zip = Buffer.from([0x50, 0x4b, 0x05, 0x06, ...Array<number>(18).fill(0)]);
  const paths: string[] = [];
  await page.route(/\/api\/v1\/runs\/[^/]+\/export\?/, (route) => {
    const url = new URL(route.request().url());
    paths.push(url.pathname + url.search);
    return url.searchParams.get("format") === "csv"
      ? file(route, header, "text/csv")
      : file(route, pdf, "application/pdf");
  });
  await page.route("**/api/v1/exports/full.zip", (route) =>
    file(route, zip, "application/zip", 'attachment; filename="full-audit-2026-10-13.zip"'),
  );

  await page.goto("/audit");
  const card = page.locator(".rv-runs > li").nth(1);
  await card.locator("summary").first().click();
  const runGroup = card.getByRole("group", { name: "Run exports" });
  const csvFile = await download(page, () => runGroup.getByRole("button", { name: "Export run CSV" }).click());
  expect(csvFile.name).toBe(`run-${RUN_IDS[1]}.csv`);
  expect(csvFile.body).toEqual(header);
  const pdfFile = await download(page, () => runGroup.getByRole("button", { name: "Export run PDF" }).click());
  expect(pdfFile.body).toEqual(pdf);
  expect(paths).toEqual([
    `/api/v1/runs/${RUN_IDS[1]}/export?format=csv`,
    `/api/v1/runs/${RUN_IDS[1]}/export?format=pdf`,
  ]);

  const full = await download(page, () => page.getByRole("button", { name: "Full audit ZIP" }).click());
  expect(full.name).toBe("full-audit-2026-10-13.zip");
  expect(full.body).toEqual(zip);
  await expect(
    page.getByRole("group", { name: "Full audit export" }).getByRole("status"),
  ).toHaveText("Download started: full-audit-2026-10-13.zip");
});

test("Full audit ZIP reports server, empty and wrong-type failures without automatic retry", async ({ page }) => {
  await mockApp(page, "reviewer");
  const zip = Buffer.from([0x50, 0x4b, 0x05, 0x06, ...Array<number>(18).fill(0)]);
  let calls = 0;
  await page.route("**/api/v1/exports/full.zip", (route) => {
    calls += 1;
    if (calls === 1) return json(route, { detail: "Export storage is temporarily unavailable." }, 503);
    if (calls === 2) return route.fulfill({ status: 200, contentType: "application/zip", body: "" });
    if (calls === 3) return route.fulfill({ status: 200, contentType: "text/html", body: "<p>synthetic</p>" });
    return file(route, zip, "application/zip");
  });

  await page.goto("/audit");
  const group = page.getByRole("group", { name: "Full audit export" });
  const alert = group.getByRole("alert");
  const retry = group.getByRole("button", { name: "Try again" });
  await group.getByRole("button", { name: "Full audit ZIP" }).click();
  await expect(alert).toContainText("Export storage is temporarily unavailable.");
  await expect(alert).not.toContainText(/changed|modified|deleted/i);
  expect(calls).toBe(1);

  await retry.click();
  await expect(alert).toContainText("empty file");
  await retry.click();
  await expect(alert).toContainText("unexpected file type");

  const result = await download(page, () => retry.click());
  expect(result.name).toBe("full-audit.zip");
  expect(result.body).toEqual(zip);
  await expect(alert).toHaveCount(0);
  await expect(group.getByRole("status")).toHaveText("Download started: full-audit.zip");
  expect(calls).toBe(4);
});
