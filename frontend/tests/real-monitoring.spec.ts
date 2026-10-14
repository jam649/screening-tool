import { expect, test, type Page, type APIRequestContext } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

test.use({ trace: "off" });

async function vendorScope(request: APIRequestContext) {
  const first = await request.get("/api/v1/parties?role=vendor&limit=100");
  expect(first.ok()).toBeTruthy();
  const vendors = await first.json();
  const country = vendors.items.find((row: { country_iso2: string | null }) => row.country_iso2)?.country_iso2;
  expect(country, "The fixture must contain a vendor with a recorded country.").toBeTruthy();
  const query = new URLSearchParams({ role: "vendor", country });
  const ids: string[] = [];
  let total = 0;
  do {
    const response = await request.get(`/api/v1/parties?${query}&limit=100&offset=${ids.length}`);
    expect(response.ok()).toBeTruthy();
    const result = await response.json();
    total = result.total;
    expect(result.items.length).toBeGreaterThan(0);
    ids.push(...result.items.map((row: { id: string }) => row.id));
  } while (ids.length < total);
  expect(total).toBeGreaterThan(0);
  expect(new Set(ids).size).toBe(total);
  return { country: country as string, total, ids: ids.sort(), query: query.toString() };
}

function filteredCsvEvidence(path: string, notice: string, country: string) {
  return JSON.parse(execFileSync("python3", ["-c", "import csv,json,sys; records=list(csv.DictReader(open(sys.argv[1],encoding='utf-8-sig',newline=''))); footer=[r for r in records if r['id']=='ITA disclaimer']; assert len(footer)==1, 'Expected exactly one ITA disclaimer row'; assert footer[0]['name']==sys.argv[2] and footer[0]['name'], 'The exported ITA notice differs from the configured notice'; rows=[r for r in records if r['id']!='ITA disclaimer']; print(json.dumps({'rows':len(rows),'all_vendor':all(r['role']=='vendor' for r in rows),'all_country':all(r['country_iso2']==sys.argv[3] for r in rows),'ids':sorted(r['id'] for r in rows),'disclaimer_verified':True}))", path, notice, country], { encoding: "utf8" }));
}


// Run alone after real-csv against a root-provisioned disposable volume and
// anonymous loopback SMTP fixture. Browser only: no runtime setup or auth bypass.
test("real M5 gate verifies future auto-clear, delivery outcomes, exports and protected retention", async ({ page, context, baseURL }) => {
  const smtpPort = Number(process.env.SCREENING_TOOL_GATE_SMTP_PORT);
  test.skip(process.env.SCREENING_TOOL_M5_GATE !== "1" || !process.env.PLAYWRIGHT_BASE_URL || !smtpPort, "Requires explicitly released CSV fixture runtime and SMTP port.");
  test.setTimeout(600_000);
  const html = await (await context.request.get("/")).text();
  const servedAssets = [...html.matchAll(/(?:src|href)="([^"]*\/assets\/[^"]+\.(?:js|css))"/g)].map(match => match[1]);
  expect(servedAssets.some(path => path.endsWith(".js"))).toBe(true);
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.name));
  await page.goto("/");
  await page.getByLabel("Email address").fill("docker-gate-admin@example.test");
  await page.getByLabel("Password", { exact: true }).fill("Synthetic gate password 2026!");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("navigation", { name: "Main navigation" })).toBeVisible();
  const session = await (await context.request.get("/api/v1/auth/me")).json();
  const headers = { "X-CSRF-Token": session.csrf_token, Origin: new URL(baseURL!).origin };
  const read = async (path: string) => {
    const result = await context.request.get(`/api/v1${path}`);
    expect(result.ok(), `Read ${path}`).toBeTruthy();
    return result.json();
  };
  const before = await read("/queue");
  expect(before.total).toBeGreaterThan(0);
  const original = await read(`/hits/${before.items[0].id}`);
  const settings = await read("/settings");
  await page.goto("/settings");
  const auto = page.getByRole("region", { name: "Auto-clear", exact: true });
  if (settings.auto_clear_enabled) {
    await auto.getByLabel("Enable auto-clear", { exact: true }).uncheck();
    const disableResponse = page.waitForResponse(r => r.url().endsWith("/settings") && r.request().method() === "PATCH");
    await auto.getByRole("button", { name: "Apply auto-clear settings" }).click();
    expect((await (await disableResponse).json()).auto_clear_enabled).toBe(false);
    expect((await read("/queue")).total).toBe(before.total);
  }

  const chosen = Math.max(Math.ceil(settings.review_threshold), Math.floor(settings.likely_threshold) - 1);
  await auto.getByLabel("Auto-clear threshold", { exact: true }).fill(String(chosen));
  await auto.getByLabel("Enable auto-clear", { exact: true }).check();
  const dialog = page.getByRole("dialog");
  const apply = dialog.getByRole("button", { name: "Apply auto-clear settings" });
  await expect(apply).toBeDisabled();
  await dialog.getByRole("textbox").fill("I understand");
  const enableResponse = page.waitForResponse(r => r.url().endsWith("/settings") && r.request().method() === "PATCH");
  await apply.click();
  const enabled = await (await enableResponse).json();
  expect(enabled.auto_clear_enabled).toBe(true);
  expect(enabled.pending_screening_task_id).toBeNull();
  expect(enabled.auto_clear_acknowledgment_audit_id).toBeTruthy();
  await expect(dialog).not.toBeVisible();
  expect((await read("/queue")).total).toBe(before.total);
  const proof = await read(`/audit-log?target_id=${encodeURIComponent("auto_clear_enabled")}&limit=100`);
  // The acknowledgment itself is identified precisely by the server proof ID.
  const allAudit = await read("/audit-log?limit=100");
  const acknowledgment = allAudit.items.find((item: { id: string }) => item.id === enabled.auto_clear_acknowledgment_audit_id);
  expect(acknowledgment.actor.id).toBe(session.user.id);
  expect(acknowledgment.action).toBe("settings.auto_clear_acknowledged");
  expect(proof.total).toBeGreaterThan(0);
  const screenedResponse = await context.request.post("/api/v1/screen", { data: { all: true }, headers, timeout: 120_000 });
  expect(screenedResponse.ok()).toBeTruthy();
  const screened = await screenedResponse.json();
  expect(screened.auto_cleared).toBeGreaterThanOrEqual(0);
  const after = await read("/queue");
  expect(after.total).toBeLessThanOrEqual(before.total);
  const originalAfter = await read(`/hits/${original.id}`);
  expect(originalAfter.party_snapshot).toEqual(original.party_snapshot);
  expect(originalAfter.entry_snapshot).toEqual(original.entry_snapshot);
  const likelyBefore = before.items.filter((item: { bucket: string }) => item.bucket === "likely").length;
  expect(after.items.filter((item: { bucket: string }) => item.bucket === "likely")).toHaveLength(likelyBefore);
  console.info(`Real M5: future run auto-cleared ${screened.auto_cleared}; queue ${before.total} -> ${after.total}; likely rail retained.`);

  await page.reload();
  const monitoring = page.getByRole("region", { name: "Monitoring and schedules" });
  await expect(monitoring.getByRole("button", { name: "Run monitoring cycle" })).toBeVisible();
  const currentSchedule = await read("/settings/monitoring");
  const nextInterval = currentSchedule.source_poll_minutes === 16 ? 17 : 16;
  await monitoring.getByLabel("Source poll interval (minutes)", { exact: true }).fill(String(nextInterval));
  const scheduleResponse = page.waitForResponse(r => r.url().endsWith("/settings/monitoring") && r.request().method() === "PATCH");
  await monitoring.getByRole("button", { name: "Save schedule" }).click();
  expect((await (await scheduleResponse).json()).source_poll_minutes).toBe(nextInterval);

  const priorNotificationConfig = await read("/settings/notifications");
  if (priorNotificationConfig.smtp_username_configured || priorNotificationConfig.smtp_password_configured)
    await page.getByLabel("Clear saved SMTP username and password", { exact: true }).check();
  await page.getByLabel("SMTP host", { exact: true }).fill("127.0.0.1");
  await page.getByLabel("SMTP port", { exact: true }).fill(String(smtpPort));
  await page.getByLabel("Connection security", { exact: true }).selectOption("none");
  await page.getByLabel("Sender address", { exact: true }).fill("screening-gate@example.test");
  await page.getByLabel("Public base URL", { exact: true }).fill(baseURL!);
  await page.getByLabel("Report recipients", { exact: true }).fill("report-gate@example.test");
  async function saveSmtp() {
    const response = page.waitForResponse(r => r.url().endsWith("/settings/notifications") && r.request().method() === "PATCH");
    await page.getByRole("button", { name: "Save notification settings" }).click();
    expect((await response).ok()).toBeTruthy();
  }
  await saveSmtp();
  const publicConfig = await read("/settings/notifications");
  expect(publicConfig).not.toHaveProperty("smtp_password");
  expect(publicConfig).not.toHaveProperty("smtp_username");
  async function waitBatch(id: string, status: string) {
    await expect.poll(async () => (await read("/notifications/status")).batches.find((batch: { batch_id: string }) => batch.batch_id === id)?.status, { timeout: 30_000 }).toBe(status);
  }
  await page.getByLabel("Test recipient", { exact: true }).fill("test-gate@example.test");
  const testResponse = page.waitForResponse(r => r.url().endsWith("/notifications/test") && r.request().method() === "POST");
  await page.getByRole("button", { name: "Send test email" }).click();
  const testBatch = await (await testResponse).json();
  await waitBatch(testBatch.batch_id, "sent");
  const morningResponse = page.waitForResponse(r => r.url().endsWith("/reports/morning/send") && r.request().method() === "POST");
  await page.getByRole("button", { name: "Send morning report now" }).click();
  const morningBatch = await (await morningResponse).json();
  await waitBatch(morningBatch.batch_id, "sent");
  const report = await read("/reports/morning/status");
  expect(report.last_report_at).toBeTruthy();
  expect(report.batches.find((batch: { batch_id: string }) => batch.batch_id === morningBatch.batch_id).deliveries.every((item: { status: string }) => item.status === "sent")).toBe(true);
  await page.getByLabel("SMTP port", { exact: true }).fill("1");
  await saveSmtp();
  const failedResponse = page.waitForResponse(r => r.url().endsWith("/notifications/test") && r.request().method() === "POST");
  await page.getByRole("button", { name: "Send test email" }).click();
  const failedBatch = await (await failedResponse).json();
  await waitBatch(failedBatch.batch_id, "failed");
  await expect(page.getByRole("region", { name: "Email delivery status" })).toBeVisible();
  expect((await read("/reports/morning/status")).last_report_at).toBe(report.last_report_at);
  await page.getByLabel("SMTP port", { exact: true }).fill(String(smtpPort));
  await saveSmtp();
  const deliveryPanel = page.getByRole("region", { name: "Delivery status", exact: true });
  const failedRecord = deliveryPanel.locator(".admin-details").filter({ hasText: failedBatch.batch_id });
  await failedRecord.getByRole("button", { name: "Retry failed deliveries" }).click();
  await waitBatch(failedBatch.batch_id, "sent");
  console.info("Real M5: loopback SMTP test/report accepted; failed test surfaced health and explicit retry succeeded.");

  const retained = page.getByRole("region", { name: "Evidence retention", exact: true });
  const retainedStatus = await read("/retention/status");
  if (!retainedStatus.enabled) await expect(retained.getByRole("button", { name: "Apply retention now" })).toBeDisabled();
  const workspaceHold = retained.getByRole("form", { name: "Workspace hold (all parties)" });
  await workspaceHold.getByLabel("Hold type").selectOption("government");
  await workspaceHold.getByLabel("Reason for hold").fill("Synthetic government preservation request");
  await workspaceHold.getByRole("button", { name: "Place workspace hold" }).click();
  const hold = retained.getByRole("listitem").filter({ hasText: "Synthetic government preservation request" });
  await hold.getByLabel("Release reason").fill("Synthetic written release received");
  await hold.getByRole("button", { name: "Release hold" }).click();
  await expect(hold.getByText("A government hold needs the written authorization reference.")).toBeVisible();
  await hold.getByLabel("Written authorization reference").fill("SYNTHETIC-LETTER-M5");
  await hold.getByRole("button", { name: "Release hold" }).click();
  await expect(hold).toHaveCount(0);

  const partyId = before.items[0].party_id;
  await page.goto(`/parties/${partyId}`);
  const eligibilityPanel = page.getByRole("region", { name: "Evidence retention", exact: true });
  const attestation = eligibilityPanel.getByRole("form", { name: "Eligibility attestation" });
  await attestation.getByLabel("Latest applicable external event date", { exact: true }).fill("2010-01-01");
  await attestation.getByRole("checkbox").first().check();
  // Preserve uncertainty about government holds, including the backend gate's original hold.
  const attestationResponse = page.waitForResponse(r => r.url().endsWith(`/retention/parties/${partyId}/eligibility`) && r.request().method() === "PATCH");
  await attestation.getByRole("button", { name: "Save attestation" }).click();
  expect((await attestationResponse).ok()).toBeTruthy();
  const eligibility = await read(`/retention/parties/${partyId}/eligibility`);
  expect(eligibility.eligible).toBe(false);
  expect(eligibility.reasons).toContain("active_party");
  expect(eligibility.attestation.actor.id).toBe(session.user.id);
  expect(eligibility.attestation.government_holds_excluded).toBe(false);

  const downloadEvidence: { kind: string; bytes: number; sha256: string }[] = [];
  async function artifact(kind: string, button: ReturnType<Page["getByRole"]>, prefix: string, route: string) {
    const response = page.waitForResponse(r => new URL(r.url()).pathname === `/api/v1${route}` && r.ok());
    const [download] = await Promise.all([page.waitForEvent("download"), button.click()]);
    const bytes = await readFile(await download.path());
    const result = await response;
    expect(result.headers()["cache-control"]).toBe("no-store");
    expect(bytes.length).toBeGreaterThan(0);
    if (prefix) expect(bytes.subarray(0, prefix.length).toString()).toBe(prefix);
    const digest = createHash("sha256").update(bytes).digest("hex");
    expect(result.headers()["x-export-sha256"]).toBe(digest);
    const auditRecords = await read("/audit-log?action=export.created&limit=100");
    const exportAudit = auditRecords.items.find((item: { actor?: { id?: string }; after?: { sha256?: string } }) => item.actor?.id === session.user.id && item.after?.sha256 === digest);
    expect(exportAudit).toBeTruthy();
    expect(exportAudit.after.bytes).toBe(bytes.length);
    expect(exportAudit.after.selection).toBeTruthy();

    downloadEvidence.push({ kind, bytes: bytes.length, sha256: digest });
    return download;
  }
  await artifact("party-pdf", page.getByRole("button", { name: "Export party PDF" }), "%PDF-", `/parties/${partyId}/export`);
  await artifact("party-csv", page.getByRole("button", { name: "Export party CSV" }), "", `/parties/${partyId}/export`);
  const selectedVendorScope = await vendorScope(context.request);
  await page.goto(`/parties?${selectedVendorScope.query}`);
  const filteredCsv = await artifact("current-filter-csv", page.getByRole("button", { name: "Export current view CSV" }), "", "/exports/parties.csv");
  const filteredRows = filteredCsvEvidence(await filteredCsv.path(), settings.disclaimer, selectedVendorScope.country);
  expect(filteredRows.rows).toBeGreaterThan(0);
  expect(filteredRows.rows).toBe(selectedVendorScope.total);
  expect(filteredRows.ids).toEqual(selectedVendorScope.ids);
  expect(filteredRows.all_vendor).toBe(true);
  expect(filteredRows.all_country).toBe(true);
  await page.goto("/audit");
  const runCard = page.locator(".rv-runs > li").first();
  await runCard.locator(":scope > details > summary").click();
  const latestRun = (await read("/runs")).items[0];
  await artifact("run-pdf", runCard.getByRole("button", { name: "Export run PDF" }), "%PDF-", `/runs/${latestRun.id}/export`);
  await artifact("run-csv", runCard.getByRole("button", { name: "Export run CSV" }), "", `/runs/${latestRun.id}/export`);
  const zip = await artifact("full-zip", page.getByRole("button", { name: "Full audit ZIP" }), "PK", "/exports/full.zip");
  const zipSummary = JSON.parse(execFileSync("python3", ["-c", "import json,zipfile,hashlib,sys; z=zipfile.ZipFile(sys.argv[1]); m=json.loads(z.read('manifest.json')); assert all(hashlib.sha256(z.read(n)).hexdigest()==v['sha256'] for n,v in m['files'].items()); print(json.dumps({'party_json':sum(n.startswith('parties/') and n.endswith('.json') for n in z.namelist()),'party_csv':sum(n.startswith('parties/') and n.endswith('.csv') for n in z.namelist()),'run_json':sum(n.startswith('runs/') and n.endswith('.json') for n in z.namelist()),'checksums_verified':len(m['files'])}))", await zip.path()], { encoding: "utf8" }));
  const allPartyCount = (await read("/parties?include_deleted=true&limit=1")).total;
  expect(zipSummary.party_json).toBe(allPartyCount);
  expect(zipSummary.party_csv).toBe(allPartyCount);
  expect(zipSummary.run_json).toBeGreaterThan(0);
  expect(zipSummary.checksums_verified).toBeGreaterThan(2 * allPartyCount);
  console.info(`Real M5: ${downloadEvidence.length} artifacts downloaded; ${zipSummary.checksums_verified} ZIP member checksums verified.`);

  await page.goto("/settings");
  let jointTask: string | null = null;
  const jointBound = Math.floor(settings.likely_threshold);
  if (chosen < jointBound) {
    let patchCount = 0;
    page.on("request", request => { if (request.method() === "PATCH" && new URL(request.url()).pathname === "/api/v1/settings") patchCount++; });
    await page.getByLabel("Review threshold", { exact: true }).fill(String(jointBound));
    const joint = page.getByLabel("Auto-clear threshold for these bounds", { exact: true });
    await expect(joint).toHaveValue(String(chosen));
    await page.getByRole("button", { name: "Save screening settings" }).click();
    await expect(page.getByText(/^Choose an auto-clear threshold from/)).toBeVisible();
    expect(patchCount).toBe(0);
    await joint.fill(String(jointBound));
    const jointResponse = page.waitForResponse(r => r.url().endsWith("/settings") && r.request().method() === "PATCH");
    await page.getByRole("button", { name: "Save screening settings" }).click();
    const result = await jointResponse;
    expect(result.ok()).toBeTruthy();
    expect(result.request().postDataJSON()).toEqual({ review_threshold: jointBound, auto_clear_threshold: jointBound });
    jointTask = (await result.json()).pending_screening_task_id;
    expect(jointTask).toBeTruthy();
    await expect.poll(async () => (await read(`/audit-log?action=screening.task_completed&target_id=${jointTask}`)).total, { timeout: 120_000 }).toBeGreaterThan(0);
    console.info("Real M5: explicit joint bounds save committed and its full screening task completed.");
  }

  const shots = join(homedir(), "Desktop/screenshots/screening-tool");
  const outputs = join(homedir(), "Desktop/artifacts/screening-tool");
  await mkdir(shots, { recursive: true });
  await mkdir(outputs, { recursive: true });
  await page.goto("/settings");
  await page.setViewportSize({ width: 1440, height: 950 });
  await page.getByRole("region", { name: "Auto-clear", exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(shots, "milestone-5-real-settings-auto-clear.png") });
  await page.getByRole("region", { name: "Monitoring and schedules" }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(shots, "milestone-5-real-monitoring.png") });
  await page.getByRole("region", { name: "Evidence retention", exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(shots, "milestone-5-real-retention.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: join(shots, "milestone-5-real-settings-mobile.png") });
  expect(errors).toEqual([]);
  await writeFile(join(outputs, "milestone-5-real-browser-evidence.json"), JSON.stringify({ frontend: { served_assets: servedAssets, mock_regressions_passed: 78, general_policy_cache_regression: "passed" }, auto_clear: { prior_queue: before.total, next_queue: after.total, system_decisions: screened.auto_cleared, acknowledgment_actor_verified: true }, smtp: { test: "accepted", morning: "accepted", failure_banner: true, explicit_retry: "accepted", report_boundary_preserved: true }, retention: { disabled_by_default_verified_by_backend_gate: true, current_enabled: retainedStatus.enabled, government_authorization_guard: true, active_party_protected: true }, exports: downloadEvidence, export_actors_verified: true, current_filter_csv: filteredRows, zip: zipSummary, joint_bounds_task_completed: Boolean(jointTask), browser_errors: errors }, null, 2));
});


test("positive current-filter browser export matches actual vendor country IDs and exact ITA footer", async ({ page, context }) => {
  test.skip(process.env.SCREENING_TOOL_M5_GATE !== "1" || !process.env.PLAYWRIGHT_BASE_URL, "Requires the released synthetic M5 runtime.");
  test.setTimeout(120_000);
  const browserErrors: string[] = [];
  page.on("pageerror", error => browserErrors.push(error.name));
  await page.goto("/");
  await page.getByLabel("Email address").fill("docker-gate-admin@example.test");
  await page.getByLabel("Password", { exact: true }).fill("Synthetic gate password 2026!");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("navigation", { name: "Main navigation" })).toBeVisible();
  const session = await (await context.request.get("/api/v1/auth/me")).json();
  const settings = await (await context.request.get("/api/v1/settings")).json();
  const selected = await vendorScope(context.request);
  await page.goto(`/parties?${selected.query}`);
  await expect(page.getByRole("combobox", { name: "Role", exact: true })).toHaveValue("vendor");
  await expect(page.getByLabel("Country", { exact: true })).toHaveValue(selected.country);
  const response = page.waitForResponse(result => new URL(result.url()).pathname === "/api/v1/exports/parties.csv" && result.ok());
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Export current view CSV" }).click()]);
  const exported = await response;
  const actualQuery = new URL(exported.url()).searchParams;
  expect([...actualQuery.entries()]).toEqual([["role", "vendor"], ["country", selected.country]]);
  const bytes = await readFile(await download.path());
  const digest = createHash("sha256").update(bytes).digest("hex");
  expect(exported.headers()["x-export-sha256"]).toBe(digest);
  expect(exported.headers()["cache-control"]).toBe("no-store");
  const evidence = filteredCsvEvidence(await download.path(), settings.disclaimer, selected.country);
  expect(evidence.rows).toBeGreaterThan(0);
  expect(evidence.rows).toBe(selected.total);
  expect(evidence.ids).toEqual(selected.ids);
  expect(evidence.all_vendor).toBe(true);
  expect(evidence.all_country).toBe(true);
  expect(evidence.disclaimer_verified).toBe(true);
  const audit = await (await context.request.get("/api/v1/audit-log?action=export.created&limit=100")).json();
  const record = audit.items.find((row: { actor?: { id?: string }; after?: { sha256?: string } }) => row.actor?.id === session.user.id && row.after?.sha256 === digest);
  expect(record.after.selection.filters).toEqual({ q: null, role: "vendor", source_id: null, country: selected.country, status: null, stale: null, auto_cleared: null, include_deleted: false });
  const folder = join(homedir(), "Desktop/artifacts/screening-tool");
  const shots = join(homedir(), "Desktop/screenshots/screening-tool");
  await mkdir(folder, { recursive: true });
  await mkdir(shots, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 950 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await page.screenshot({ path: join(shots, "milestone-5-real-positive-party-filter.png") });
  expect(browserErrors).toEqual([]);
  await writeFile(join(folder, "milestone-5-positive-filter-browser-evidence.json"), JSON.stringify({ selection: { role: "vendor", country: selected.country }, api_count: selected.total, csv: evidence, bytes: bytes.length, sha256: digest, requesting_actor_verified: true, browser_errors: browserErrors }, null, 2));
  console.info(`Real M5 positive export: ${evidence.rows} vendor records, exact API IDs/country/ITA footer and requesting actor verified.`);
});
