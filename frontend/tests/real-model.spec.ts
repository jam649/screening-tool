import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ModelConfig, ModelTestResult, SavedModelConfig } from "../src/modelApi";

// Fresh packaged fixture runtime released by the coordinator only. No routes are
// intercepted, no runtime/database is reset, no test is resumed or retried.
// SCREENING_TOOL_M8_GATE=1 PLAYWRIGHT_BASE_URL=http://localhost:8023
// npm test -- tests/real-model.spec.ts --workers=1
// System One provider HTTP is replaced solely inside the fixture API process.
test.use({ trace: "off", video: "off" });
test.describe.configure({ retries: 0 });
type Json = Record<string, any>;
const SOURCE_LABEL = "Synthetic model browser CSV";
const ADMIN_EMAIL = "model-fixture-admin@example.test";
const ADMIN_PASSWORD = "Synthetic model gate password2026!";
const REVIEWER_PASSWORD = "Synthetic model reviewer password2026!";
const LAYA_KEY = "synthetic-laya-private-key";
const JEV_KEY = "fixture-jev-write-only-key-v8";
const NOTE = "Synthetic model fixture false-positive decision";
const NOTICE = "Enabling Jev sends your customer and vendor names and addresses to TypeSafe AI for scoring. Laya keeps all data on this server.";
const TARGET = "M8 Synthetic Zqxv Identity Control 42";
const ADDRESS = "1 Main Street " + "long street identity evidence ".repeat(200);
const csvBook = (changed = false) => [
  "external_id,name,party_type,role,country,city,address_lines",
  `fp,${TARGET}${changed ? " LLC" : ""},${changed ? "individual" : "company"},customer,US,Boston,${ADDRESS}`,
  `confirmed,${TARGET},company,vendor,US,Boston,${ADDRESS}`,
  "",
].join("\n");
const digest = (value: unknown) => createHash("sha256").update(JSON.stringify(value)).digest("hex");
const panel = (page: Page) => page.getByRole("region", { name: "Model settings", exact: true });
const provider = (page: Page) => panel(page).getByLabel("Model provider", { exact: true });
const save = (page: Page) => panel(page).getByRole("button", { name: "Save model settings", exact: true });
const testButton = (page: Page) => panel(page).getByRole("button", { name: /^Test saved/ });
const testRegion = (page: Page) => panel(page).getByRole("region", { name: "Connection test", exact: true });
const fact = (page: Page, label: string) => testRegion(page).getByRole("listitem").filter({ has: page.getByText(label, { exact: true }) }).locator("span");

async function read<T = Json>(request: APIRequestContext, path: string): Promise<T> {
  const response = await request.get(`/api/v1${path}`);
  expect(response.ok(), `Read failed: ${path.split("?")[0]}`).toBe(true);
  return response.json();
}
async function settledTask(request: APIRequestContext, taskId: string | null) {
  if (!taskId) return null;
  let runId: string | undefined;
  await expect.poll(async () => {
    const events = await read(request, `/audit-log?action=screening.task_completed&target_table=screening_tasks&target_id=${encodeURIComponent(taskId)}&limit=100`);
    runId = events.items.find((row: Json) => row.after?.run_id)?.after.run_id;
    return Boolean(runId);
  }, { timeout: 90_000, intervals: [100, 250, 500, 1000] }).toBe(true);
  const run = await read(request, `/screen/runs/${runId}`);
  expect(run.finished_at).toBeTruthy();
  return run;
}
async function saveNative(page: Page, request: APIRequestContext) {
  const pending = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/settings/model" && response.request().method() === "PATCH");
  await save(page).click();
  const response = await pending;
  expect(response.status()).toBe(200);
  const result = await response.json() as SavedModelConfig;
  await expect(panel(page).locator("form").getByRole("status")).toContainText(`generation ${result.generation}`);
  const run = await settledTask(request, result.pending_screening_task_id);
  return { result, run };
}
async function testNative(page: Page) {
  const pending = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/settings/model/test" && response.request().method() === "POST");
  await testButton(page).click();
  const response = await pending;
  expect(response.status()).toBe(200);
  const result = await response.json() as ModelTestResult;
  await expect(fact(page, "Tested generation")).toHaveText(String(result.generation));
  return result;
}
async function importNative(page: Page, request: APIRequestContext, contents: string, existing: boolean) {
  await page.goto("/sources");
  if (!existing) {
    await page.getByRole("region", { name: "Add a CSV source", exact: true }).getByLabel("Source name").fill(SOURCE_LABEL);
    await page.getByRole("button", { name: "Add CSV source", exact: true }).click();
  } else {
    await page.getByRole("article", { name: SOURCE_LABEL, exact: true }).getByRole("button", { name: "Upload CSV or XLSX", exact: true }).click();
  }
  const wizard = page.getByRole("region", { name: `Upload a file to ${SOURCE_LABEL}`, exact: true });
  await wizard.getByLabel("File to import", { exact: true }).setInputFiles({ name: "synthetic-model-parties.csv", mimeType: "text/csv", buffer: Buffer.from(contents) });
  await wizard.getByRole("button", { name: "Preview file", exact: true }).click();
  await expect(wizard.getByLabel("Name column", { exact: true })).toHaveValue("name");
  await expect(wizard.getByLabel("External ID column", { exact: true })).toHaveValue("external_id");
  await wizard.getByLabel("I confirm this file is the complete source snapshot").check();
  const pending = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/upload") && response.request().method() === "POST");
  await wizard.getByRole("button", { name: "Import file", exact: true }).click();
  const response = await pending; expect(response.status()).toBe(200);
  const result = await response.json(); expect(result.applied).toBe(true);
  for (const taskId of result.task_ids) await settledTask(request, taskId);
  await expect(wizard.getByText("The import was applied.", { exact: true })).toBeVisible();
  return result;
}

const privacyScript = `
import hashlib,json,re,sys,zipfile
needles=[s.encode() for s in json.loads(sys.stdin.read()) if s]
private={"jev_api_key","laya_api_key","client_id","client_secret","verifier_token","credentials_enc","password","password_hash","csrf_token","session_token","token_hash","api_key","smtp_password","smtp_username","client_state","clientState","app_secret","bearer_token","access_token","refresh_token","token_credentials","cursor"}
oauth={"state_hash","authorization_code","code","oauth_state","raw_state","authorization_url"}
markers=0
geographic_states=0
def inspect(value,trail):
    global markers,geographic_states
    if isinstance(value,dict):
        for key,item in value.items():
            if key in oauth: raise RuntimeError('OAuth field present in public export')
            if key=='state':
                geography=bool(re.search(r"(?:^|[.])addresses\\[\\d+\\]$",trail)) and (item is None or (isinstance(item,str) and len(item)<=200 and not any(ord(c)<32 for c in item)))
                if not geography: raise RuntimeError('Unexpected state at '+trail+'.state (type='+type(item).__name__+', length='+str(len(item) if isinstance(item,str) else None)+')')
                geographic_states+=1
            if key in private or key.endswith('_enc'):
                audit=(".audit_log[" in trail or trail.startswith("audit.jsonl[")) and bool(re.search(r"[.](?:before|after)(?:[.\\[]|$)",trail))
                if not audit or (item is not None and item!='[redacted]'): raise RuntimeError('Private field outside exact redacted audit evidence')
                markers+=1
            inspect(item,trail+'.'+key)
    elif isinstance(value,list):
        for index,item in enumerate(value): inspect(item,trail+'['+str(index)+']')
with zipfile.ZipFile(sys.argv[1]) as archive:
    names=archive.namelist();index=json.loads(archive.read('manifest.json'))['files']
    if set(index)!=set(names)-{'manifest.json'}: raise RuntimeError('Incomplete checksum manifest')
    for name in names:
        data=archive.read(name)
        if any(needle in data for needle in needles): raise RuntimeError('Private value found in ZIP')
        if name in index:
            if hashlib.sha256(data).hexdigest()!=index[name]['sha256'] or len(data)!=index[name]['bytes']: raise RuntimeError('Member checksum mismatch')
        if name.endswith('.json'): inspect(json.loads(data),name)
        elif name.endswith('.jsonl'):
            for position,line in enumerate(data.splitlines()):
                row=json.loads(line)
                if name=='sources.jsonl' and ((private|oauth|{'last_error','state'}).intersection(row) or any(key.endswith('_enc') for key in row)): raise RuntimeError('Private public source field')
                inspect(row,name+'['+str(position)+']')
    print(json.dumps({'verified_checksums':len(index),'members':len(names),'party_json':sum(name.startswith('parties/') and name.endswith('.json') for name in names),'private_values_absent':True,'public_source_private_fields_absent':True,'oauth_fields_absent':True,'exact_audit_redactions_checked':markers,'geographic_state_fields_checked':geographic_states}))
`;

async function auditZip(page: Page, request: APIRequestContext, actorId: string, privateValues: string[]) {
  await page.goto("/audit");
  const pending = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/exports/full.zip" && response.ok());
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Full audit ZIP", exact: true }).click()]);
  const path = await download.path();
  const content = await readFile(path!); const sha256 = createHash("sha256").update(content).digest("hex");
  const response = await pending; expect(response.headers()["x-export-sha256"]).toBe(sha256);
  expect(response.headers()["cache-control"]).toBe("no-store");
  const proof = JSON.parse(execFileSync("python3", ["-c", privacyScript, path!], { input: JSON.stringify(privateValues), encoding: "utf8" }));
  expect(proof.party_json).toBe(2); expect(proof.verified_checksums).toBeGreaterThan(0);
  const audit = await read(request, "/audit-log?action=export.created&limit=100");
  expect(audit.items.some((row: Json) => row.actor?.id === actorId && row.after?.sha256 === sha256)).toBe(true);
  return { ...proof, sha256, bytes: content.length };
}

test("real model fixture browser verifies native settings, rail, fallback, human history and privacy", async ({ browser, baseURL }) => {
  test.skip(process.env.SCREENING_TOOL_M8_GATE !== "1" || !process.env.PLAYWRIGHT_BASE_URL, "Requires a released fresh packaged M8 fixture runtime.");
  test.setTimeout(420_000);
  const artifactPrefix = process.env.SCREENING_TOOL_MODEL_ARTIFACT_PREFIX ?? "milestone-8-real-model";
  expect(artifactPrefix, "Use a bounded filename prefix without path separators.").toMatch(/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$/);
  const origin = new URL(baseURL!).origin;
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const browserErrors: string[] = []; const externalOrigins: string[] = [];
  function observe(target: Page) {
    target.on("pageerror", error => browserErrors.push(error.name));
    target.on("request", request => { const url = new URL(request.url()); if (url.origin !== origin) externalOrigins.push(url.origin); });
  }
  observe(page);
  const setup = await read(context.request, "/setup");
  expect(setup.needs_setup, "Use a fresh isolated volume; partial runs are never resumed.").toBe(true);
  await page.goto("/");
  await page.getByLabel("Full name").fill("Fixture Model Admin");
  await page.getByLabel("Email address").fill(ADMIN_EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page.getByText(setup.disclaimer, { exact: true })).toBeVisible();
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Create workspace", exact: true }).click();
  await expect(page.getByRole("navigation", { name: "Main navigation", exact: true })).toBeVisible();
  const session = await read(context.request, "/auth/me");
  const headers = { "X-CSRF-Token": session.csrf_token, Origin: origin };
  async function phase(value: string) {
    const response = await context.request.post("/_fixture/model/phase", { headers, data: { phase: value } });
    expect(response.status()).toBe(200);
  }
  async function remote() { const response = await context.request.get("/_fixture/model/status"); expect(response.ok()).toBe(true); return response.json(); }
  const seeded = await read(context.request, "/csl/status"); expect(seeded.current_version.entry_count).toBe(301);
  const fixtureStatus = await remote(); expect(fixtureStatus).toMatchObject({ validation: "synthetic-model-contract", synthetic_transport_installed: true });
  const imported = await importNative(page, context.request, csvBook(), false);
  expect(imported).toMatchObject({ added: 2, changed: 0, deleted: 0 });
  const parties = (await read(context.request, "/parties?limit=100")).items;
  const target = parties.find((row: Json) => row.external_id === "fp"); expect(Boolean(target)).toBe(true);
  const sources = await read<Json[]>(context.request, "/sources"); expect(sources).toHaveLength(1);
  const initialQueue = await read(context.request, "/queue");
  const initial = initialQueue.items.find((row: Json) => row.party_id === target.id && row.entry_name === TARGET);
  expect(Boolean(initial)).toBe(true); expect(initial.bucket).toBe("likely");
  const initialDetail = await read(context.request, `/hits/${initial.id}`); expect(initialDetail.model_prob).toBeNull();
  const originalSnapshots = { party: digest(initialDetail.party_snapshot), entry: digest(initialDetail.entry_snapshot) };
  await page.goto("/settings");
  await expect(provider(page)).toHaveValue("none"); await expect(testButton(page)).toBeDisabled();
  expect(await read<ModelConfig>(context.request, "/settings/model")).toMatchObject({ provider: "none", generation: 0, w_det: 1, w_model: 0 });
  await phase("valid");
  await provider(page).selectOption("laya");
  await panel(page).getByLabel("Laya endpoint", { exact: true }).fill("http://127.0.0.1:8000");
  await panel(page).getByLabel("New Laya API key", { exact: true }).fill(LAYA_KEY);
  await expect(testButton(page)).toBeDisabled();
  const laya = await saveNative(page, context.request);
  expect(laya.result).toMatchObject({ provider: "laya", generation: 1, laya_api_key_configured: true, w_det: 1, w_model: 0 });
  await expect(panel(page).getByLabel("New Laya API key", { exact: true })).toHaveValue("");
  const connected = await testNative(page);
  expect(connected).toMatchObject({ status: "connected", provider: "laya", generation: 1, requested_model: "multilingual", returned_model: "multilingual" });
  expect(connected.model_prob).toBe(0);
  expect(connected.usage.input_tokens).toBeGreaterThan(0); expect(connected.usage.input_tokens).toBeLessThanOrEqual(1024);
  await expect(fact(page, "Model probability")).toHaveText(String(connected.model_prob));
  await expect(fact(page, "Input tokens")).toHaveText(String(connected.usage.input_tokens));
  const factsBeforeDraft = await read<ModelConfig>(context.request, "/settings/model");
  await panel(page).getByLabel("New Laya API key", { exact: true }).fill("synthetic unsaved draft");
  await expect(testButton(page)).toBeDisabled();
  expect(await read<ModelConfig>(context.request, "/settings/model")).toEqual(factsBeforeDraft);
  await panel(page).getByLabel("New Laya API key", { exact: true }).fill("");
  await expect(testButton(page)).toBeEnabled();

  // Real optimistic conflict: another authorized caller changes saved generation.
  await panel(page).getByLabel("Deterministic weight", { exact: true }).fill("0.8");
  await panel(page).getByLabel("Model weight", { exact: true }).fill("0.2");
  const concurrent = await context.request.patch("/api/v1/settings/model", { headers, data: { expected_generation: 1, w_det: 0.75, w_model: 0.25 } });
  expect(concurrent.status()).toBe(200);
  const concurrentSaved = await concurrent.json(); await settledTask(context.request, concurrentSaved.pending_screening_task_id);
  const staleResponse = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/settings/model" && response.request().method() === "PATCH");
  await save(page).click(); expect((await staleResponse).status()).toBe(409);
  await expect(panel(page).getByRole("button", { name: "Reload configuration", exact: true })).toBeVisible();
  await panel(page).getByRole("button", { name: "Reload configuration", exact: true }).click();
  await expect(panel(page).getByLabel("Deterministic weight", { exact: true })).toHaveValue("0.75");

  await phase("valid");
  await panel(page).getByLabel("Deterministic weight", { exact: true }).fill("0.4");
  await panel(page).getByLabel("Model weight", { exact: true }).fill("0.6");
  const rail = await saveNative(page, context.request);
  const railRow = (await read(context.request, "/queue")).items.find((row: Json) => row.party_id === target.id && row.csl_entry_id === initial.csl_entry_id);
  const railHit = await read(context.request, `/hits/${railRow.id}`);
  expect(railHit.model_prob).toBe(0); expect(railHit.det_score).toBeGreaterThanOrEqual(90);
  expect(railHit.final_confidence).toBeLessThan(65); expect(railHit.bucket).toBe("likely");
  expect(railHit.explanation.adjudication).toMatchObject({ provider: "laya", model_prob: 0, error_code: null });
  expect(rail.run!.settings_snapshot.model.generation).toBe(rail.result.generation);
  await phase("invalid_response");
  const fallbackResponse = await context.request.post("/api/v1/screen", { headers, data: { all: true }, timeout: 90_000 });
  expect(fallbackResponse.status()).toBe(200); const fallbackRun = await fallbackResponse.json();
  expect(fallbackRun.model_fallbacks.length).toBeGreaterThan(0);
  const file = await read(context.request, `/parties/${target.id}`);
  const fallbackHit = file.events.find((event: Json) => event.run.id === fallbackRun.id).hits.find((hit: Json) => hit.csl_entry_id === initial.csl_entry_id);
  expect(fallbackHit.model_prob).toBeNull(); expect(fallbackHit.final_confidence).toBe(fallbackHit.det_score); expect(fallbackHit.bucket).toBe("likely");
  expect(fallbackHit.explanation.adjudication.error_code).toBe("invalid_response");
  console.info("Real model browser gate: native saved-provider facts, conflict, probability-zero likely rail and deterministic fallback verified.");

  const inviteResponse = await context.request.post("/api/v1/users/invitations", { headers, data: { email: "model-fixture-reviewer@example.test", role: "reviewer" } });
  expect(inviteResponse.status()).toBe(201); const invitation = await inviteResponse.json();
  const invitationToken = new URL(invitation.invite_url, baseURL).hash.slice("#token=".length);
  const reviewerContext = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1000 } });
  const reviewerPage = await reviewerContext.newPage(); observe(reviewerPage);
  try { await reviewerPage.goto(invitation.invite_url); } catch { throw new Error("Synthetic reviewer invitation navigation failed; private fragment omitted."); }
  await expect.poll(() => new URL(reviewerPage.url()).hash === "").toBe(true);
  await reviewerPage.getByRole("textbox", { name: /name/i }).fill("Fixture Model Reviewer");
  await reviewerPage.getByLabel("Password", { exact: true }).fill(REVIEWER_PASSWORD);
  await reviewerPage.getByRole("button", { name: "Create account", exact: true }).click();
  await expect(reviewerPage.getByRole("navigation", { name: "Main navigation", exact: true })).toBeVisible();
  const reviewer = await read(reviewerContext.request, "/auth/me");
  expect(reviewer.user.role).toBe("reviewer");
  const reviewerHeaders = { "X-CSRF-Token": reviewer.csrf_token, Origin: origin };
  expect((await reviewerContext.request.get("/api/v1/settings/model")).status()).toBe(403);
  expect((await reviewerContext.request.post("/api/v1/settings/model/test", { headers: reviewerHeaders, data: { expected_generation: rail.result.generation } })).status()).toBe(403);
  await reviewerPage.goto(`/queue?hit=${railRow.id}`);
  await reviewerPage.getByRole("textbox", { name: /note/i }).fill(NOTE); await reviewerPage.locator("h1").click();
  const decisionResponse = reviewerPage.waitForResponse(response => response.request().method() === "POST" && new URL(response.url()).pathname === `/api/v1/hits/${railRow.id}/dispositions`);
  await reviewerPage.keyboard.press("f");
  const decided = await decisionResponse; expect(decided.status()).toBe(201); const decision = await decided.json();
  expect(decision.observations_dispositioned).toBeGreaterThanOrEqual(2);
  expect(decision.disposition).toMatchObject({ kind: "false_positive", note: NOTE, actor: { id: reviewer.user.id } });
  const reviewAudit = await read(reviewerContext.request, "/audit-log?action=review.disposition&limit=100");
  const group = reviewAudit.items.filter((row: Json) => row.after?.queue_hit_id === railRow.id);
  expect(group).toHaveLength(decision.observations_dispositioned);
  expect(group.some((row: Json) => row.target_id === decision.disposition.id)).toBe(true);
  for (const row of group) {
    expect(row.after).toMatchObject({ actor: { id: reviewer.user.id }, kind: "false_positive", party_hash: initialDetail.party_hash, entry_hash: initialDetail.entry_hash });
    const hit = await read(reviewerContext.request, `/hits/${row.after.hit_id}`);
    expect(digest(hit.party_snapshot)).toBe(originalSnapshots.party); expect(digest(hit.entry_snapshot)).toBe(originalSnapshots.entry);
  }
  const vendor = parties.find((row: Json) => row.external_id === "confirmed");
  const vendorRow = (await read(reviewerContext.request, "/queue")).items.find((row: Json) => row.party_id === vendor.id && row.csl_entry_id === initial.csl_entry_id);
  expect(Boolean(vendorRow)).toBe(true);
  await reviewerPage.goto(`/queue?hit=${vendorRow.id}`);
  await reviewerPage.getByRole("textbox", { name: /note/i }).fill("Synthetic fixture confirmed-match decision");
  await reviewerPage.locator("h1").click();
  const confirmedResponse = reviewerPage.waitForResponse(response => response.request().method() === "POST" && new URL(response.url()).pathname === `/api/v1/hits/${vendorRow.id}/dispositions`);
  await reviewerPage.keyboard.press("m");
  const confirmedResult = await confirmedResponse; expect(confirmedResult.status()).toBe(201);
  const confirmedDecision = await confirmedResult.json();
  expect(confirmedDecision.disposition).toMatchObject({ kind: "confirmed_match", actor: { id: reviewer.user.id } });
  expect(confirmedDecision.observations_dispositioned).toBeGreaterThanOrEqual(2);
  const beforeRepeat = await remote();
  const repeatResponse = await context.request.post("/api/v1/screen", { headers, data: { all: true }, timeout: 90_000 });
  expect(repeatResponse.status()).toBe(200); const repeatRun = await repeatResponse.json(); expect(repeatRun.hits_suppressed).toBeGreaterThan(0);
  const afterRepeat = await remote(); expect(afterRepeat.calls).toBe(beforeRepeat.calls);
  expect((await read(context.request, "/queue")).items.filter((row: Json) => row.party_id === target.id && row.csl_entry_id === initial.csl_entry_id)).toHaveLength(0);
  await phase("valid");
  const changed = await importNative(page, context.request, csvBook(true), true);
  expect(changed).toMatchObject({ added: 0, changed: 1, unchanged: 1, deleted: 0 });
  const reopenedRow = (await read(reviewerContext.request, "/queue")).items.find((row: Json) => row.party_id === target.id && row.csl_entry_id === initial.csl_entry_id);
  const reopened = await read(reviewerContext.request, `/hits/${reopenedRow.id}`);
  expect(reopened.party_snapshot.party_type).toBe("individual");
  expect(reopened.party_hash).not.toBe(initialDetail.party_hash); expect(reopened.entry_hash).toBe(initialDetail.entry_hash);
  expect(reopened.prior_disposition).toMatchObject({ kind: "false_positive", note: NOTE, actor: { id: reviewer.user.id } });
  expect(group.some((row: Json) => row.target_id === reopened.prior_disposition.id)).toBe(true);
  await reviewerPage.goto(`/queue?hit=${reopenedRow.id}`);
  const earlier = reviewerPage.getByRole("region", { name: "Earlier decision on this pairing", exact: true });
  await expect(earlier).toContainText("Fixture Model Reviewer"); await expect(earlier).toContainText(NOTE);
  const screenshots = join(homedir(), "Desktop/screenshots/screening-tool"); const artifacts = join(homedir(), "Desktop/artifacts/screening-tool");
  await mkdir(screenshots, { recursive: true }); await mkdir(artifacts, { recursive: true });
  const visibleChanges = earlier.locator(".rv-changes");
  await expect(visibleChanges).toContainText("Party type");
  await expect(visibleChanges).toContainText("company");
  await expect(visibleChanges).toContainText("individual");
  await expect(visibleChanges).toContainText(TARGET);
  const rawDisclosure = earlier.locator("details.rv-raw");
  await expect(rawDisclosure).toHaveCount(1);
  await expect(rawDisclosure).toHaveJSProperty("open", false);
  const rawSummary = rawDisclosure.locator(":scope > summary");
  await expect(rawSummary).toContainText("Your record · Raw source data changed");
  const decisionPosts: string[] = [];
  const observeDecision = (request: import("@playwright/test").Request) => {
    if (request.method() === "POST" && /\/hits\/[^/]+\/dispositions$/.test(new URL(request.url()).pathname))
      decisionPosts.push(request.method());
  };
  reviewerPage.on("request", observeDecision);
  await earlier.scrollIntoViewIfNeeded();
  await reviewerPage.screenshot({ path: join(screenshots, `${artifactPrefix}-reviewer-history.png`) });
  await rawSummary.focus();
  await reviewerPage.keyboard.press("Enter");
  await expect(rawDisclosure).toHaveJSProperty("open", true);
  const rawChange = reopened.prior_disposition.what_changed.party.raw;
  const rawBlocks = rawDisclosure.locator("pre.rv-json");
  expect(await rawBlocks.nth(0).textContent()).toBe(JSON.stringify(rawChange.before, null, 2));
  expect(await rawBlocks.nth(1).textContent()).toBe(JSON.stringify(rawChange.after, null, 2));
  expect(await rawBlocks.first().evaluate(node => node.clientHeight)).toBeLessThanOrEqual(400);
  await rawSummary.scrollIntoViewIfNeeded();
  await reviewerPage.screenshot({ path: join(screenshots, `${artifactPrefix}-reviewer-raw-expanded.png`) });
  await reviewerPage.keyboard.press("Space");
  await expect(rawDisclosure).toHaveJSProperty("open", false);
  await reviewerPage.setViewportSize({ width: 390, height: 844 });
  await expect(earlier).toContainText(NOTE);
  await expect(visibleChanges).toContainText("Party type");
  expect(await reviewerPage.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await earlier.scrollIntoViewIfNeeded();
  await reviewerPage.screenshot({ path: join(screenshots, `${artifactPrefix}-reviewer-history-mobile.png`) });
  expect(decisionPosts).toEqual([]);
  reviewerPage.off("request", observeDecision);
  await reviewerPage.setViewportSize({ width: 1440, height: 1000 });

  await page.goto("/settings");
  await provider(page).selectOption("jev");
  await expect(panel(page).getByText(NOTICE, { exact: true })).toBeVisible();
  await panel(page).getByLabel("New Jev API key", { exact: true }).fill(JEV_KEY);
  const beforeAck = await read<ModelConfig>(context.request, "/settings/model");
  await save(page).click();
  await expect(panel(page).locator("form").getByRole("alert")).toContainText("Nothing was sent");
  expect(await read<ModelConfig>(context.request, "/settings/model")).toEqual(beforeAck);
  await expect(panel(page).getByLabel("New Jev API key", { exact: true })).toHaveValue("");
  await panel(page).getByLabel("New Jev API key", { exact: true }).fill(JEV_KEY);
  await panel(page).getByRole("checkbox", { name: /I have read the notice above/ }).check();
  const jev = await saveNative(page, context.request);
  expect(jev.result).toMatchObject({ provider: "jev", jev_api_key_configured: true, hosted_acknowledgment_valid: true });
  await expect(panel(page).getByLabel("New Jev API key", { exact: true })).toHaveValue("");
  const hostedTest = await testNative(page); expect(hostedTest).toMatchObject({ status: "connected", provider: "jev", requested_model: "jev-1.13.0", returned_model: "jev-1.13.0" });
  const acknowledgments = await read(context.request, "/audit-log?action=settings.model_hosted_acknowledged&limit=100");
  expect(acknowledgments.items.some((row: Json) => row.actor?.id === session.user.id && row.after?.notice === NOTICE && row.after?.acknowledgment === NOTICE)).toBe(true);
  await provider(page).selectOption("none");
  await panel(page).getByRole("checkbox", { name: "Clear the saved Jev API key", exact: true }).check();
  await panel(page).getByRole("checkbox", { name: "Clear the saved Laya API key", exact: true }).check();
  const disabled = await saveNative(page, context.request);
  expect(disabled.result).toMatchObject({ provider: "none", jev_api_key_configured: false, laya_api_key_configured: false });
  await expect(testButton(page)).toBeDisabled();
  await provider(page).selectOption("laya");
  await expect(panel(page).getByLabel("New Laya API key", { exact: true })).toHaveValue("");
  await panel(page).getByLabel("New Laya API key", { exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(screenshots, `${artifactPrefix}-settings-cleared.png`) });
  await provider(page).selectOption("none");
  const privateValues = [LAYA_KEY, JEV_KEY, ADMIN_PASSWORD, REVIEWER_PASSWORD, session.csrf_token, reviewer.csrf_token, invitationToken, ...(await context.cookies()).map(cookie => cookie.value), ...(await reviewerContext.cookies()).map(cookie => cookie.value)];
  const zip = await auditZip(page, context.request, session.user.id, privateValues);
  const oldHit = await read(context.request, `/hits/${initial.id}`);
  expect(digest(oldHit.party_snapshot)).toBe(originalSnapshots.party); expect(digest(oldHit.entry_snapshot)).toBe(originalSnapshots.entry);
  expect(browserErrors).toEqual([]); expect(externalOrigins).toEqual([]);
  const finalRemote = await remote(); expect(finalRemote.calls).toBeGreaterThan(0); expect(finalRemote.writer_checks).toBe(finalRemote.calls);
  const evidence = { gate: "milestone-8-real-model-browser", fixture_only: true, fresh_complete_gate: true, live_jev: "unverified", actual_local_laya: "separate coordinator gate", admin_id: session.user.id, reviewer_id: reviewer.user.id, source_id: sources[0].id, party_count: 2, initial_hit_id: initial.id, rail_hit_id: railRow.id, reopened_hit_id: reopenedRow.id, generation_conflict_http: 409, model_generations: [laya.result.generation, rail.result.generation, jev.result.generation, disabled.result.generation], synthetic_test: { laya: connected, jev: hostedTest }, rail: { det_score: railHit.det_score, probability: railHit.model_prob, confidence: railHit.final_confidence, bucket: railHit.bucket }, fallback: { run_id: fallbackRun.id, hit_id: fallbackHit.id, code: fallbackHit.explanation.adjudication.error_code, model_prob: fallbackHit.model_prob, confidence: fallbackHit.final_confidence }, genuine_human_group: { decision_id: decision.disposition.id, observation_count: group.length, actor_id: reviewer.user.id, confirmed_decision_id: confirmedDecision.disposition.id, confirmed_observations: confirmedDecision.observations_dispositioned }, four_hash_reopening: true, snapshot_sha256: originalSnapshots, suppressed_repeat_no_new_model_call: true, native_raw_disclosure: { default_closed: true, exact_before_after: true, bounded: true, keyboard_no_decision_post: true, business_changes_visible: true, mobile_no_horizontal_overflow: true }, write_only_keys_cleared: true, exact_hosted_acknowledgment: true, private_drafts_not_tested: true, zip, browser_errors: browserErrors, external_browser_origins: externalOrigins, provider_writer_checks: finalRemote.writer_checks, provider_calls: finalRemote.calls };
  await writeFile(join(artifacts, `${artifactPrefix}-browser.json`), JSON.stringify(evidence, null, 2) + "\n");
  console.info("Real model browser gate: native Reviewer history, synthetic-only hosted acknowledgment, key clearing and ZIP privacy verified; safe evidence saved.");
  await reviewerContext.close(); await context.close();
});

// The checker is part of the gate's evidence: prove its audit-only exception
// cannot admit a plaintext key, a marker in party data or a corrupt manifest.
// This case uses temporary synthetic archives and makes no API/runtime request.
test("model ZIP checker rejects plaintext keys, wrong scopes and incomplete or corrupt checksums", () => {
  const proofScript = String.raw`
import hashlib,json,tempfile,zipfile,subprocess,sys
checker=sys.stdin.read()
valid={'party':{'name':'Synthetic test'},'hit':{'entry_snapshot':{'addresses':[{'country':'US','state':'MA'},{'country':'US','state':None},{'country':'US','state':''}]}},'audit_log':[{'after':{'jev_api_key':'[redacted]','laya_api_key':None}}]}
cases=[
 ('valid-original-audit-markers',valid,{},True,None),
 ('plaintext-model-key',{'audit_log':[{'after':{'jev_api_key':'private'}}]},{},False,None),
 ('model-marker-outside-audit',{'party':{'jev_api_key':'[redacted]'}},{},False,None),
 ('fake-before-wrapper',{'party':{'before':{'laya_api_key':'[redacted]'}}},{},False,None),
 ('wrong-audit-scope',{'audit_log':[{'laya_api_key':'[redacted]'}]},{},False,None),
 ('public-source-private-marker',{}, {'credentials_enc':'[redacted]'},False,None),
 ('known-private-value',{'party':{'name':'synthetic-private-needle'}},{},False,None),
 ('oauth-field',{'party':{'authorization_code':None}},{},False,None),
 ('opaque-state',{'party':{'state':'opaque'}},{},False,None),
 ('outside-address-null-state',{'party':{'state':None}},{},False,None),
 ('outside-address-empty-state',{'party':{'state':''}},{},False,None),
 ('address-control-character',{'party':{'addresses':[{'state':'MA\n'}]}},{},False,None),
 ('address-oversized-state',{'party':{'addresses':[{'state':'x'*201}]}},{},False,None),
 ('address-boolean-state',{'party':{'addresses':[{'state':False}]}},{},False,None),
 ('address-integer-state',{'party':{'addresses':[{'state':1}]}},{},False,None),
 ('audit-state',{'audit_log':[{'after':{'state':'[redacted]'}}]},{},False,None),
 ('source-state',{}, {'state':'MA'},False,None),
 ('wrong-address-state-type',{'party':{'addresses':[{'state':{'token':'opaque'}}]}},{},False,None),
 ('state-wrapper-impostor',{'party':{'addresses':{'state':'MA'}}},{},False,None),
 ('wrong-hash',valid,{},False,'hash'),
 ('wrong-size',valid,{},False,'size'),
 ('incomplete-manifest',valid,{},False,'missing'),
]
for label,payload,source,good,corrupt in cases:
 with tempfile.TemporaryDirectory() as tmp:
  path=tmp+'/synthetic.zip'
  members={'parties/synthetic.json':json.dumps(payload).encode(),'sources.jsonl':json.dumps({'id':'synthetic-source',**source}).encode()}
  manifest={name:{'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)} for name,data in members.items()}
  if corrupt=='hash':manifest['parties/synthetic.json']['sha256']='0'*64
  if corrupt=='size':manifest['parties/synthetic.json']['bytes']+=1
  if corrupt=='missing':manifest.pop('sources.jsonl')
  with zipfile.ZipFile(path,'w') as archive:
   for name,data in members.items():archive.writestr(name,data)
   archive.writestr('manifest.json',json.dumps({'files':manifest}))
  result=subprocess.run(['python3','-c',checker,path],input=json.dumps(['synthetic-private-needle']),text=True,capture_output=True)
  assert (result.returncode==0)==good,label
  if good:assert json.loads(result.stdout)['geographic_state_fields_checked']==3
print(json.dumps({'cases':len(cases),'strict_private_scope_and_checksum_validation':True}))
`;
  const proof = JSON.parse(execFileSync("python3", ["-c", proofScript], { input: privacyScript, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] }));
  expect(proof).toEqual({ cases: 22, strict_private_scope_and_checksum_validation: true });
});
