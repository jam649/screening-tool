import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

test.use({ trace: "off" });

// Run this file alone against a newly created, disposable Docker data volume:
// PLAYWRIGHT_BASE_URL=http://localhost:8001 SCREENING_TOOL_GATE_CONTAINER=<container>
// npm test -- real-csv.spec.ts --workers=1
// The operator CLI uses the packaged official 300-entry fixture; browser API calls
// and uploads below are real, with no route interception or mocked responses.
test("real Docker CSV gate creates Admin, imports 2060 parties, and preserves one queue across repeats", async ({ page, context }) => {
  const container = process.env.SCREENING_TOOL_GATE_CONTAINER;
  test.skip(!process.env.PLAYWRIGHT_BASE_URL || !container, "Requires an isolated fresh Docker installation and explicit gate container.");
  test.setTimeout(180_000);
  const setupResponse = await context.request.get("/api/v1/setup");
  expect(setupResponse.ok()).toBeTruthy();
  const setup = await setupResponse.json();
  expect(setup.needs_setup, "Use a fresh disposable volume for this gate.").toBe(true);
  await page.goto("/");
  await page.getByLabel("Full name").fill("Synthetic CSV Gate Administrator");
  await page.getByLabel("Email address").fill("docker-gate-admin@example.test");
  await page.getByLabel("Password", { exact: true }).fill("Synthetic gate password 2026!");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText(setup.disclaimer, { exact: true })).toBeVisible();
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Create workspace" }).click();
  await expect(page.getByRole("navigation", { name: "Main navigation" })).toBeVisible();

  const seeded = JSON.parse(execFileSync("docker", [
    "exec", container!, "screening-tool", "csl", "refresh", "--file", "/app/fixtures/csl_sample_v1.json",
  ], { encoding: "utf8", timeout: 30_000 }));
  expect(seeded.version.entry_count).toBe(300);
  expect(seeded.changed).toBe(true);
  await page.reload();
  await expect(page.getByText("2 of 3 steps complete")).toBeVisible();
  await page.getByRole("link", { name: "Add source", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Sources", exact: true, level: 1 })).toBeVisible();
  const sample = await context.request.get("/api/v1/sources/demo.csv");
  expect(sample.ok()).toBeTruthy();
  expect(sample.headers()["content-type"]).toContain("text/csv");
  const csv = await sample.body();
  expect(csv.length).toBeGreaterThan(0);
  await page.getByLabel("Source name").fill("Synthetic 2060-party gate");
  await page.getByRole("button", { name: "Add CSV source" }).click();
  const wizard = page.getByRole("region", { name: "Upload a file to Synthetic 2060-party gate" });
  const file = { name: "screening-tool-demo-parties.csv", mimeType: "text/csv", buffer: csv };
  await wizard.getByLabel("File to import").setInputFiles(file);
  await wizard.getByRole("button", { name: "Preview file" }).click();
  const preview = wizard.getByRole("table", { name: /first 20 of 2,060 rows/ });
  await expect(preview.locator("tbody tr")).toHaveCount(20);
  await expect(wizard.getByLabel("Name column", { exact: true })).toHaveValue("name");
  await expect(wizard.getByLabel("External ID column", { exact: true })).toHaveValue("external_id");
  await expect(wizard.getByRole("group", { name: "Address line columns" }).getByRole("checkbox", { name: "address_lines", exact: true })).toBeChecked();
  await wizard.getByLabel("I confirm this file is the complete source snapshot").check();
  const firstResponse = page.waitForResponse(response => response.url().endsWith("/upload") && response.request().method() === "POST");
  await wizard.getByRole("button", { name: "Import file" }).click();
  const imported = await firstResponse;
  expect(imported.status()).toBe(200);
  const first = await imported.json();
  expect(first).toMatchObject({ applied: true, added: 2060, changed: 0, unchanged: 0, deleted: 0, rejected_count: 0 });
  expect(first.source.record_count).toBe(2060);
  expect(first.screening_run_ids.length).toBeGreaterThan(0);
  await expect(wizard.getByText("The import was applied.")).toBeVisible();

  const session = await (await context.request.get("/api/v1/auth/me")).json();
  const headers = { "X-CSRF-Token": session.csrf_token, Origin: new URL(process.env.PLAYWRIGHT_BASE_URL!).origin };
  const runRows = await Promise.all(first.screening_run_ids.map(async (id: string) => {
    const response = await context.request.get(`/api/v1/screen/runs/${id}`);
    expect(response.ok()).toBeTruthy();
    return response.json();
  }));
  const fullRun = runRows.find(run => run.parties_screened === 2060);
  expect(fullRun).toBeDefined();
  expect(fullRun.finished_at).toBeTruthy();
  expect(fullRun.csl_version_id).toBe(seeded.version.id);
  expect(fullRun.model_name).toBeNull();
  const before = await (await context.request.get("/api/v1/queue")).json();
  expect(before.total).toBeGreaterThan(0);
  expect(new Set(before.items.map((hit: { id: string }) => hit.id)).size).toBe(before.total);
  const dashboard = await (await context.request.get("/api/v1/dashboard")).json();
  expect(dashboard.total_parties).toBe(2060);
  expect(dashboard.coverage_latest_version).toBe(2060);
  expect(dashboard.open_items).toBe(before.total);
  expect(dashboard.likely_matches).toBeGreaterThan(0);

  await wizard.getByRole("button", { name: "Back to sources" }).click();
  await page.getByRole("article", { name: "Synthetic 2060-party gate" }).getByRole("button", { name: "Upload CSV or XLSX" }).click();
  await wizard.getByLabel("File to import").setInputFiles(file);
  await wizard.getByRole("button", { name: "Preview file" }).click();
  await expect(wizard.getByLabel("Name column", { exact: true })).toHaveValue("name");
  await expect(wizard.getByRole("group", { name: "Address line columns" }).getByRole("checkbox", { name: "address_lines", exact: true })).toBeChecked();
  await wizard.getByLabel("I confirm this file is the complete source snapshot").check();
  const repeatResponse = page.waitForResponse(response => response.url().endsWith("/upload") && response.request().method() === "POST");
  await wizard.getByRole("button", { name: "Import file" }).click();
  const repeated = await repeatResponse;
  expect(repeated.status()).toBe(200);
  const repeat = await repeated.json();
  expect(repeat).toMatchObject({ applied: true, added: 0, changed: 0, unchanged: 2060, deleted: 0, rejected_count: 0 });
  expect(repeat.task_ids).toEqual([]);
  expect(repeat.screening_run_ids).toEqual([]);
  const screen = await context.request.post("/api/v1/screen", { data: { all: true }, headers, timeout: 60_000 });
  expect(screen.ok()).toBeTruthy();
  const manual = await screen.json();
  expect(manual.parties_screened).toBe(2060);
  expect(manual.hits_new).toBe(0);
  const after = await (await context.request.get("/api/v1/queue")).json();
  expect(after.total).toBe(before.total);
  expect(after.items.map((hit: { id: string }) => hit.id).sort()).toEqual(before.items.map((hit: { id: string }) => hit.id).sort());

  const screenshots = join(homedir(), "Desktop/screenshots/screening-tool");
  const artifacts = join(homedir(), "Desktop/artifacts/screening-tool");
  await mkdir(screenshots, { recursive: true });
  await mkdir(artifacts, { recursive: true });
  await wizard.getByRole("button", { name: "Back to sources" }).click();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await page.screenshot({ path: join(screenshots, "milestone-3-real-docker-sources.png"), fullPage: true });
  await writeFile(join(artifacts, "milestone-3-real-docker-gate.json"), JSON.stringify({
    fixture_entries: seeded.version.entry_count, parties: first.source.record_count,
    first_added: first.added, completed_runs: runRows.length,
    latest_version_coverage: dashboard.coverage_latest_version, open_items: before.total,
    repeat_unchanged: repeat.unchanged, repeat_tasks: repeat.task_ids.length,
    manual_repeat_new_items: manual.hits_new, stable_queue: after.total === before.total,
  }, null, 2) + "\n");
});
