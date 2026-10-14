import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

// Run alone on the coordinator's fresh fixture-only packaged application server.
// SCREENING_TOOL_M6_GATE=1 PLAYWRIGHT_BASE_URL=http://localhost:8011
// npm test -- tests/real-qbo.spec.ts --workers=1
// No API interception, provider navigation, runtime startup, Docker or DB edits.
// The only vendor substitute is the server's process-memory fixed MockTransport.
test.use({ trace: "off", video: "off" });
// Stateful fresh-instance evidence must never be replayed after a partial failure.
test.describe.configure({ retries: 0 });

type Party = { id: string; external_id: string; name: string; record_hash: string; role: string; deleted_at: string | null; raw: unknown };
type Operation = { id: string; status: string; error_code: string | null; outcome: Record<string, unknown> | null };
const SOURCE_LABEL = "Synthetic QBO browser fixture";
const ADMIN_EMAIL = "qbo-fixture-admin@example.test";
const ADMIN_PASSWORD = "Synthetic QBO gate password2026!";
const CUSTOMER_NAME = "Example Manufacturing 42";
const REVIEWER_PASSWORD = "Synthetic QBO reviewer password2026!";
const fixtureApp = { client_id: "fixture-client", client_secret: "fixture-secret", verifier_token: "fixture-verifier" };

async function read<T = Record<string, any>>(request: APIRequestContext, path: string): Promise<T> {
  const response = await request.get(`/api/v1${path}`);
  expect(response.ok(), `Read failed for ${path.split("?")[0]}`).toBe(true);
  return response.json() as Promise<T>;
}
async function top(page: Page) {
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
}
function fingerprints(parties: Party[]) {
  return parties.map(party => ({ id: party.id, external_id: party.external_id, name: party.name, record_hash: party.record_hash, deleted_at: party.deleted_at, raw: party.raw })).sort((a, b) => a.id.localeCompare(b.id));
}
async function finished(request: APIRequestContext, id: string) {
  let last: Operation | undefined;
  await expect.poll(async () => {
    last = await read<Operation>(request, `/monitoring/operations/${encodeURIComponent(id)}`);
    return ["queued", "running"].includes(last.status) ? "active" : "settled";
  }, { timeout: 90_000, intervals: [100, 250, 500, 1000] }).toBe("settled");
  return last!;
}

const fixtureTokens = ["access", "refresh"].flatMap(kind => ["initial", "rotated"].map(stage => `synthetic-qbo-gate-${kind}-${stage}`));
const privacyScript = `
import hashlib,json,re,sys,zipfile
path=sys.argv[1]
needles=[s.encode() for s in json.loads(sys.stdin.read()) if s]
credential_keys={"client_id","client_secret","verifier_token","access_token","refresh_token","credentials_enc","password","password_hash","csrf_token","session_token","token_hash","api_key","smtp_password","smtp_username"}
oauth_keys={"state_hash","authorization_code","code","oauth_state","raw_state","authorization_url"}
markers=0
json_documents=0
jsonl_rows=0
def inspect(value,trail):
    global markers
    if isinstance(value,dict):
        for key,item in value.items():
            if key in oauth_keys: raise RuntimeError("OAuth field present in export")
            if key=='state' and not re.search(r"(?:^|[.])addresses\\[\\d+\\]$",trail): raise RuntimeError("OAuth state field present in export")
            if key in credential_keys:
                audit=(".audit_log[" in trail or trail.startswith("audit.jsonl[")) and (".before" in trail or ".after" in trail)
                if not audit or (item is not None and item!="[redacted]"): raise RuntimeError("Private credential field outside exact redacted audit evidence")
                markers+=1
            inspect(item,trail+'.'+key)
    elif isinstance(value,list):
        for index,item in enumerate(value): inspect(item,trail+'['+str(index)+']')
with zipfile.ZipFile(path) as archive:
    for name in archive.namelist():
        data=archive.read(name)
        if any(needle in data for needle in needles): raise RuntimeError("Private credential value found in export")
        if name.endswith('.json'):
            inspect(json.loads(data),name)
            json_documents+=1
        elif name.endswith('.jsonl'):
            for index,line in enumerate(data.splitlines()):
                row=json.loads(line)
                if name=='sources.jsonl' and (credential_keys|oauth_keys|{'state','cursor','last_error'}).intersection(row): raise RuntimeError("Private field present in public source export")
                inspect(row,name+'['+str(index)+']')
                jsonl_rows+=1
    index=json.loads(archive.read('manifest.json'))['files']
    for name,metadata in index.items():
        if hashlib.sha256(archive.read(name)).hexdigest()!=metadata['sha256']: raise RuntimeError("Export checksum mismatch")
    print(json.dumps({"members":len(archive.namelist()),"verified_checksums":len(index),"party_json":sum(n.startswith('parties/') and n.endswith('.json') for n in archive.namelist()),"json_documents_checked":json_documents,"jsonl_rows_checked":jsonl_rows,"exact_audit_redactions_checked":markers,"private_values_absent":True,"oauth_fields_absent":True,"public_source_private_fields_absent":True}))
`;

async function auditZip(page: Page, request: APIRequestContext, actorId: string, privateValues: (string | null)[]) {
  await page.goto("/audit");
  const response = page.waitForResponse(result => new URL(result.url()).pathname === "/api/v1/exports/full.zip" && result.ok());
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Full audit ZIP", exact: true }).click()]);
  const path = await download.path();
  const bytes = await readFile(path!);
  const exported = await response;
  expect(exported.headers()["cache-control"]).toBe("no-store");
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  expect(exported.headers()["x-export-sha256"]).toBe(sha256);
  const privacy = JSON.parse(execFileSync("python3", ["-c", privacyScript, path!], { input: JSON.stringify(privateValues), encoding: "utf8" }));
  expect(privacy.party_json).toBe(2); expect(privacy.verified_checksums).toBeGreaterThan(0);
  expect(privacy.jsonl_rows_checked).toBeGreaterThan(0);
  const audit = await read(request, "/audit-log?action=export.created&limit=100");
  expect(audit.items.some((item: Record<string, any>) => item.actor?.id === actorId && item.after?.sha256 === sha256)).toBe(true);
  return { bytes: bytes.length, sha256, ...privacy };
}

test("real QBO fixture browser gate verifies OAuth, scoped sync, human history, roles, failure atomicity and disconnect", async ({ browser, baseURL }) => {
  test.skip(process.env.SCREENING_TOOL_M6_GATE !== "1" || !process.env.PLAYWRIGHT_BASE_URL || process.env.SCREENING_TOOL_M6_EXPORT_ONLY === "1", "Requires an explicitly released fresh fixture-only QBO application server.");
  test.setTimeout(360_000);
  const resumeInitial = process.env.SCREENING_TOOL_M6_RESUME_INITIAL === "1";
  const origin = new URL(baseURL!).origin;
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const browserErrors: string[] = [];
  const externalOrigins: string[] = [];
  const checkLocal = (request: { url(): string }) => { const url = new URL(request.url()); if (url.origin !== origin) externalOrigins.push(url.origin); };
  page.on("request", checkLocal);
  page.on("pageerror", error => browserErrors.push(error.name));
  const setup = await read(context.request, "/setup");
  expect(setup.needs_setup, "Fresh execution requires an isolated empty database; resume is explicit and preserves earlier evidence.").toBe(!resumeInitial);
  await page.goto("/");
  if (!resumeInitial) {
    await page.getByLabel("Full name").fill("Fixture QBOAdmin");
    await page.getByLabel("Email address").fill(ADMIN_EMAIL);
    await page.getByLabel("Password", { exact: true }).fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.getByText(setup.disclaimer, { exact: true })).toBeVisible();
    await page.getByRole("checkbox").check();
    await page.getByRole("button", { name: "Create workspace" }).click();
  } else {
    await page.getByLabel("Email address").fill(ADMIN_EMAIL);
    await page.getByLabel("Password", { exact: true }).fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
  }
  await expect(page.getByRole("navigation", { name: "Main navigation" })).toBeVisible();
  const session = await read(context.request, "/auth/me");
  const headers = { "X-CSRF-Token": session.csrf_token, Origin: origin };
  const seeded = await read(context.request, "/csl/status");
  expect(seeded.current_version.entry_count).toBeGreaterThan(0);
  console.info(`Real QBO browser gate: normal Admin ${resumeInitial ? "sign-in to preserved initial data" : "setup"} and seeded CSL verified.`);

  await page.goto("/settings");
  const emailSettings = page.getByRole("region", { name: "Email notifications", exact: true });
  if (!resumeInitial) {
    await emailSettings.getByLabel("Public base URL", { exact: true }).fill(origin);
    await emailSettings.getByRole("button", { name: "Save notification settings" }).click();
    await expect(emailSettings.getByRole("status").filter({ hasText: "Notification settings saved." })).toBeVisible();
  } else await expect(emailSettings.getByLabel("Public base URL", { exact: true })).toHaveValue(origin);
  const app = page.getByRole("region", { name: "QuickBooks app settings", exact: true });
  await expect(app.getByRole("radio", { name: "Sandbox", exact: true })).toBeChecked();
  if (!resumeInitial) {
    await app.getByLabel("New client ID", { exact: true }).fill(fixtureApp.client_id);
    await app.getByLabel("New client secret", { exact: true }).fill(fixtureApp.client_secret);
    await app.getByLabel("New webhook verifier token", { exact: true }).fill(fixtureApp.verifier_token);
    await app.getByRole("button", { name: "Save sandbox app settings" }).click();
    await expect(app.getByRole("status").filter({ hasText: "Sandbox app settings saved." })).toBeVisible();
  }
  for (const label of ["New client ID", "New client secret", "New webhook verifier token"])
    await expect(app.getByLabel(label, { exact: true })).toHaveValue("");
  const flags = await read(context.request, "/settings/connectors/qbo");
  expect(flags.sandbox).toEqual({ client_id_configured: true, client_secret_configured: true, verifier_token_configured: true, configured: true });
  expect(flags.production.configured).toBe(false);
  expect(Object.keys(flags).sort()).toEqual(["production", "redirect_uri", "sandbox", "webhook_urls"]);
  expect(JSON.stringify(flags).includes(fixtureApp.client_secret)).toBe(false);
  expect(flags.redirect_uri).toBe(`${origin}/api/v1/connectors/qbo/oauth/callback`);

  await page.goto("/sources");
  let sourceId: string;
  let state: string | null = null;
  const sourceCard = page.getByRole("article", { name: SOURCE_LABEL, exact: true });
  const connectionUI = sourceCard.getByRole("region", { name: "QuickBooks connection", exact: true });
  if (!resumeInitial) {
  const add = page.getByRole("region", { name: "Add a QuickBooks source", exact: true });
  await add.getByLabel("Source name", { exact: true }).fill(SOURCE_LABEL);
  await add.getByRole("combobox", { name: "QuickBooks environment" }).selectOption("sandbox");
  await add.getByRole("button", { name: "Add QuickBooks source" }).click();
  await expect(connectionUI.getByText("Not connected", { exact: true })).toBeVisible();
  await expect(connectionUI.getByRole("button", { name: "Connect to QuickBooks", exact: true })).toBeVisible();
  const sources = await read<Array<Record<string, any>>>(context.request, "/sources");
  expect(sources).toHaveLength(1);
  sourceId = sources[0].id as string;
  expect(sources[0]).toMatchObject({ kind: "qbo", environment: "sandbox", connection_status: "configured", record_count: 0 });
  // A real in-page request uses the active browser session and CSRF header.
  // Keep the provider URL and nonce in memory; never open them or print them.
  const authorization = await page.evaluate(async ({ id, csrf }) => {
    const response = await fetch(`/api/v1/sources/${encodeURIComponent(id)}/qbo/oauth/start`, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: "{}" });
    if (!response.ok) throw new Error("Fixture OAuth start failed");
    return response.json();
  }, { id: sourceId, csrf: session.csrf_token });
  const authorizeURL = new URL(authorization.authorization_url);
  expect(authorizeURL.origin).toBe("https://appcenter.intuit.com");
  expect(authorizeURL.pathname).toBe("/connect/oauth2");
  expect(authorizeURL.searchParams.get("response_type")).toBe("code");
  state = authorizeURL.searchParams.get("state");
  expect(Boolean(state)).toBe(true);
  const callbackQuery = new URLSearchParams({ state: state!, code: "fixture-browser-code", realmId: "12345" });
  let callback;
  try { callback = await context.request.get(`/api/v1/connectors/qbo/oauth/callback?${callbackQuery}`, { maxRedirects: 0 }); }
  catch { throw new Error("Fixture callback transport failed; private authorization parameters are omitted."); }
  expect(callback.status()).toBe(303);
  expect(callback.headers()["referrer-policy"]).toBe("no-referrer");
  const returnURL = new URL(callback.headers().location, baseURL);
  expect(returnURL.origin).toBe(origin);
  expect(returnURL.pathname).toBe("/sources");
  expect([...returnURL.searchParams.keys()].every(key => ["source_id", "connection", "connection_error"].includes(key))).toBe(true);
  await page.goto(returnURL.href);
  } else {
    const sources = await read<Array<Record<string, any>>>(context.request, "/sources");
    expect(sources).toHaveLength(1);
    expect(sources[0]).toMatchObject({ label: SOURCE_LABEL, kind: "qbo", environment: "sandbox", connection_status: "connected", record_count: 2 });
    sourceId = sources[0].id;
    expect((await context.request.get("/_fixture/qbo/status")).status()).toBe(200);
  }
  await expect(connectionUI.getByText("Connected", { exact: true })).toBeVisible();
  await expect(connectionUI.getByRole("button", { name: "Full sync", exact: true })).toBeVisible();
  let connection = await read(context.request, `/sources/${sourceId}/qbo/connection`);
  expect(connection).toMatchObject({ status: "connected", environment: "sandbox", realm_id: "12345" });
  expect(Object.keys(connection).some(key => ["access_token", "refresh_token", "credentials_enc"].includes(key))).toBe(false);
  await expect.poll(async () => (await read<Array<Record<string, any>>>(context.request, "/sources"))[0].record_count, { timeout: 90_000 }).toBe(2);
  await expect.poll(async () => (await read(context.request, "/dashboard")).coverage_latest_version, { timeout: 90_000 }).toBe(2);

  const operations: { phase: string; attempt_id: string; status: string; added?: unknown; changed?: unknown; deleted?: unknown; unchanged?: unknown }[] = [];
  async function phase(value: string) {
    const response = await context.request.post("/_fixture/qbo/phase", { headers, data: { phase: value } });
    expect(response.status()).toBe(200);
    expect(await response.json()).toEqual({ phase: value });
  }
  async function parties() { return (await read<{ items: Party[]; total: number }>(context.request, `/parties?source_id=${sourceId}&include_deleted=true&limit=100`)).items; }
  async function sync(mode: "full" | "incremental", phaseName: string, expectedStatus = "success") {
    await page.goto("/sources");
    const button = connectionUI.getByRole("button", { name: mode === "full" ? "Full sync" : "Sync changes", exact: true });
    await expect(button).toBeEnabled();
    const response = page.waitForResponse(result => new URL(result.url()).pathname === `/api/v1/sources/${sourceId}/sync` && result.request().method() === "POST");
    await button.click();
    const queuedResponse = await response;
    expect(queuedResponse.status()).toBe(202);
    const queued = await queuedResponse.json();
    expect(queued.operation).toBe("source_sync"); expect(queued.status).toBe("queued");
    const operation = await finished(context.request, queued.attempt_id);
    expect(operation.status).toBe(expectedStatus);
    const outcome = operation.outcome ?? {};
    if (typeof outcome.pending_task_id === "string" && expectedStatus === "success") {
      await expect.poll(async () => (await read(context.request, `/audit-log?action=screening.task_completed&target_table=screening_tasks&target_id=${outcome.pending_task_id}&limit=100`)).items.some((item: Record<string, any>) => Boolean(item.after?.run_id)), { timeout: 90_000 }).toBe(true);
    }
    operations.push({ phase: phaseName, attempt_id: queued.attempt_id, status: operation.status, ...Object.fromEntries(["added", "changed", "deleted", "unchanged"].filter(key => typeof outcome[key] === "number").map(key => [key, outcome[key]])) });
    const status = connectionUI.getByRole("status", { name: "QuickBooks operation status", exact: true });
    await expect(status).toContainText(expectedStatus === "success" ? "finished." : "ended with status “failed”", { timeout: 30_000 });
    return operation;
  }
  const firstParties = await parties();
  expect(firstParties).toHaveLength(2);
  expect(firstParties.map(party => party.external_id).sort()).toEqual(["Customer:42", "Vendor:42"]);
  const customer = firstParties.find(party => party.external_id === "Customer:42")!;
  const vendor = firstParties.find(party => party.external_id === "Vendor:42")!;
  expect(customer.name).toBe(CUSTOMER_NAME); expect(customer.role).toBe("customer"); expect(vendor.role).toBe("vendor");
  expect(customer.id).not.toBe(vendor.id);
  const unchanged = await sync("full", "initial-repeat");
  expect(unchanged.outcome).toMatchObject({ added: 0, changed: 0, unchanged: 2, deleted: 0, record_count: 2 });
  expect(fingerprints(await parties())).toEqual(fingerprints(firstParties));
  // A public HTTP callback works for local fixtures; webhooks still require HTTPS.
  const webhookResponse = page.waitForResponse(result => new URL(result.url()).pathname === `/api/v1/sources/${sourceId}/qbo/connection` && result.request().method() === "PATCH");
  await connectionUI.getByRole("checkbox", { name: "Receive QuickBooks change notifications (webhooks)" }).click();
  expect((await webhookResponse).status()).toBe(422);
  await expect(connectionUI.getByRole("checkbox", { name: "Receive QuickBooks change notifications (webhooks)" })).not.toBeChecked();
  console.info("Real QBO browser gate: actual OAuth callback, entity namespaces, unchanged sync and webhook HTTPS guard verified.");

  const inviteResponse = await context.request.post("/api/v1/users/invitations", { headers, data: { email: "qbo-fixture-reviewer@example.test", role: "reviewer" } });
  expect(inviteResponse.status()).toBe(201);
  const invite = await inviteResponse.json();
  const invitationToken = new URL(invite.invite_url, baseURL).hash.slice("#token=".length);
  const reviewerContext = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1000 } });
  const reviewerPage = await reviewerContext.newPage();
  const reviewerRequests: string[] = [];
  reviewerPage.on("request", request => reviewerRequests.push(`${request.method()} ${new URL(request.url()).pathname}`));
  reviewerPage.on("pageerror", error => browserErrors.push(error.name));
  reviewerPage.on("request", checkLocal);
  try { await reviewerPage.goto(invite.invite_url); } catch { throw new Error("Fixture invitation could not be opened; private token omitted."); }
  await expect.poll(() => new URL(reviewerPage.url()).hash === "").toBe(true);
  await reviewerPage.getByRole("textbox", { name: /name/i }).fill("Fixture QBO Reviewer");
  await reviewerPage.getByLabel("Password", { exact: true }).fill(REVIEWER_PASSWORD);
  await reviewerPage.getByRole("button", { name: "Create account" }).click();
  await expect(reviewerPage.getByRole("navigation", { name: "Main navigation" })).toBeVisible();
  const reviewer = await read(reviewerContext.request, "/auth/me");
  expect(reviewer.user.role).toBe("reviewer");
  const reviewerHeaders = { "X-CSRF-Token": reviewer.csrf_token, Origin: origin };
  await reviewerPage.goto("/sources");
  const readonlyCard = reviewerPage.getByRole("article", { name: SOURCE_LABEL, exact: true });
  await expect(readonlyCard.getByText("Connected", { exact: true })).toBeVisible();
  await expect(readonlyCard.getByRole("region", { name: "QuickBooks connection" })).toHaveCount(0);
  await expect(readonlyCard.getByRole("button")).toHaveCount(0);
  expect(reviewerRequests.some(path => path.includes("/qbo/connection") || path.includes("/settings/connectors/qbo"))).toBe(false);
  expect((await reviewerContext.request.get("/api/v1/settings/connectors/qbo")).status()).toBe(403);
  expect((await reviewerContext.request.post(`/api/v1/sources/${sourceId}/sync`, { headers: reviewerHeaders, data: { mode: "full" } })).status()).toBe(403);
  expect((await reviewerContext.request.post("/_fixture/qbo/phase", { headers: reviewerHeaders, data: { phase: "changed" } })).status()).toBe(403);
  const queue = await read(reviewerContext.request, "/queue");
  const selected = queue.items.find((hit: Record<string, any>) => hit.party_id === customer.id && hit.entry_name === CUSTOMER_NAME);
  expect(Boolean(selected), "The fixture customer must have a real queued comparison.").toBe(true);
  expect(selected.bucket).toBe("likely");
  const original = await read(reviewerContext.request, `/hits/${selected.id}`);
  await reviewerPage.goto(`/queue?hit=${selected.id}`);
  const note = reviewerPage.getByRole("textbox", { name: /note/i });
  await expect(note).toBeVisible();
  await note.fill("Synthetic QBO fixture false-positive review");
  await reviewerPage.locator("h1").click();
  const decisionResponse = reviewerPage.waitForResponse(result => result.request().method() === "POST" && new URL(result.url()).pathname === `/api/v1/hits/${selected.id}/dispositions`);
  await reviewerPage.keyboard.press("f");
  const decisionResult = await decisionResponse;
  expect(decisionResult.status()).toBe(201);
  const decision = await decisionResult.json();
  expect(decision.disposition.actor.id).toBe(reviewer.user.id);
  expect(decision.disposition.kind).toBe("false_positive");
  expect(decision.hit_id).toBe(selected.id);
  const decisionAudit = await read(reviewerContext.request, "/audit-log?action=review.disposition&limit=100");
  const reviewGroup = decisionAudit.items.filter((row: Record<string, any>) => row.after?.queue_hit_id === selected.id);
  expect(reviewGroup.length).toBe(decision.observations_dispositioned);
  expect(reviewGroup.length).toBeGreaterThan(0);
  const reviewGroupIds = reviewGroup.map((row: Record<string, any>) => row.target_id);
  expect(reviewGroupIds).toContain(decision.disposition.id);
  for (const row of reviewGroup) {
    expect(row.after).toMatchObject({ actor: { id: reviewer.user.id }, note: "Synthetic QBO fixture false-positive review", kind: "false_positive", party_id: original.party_id, csl_entry_id: original.csl_entry_id, party_hash: original.party_hash, entry_hash: original.entry_hash, queue_hit_id: selected.id });
    const covered = await read(reviewerContext.request, `/hits/${row.after.hit_id}`);
    expect(covered.party_snapshot).toEqual(original.party_snapshot);
    expect(covered.entry_snapshot).toEqual(original.entry_snapshot);
    expect(covered.dispositions.some((item: Record<string, any>) => item.id === row.target_id && item.actor?.id === reviewer.user.id && item.kind === "false_positive" && item.note === row.after.note)).toBe(true);
  }
  const repeatedScreen = await context.request.post("/api/v1/screen", { headers, data: { all: true }, timeout: 90_000 });
  expect(repeatedScreen.ok()).toBe(true);
  expect((await repeatedScreen.json()).hits_new).toBe(0);
  expect((await read(context.request, "/queue")).items.filter((hit: Record<string, any>) => hit.party_id === customer.id && hit.csl_entry_id === selected.csl_entry_id)).toHaveLength(0);

  await phase("changed");
  const changedOperation = await sync("incremental", "changed");
  expect(changedOperation.outcome).toMatchObject({ added: 0, changed: 1, deleted: 0 });
  const changedCustomer = (await parties()).find(party => party.id === customer.id)!;
  expect(changedCustomer.external_id).toBe(customer.external_id);
  expect(changedCustomer.name).not.toBe(customer.name);
  expect(changedCustomer.record_hash).not.toBe(customer.record_hash);
  expect(fingerprints([(await parties()).find(party => party.id === vendor.id)!])).toEqual(fingerprints([vendor]));
  const reopenedQueue = await read(reviewerContext.request, "/queue");
  const reopenedRow = reopenedQueue.items.find((hit: Record<string, any>) => hit.party_id === customer.id && hit.csl_entry_id === selected.csl_entry_id);
  expect(Boolean(reopenedRow), "The changed hash must reopen the previously reviewed comparison.").toBe(true);
  const reopened = await read(reviewerContext.request, `/hits/${reopenedRow.id}`);
  expect(reopened.prior_disposition).toMatchObject({ kind: "false_positive", actor: { id: reviewer.user.id }, note: "Synthetic QBO fixture false-positive review" });
  expect(reviewGroupIds).toContain(reopened.prior_disposition.id);
  const priorAudit = reviewGroup.find((row: Record<string, any>) => row.target_id === reopened.prior_disposition.id);
  expect(reopened.prior_disposition.hit_id).toBe(priorAudit.after.hit_id);
  const priorOriginal = await read(reviewerContext.request, `/hits/${reopened.prior_disposition.hit_id}`);
  expect(priorOriginal.party_snapshot).toEqual(original.party_snapshot);
  expect(priorOriginal.entry_snapshot).toEqual(original.entry_snapshot);
  expect(reopened.entry_hash).toBe(original.entry_hash);
  expect(reopened.party_hash).not.toBe(original.party_hash);
  expect((await read(reviewerContext.request, `/hits/${selected.id}`)).party_snapshot).toEqual(original.party_snapshot);
  expect((await read(reviewerContext.request, `/hits/${selected.id}`)).entry_snapshot).toEqual(original.entry_snapshot);
  await reviewerPage.goto(`/queue?hit=${reopenedRow.id}`);
  const prior = reviewerPage.getByRole("region", { name: "Earlier decision on this pairing", exact: true });
  await expect(prior).toContainText("Fixture QBO Reviewer");
  await expect(prior).toContainText("Synthetic QBO fixture false-positive review");
  const scopedRuns = changedOperation.outcome?.run_ids;
  if (Array.isArray(scopedRuns)) for (const runId of scopedRuns) expect((await read(context.request, `/screen/runs/${runId}`)).parties_screened).toBe(1);
  console.info("Real QBO browser gate: real Reviewer decision, same-hash memory and changed-party prior attribution verified.");

  await phase("inactive");
  await sync("incremental", "inactive");
  expect(Boolean((await parties()).find(party => party.id === customer.id)?.deleted_at)).toBe(true);
  await reviewerPage.goto(`/parties/${customer.id}`);
  await expect(reviewerPage.locator(".page-header").getByText("Inactive", { exact: true })).toBeVisible();
  await phase("reactivated");
  await sync("incremental", "reactivated");
  expect((await parties()).find(party => party.id === customer.id)?.deleted_at).toBeNull();
  const beforeMalformed = fingerprints(await parties());
  const sourceBefore = (await read<Array<Record<string, any>>>(context.request, "/sources"))[0];
  await phase("malformed");
  const malformed = await sync("incremental", "malformed", "failed");
  expect(malformed.error_code).toBe("operation_failed");
  expect(fingerprints(await parties())).toEqual(beforeMalformed);
  const sourceAfter = (await read<Array<Record<string, any>>>(context.request, "/sources"))[0];
  expect(sourceAfter.record_count).toBe(sourceBefore.record_count);
  expect(sourceAfter.last_synced_at).toBe(sourceBefore.last_synced_at);
  expect(sourceAfter.last_error).toMatch(/invalid_(cdc|query)_response/);
  await phase("reactivated");
  await sync("incremental", "recovery");
  expect((await read<Array<Record<string, any>>>(context.request, "/sources"))[0].last_error).toBeNull();

  const preserved = fingerprints(await parties());
  async function disconnect(buttonName: string) {
    await connectionUI.getByRole("button", { name: buttonName, exact: true }).click();
    const group = connectionUI.getByRole("group", { name: "Confirm disconnect", exact: true });
    await expect(group).toContainText("Parties, screening history and audit records from this source are kept");
    const response = page.waitForResponse(result => new URL(result.url()).pathname === `/api/v1/sources/${sourceId}/qbo/disconnect` && result.request().method() === "POST");
    await group.getByRole("button", { name: "Confirm disconnect", exact: true }).click();
    const accepted = await response;
    expect(accepted.status()).toBe(202);
    const queued = await accepted.json();
    expect(queued.status).toBe("queued"); expect(queued.connection.status).toBe("disconnecting");
    await finished(context.request, queued.attempt_id);
    await expect.poll(async () => (await read(context.request, `/sources/${sourceId}/qbo/connection`)).status, { timeout: 30_000 }).toBe("disconnected");
    return queued.attempt_id as string;
  }
  await phase("revoke_failed");
  const failedDisconnectId = await disconnect("Disconnect");
  connection = await read(context.request, `/sources/${sourceId}/qbo/connection`);
  expect(connection.revocation_status).toBe("failed");
  await expect(connectionUI.getByText("Intuit did not confirm revoking the authorization.", { exact: true })).toBeVisible();
  expect((await read<Array<Record<string, any>>>(context.request, "/sources"))[0].next_sync_at).toBeNull();
  expect(fingerprints(await parties())).toEqual(preserved);
  await phase("initial");
  const successfulDisconnectId = await disconnect("Retry Intuit revocation");
  expect(successfulDisconnectId).not.toBe(failedDisconnectId);
  connection = await read(context.request, `/sources/${sourceId}/qbo/connection`);
  expect(connection.revocation_status).toBe("revoked");
  await expect(connectionUI.getByText("Intuit confirmed the authorization was revoked.", { exact: true })).toBeVisible();
  expect(fingerprints(await parties())).toEqual(preserved);
  console.info("Real QBO browser gate: inactive/reactivation, malformed atomicity, failed revocation and explicit retry verified.");

  const shots = join(homedir(), "Desktop/screenshots/screening-tool");
  const artifacts = join(homedir(), "Desktop/artifacts/screening-tool");
  await mkdir(shots, { recursive: true }); await mkdir(artifacts, { recursive: true });
  await page.goto("/sources"); await top(page);
  await page.screenshot({ path: join(shots, "milestone-6-real-qbo-sources.png") });
  await sourceCard.screenshot({ path: join(shots, "milestone-6-real-qbo-source-card.png") });
  await reviewerPage.goto(`/parties/${customer.id}`);
  await expect(reviewerPage.getByRole("heading", { level: 1, name: (await parties()).find(party => party.id === customer.id)!.name })).toBeVisible();
  await top(reviewerPage);
  await reviewerPage.screenshot({ path: join(shots, "milestone-6-real-qbo-reviewer-party.png") });
  const exportProof = await auditZip(page, context.request, session.user.id, [...Object.values(fixtureApp), ADMIN_PASSWORD, REVIEWER_PASSWORD, session.csrf_token, reviewer.csrf_token, ...(await context.cookies()).map(cookie => cookie.value), ...(await reviewerContext.cookies()).map(cookie => cookie.value), state, "fixture-browser-code", invitationToken, ...fixtureTokens]);
  const frozenAgain = await read(reviewerContext.request, `/hits/${selected.id}`);
  expect(frozenAgain.party_snapshot).toEqual(original.party_snapshot); expect(frozenAgain.entry_snapshot).toEqual(original.entry_snapshot);
  expect(browserErrors).toEqual([]);
  expect(externalOrigins).toEqual([]);
  const evidence = { gate: "milestone-6-real-qbo-browser", fixture_only: true, resumed_after_initial_webhook_harness: resumeInitial, admin_id: session.user.id, reviewer_id: reviewer.user.id, source_id: sourceId, csl_entries: seeded.current_version.entry_count, party_ids: firstParties.map(party => party.id), entity_id_namespaces: firstParties.map(party => party.external_id).sort(), original_hit_id: selected.id, reopened_hit_id: reopenedRow.id, disposition_id: decision.disposition.id, review_group_ids: reviewGroupIds, observations_dispositioned: decision.observations_dispositioned, prior_disposition_id: reopened.prior_disposition.id, original_snapshots_retained: true, malformed_atomic: true, role_guards: true, local_disconnect_retained_records: true, remote_revocation_retry_succeeded: true, operations, export: exportProof, browser_errors: browserErrors };
  await writeFile(join(artifacts, "milestone-6-real-qbo-browser-evidence.json"), JSON.stringify(evidence, null, 2) + "\n");
  console.info("Real QBO browser gate: export checksum/privacy and immutable snapshots verified; safe evidence saved.");
  await reviewerContext.close(); await context.close();
});


test("real QBO export-only resume verifies preserved actors, decisions and strict export redaction", async ({ browser, baseURL }) => {
  test.skip(process.env.SCREENING_TOOL_M6_GATE !== "1" || process.env.SCREENING_TOOL_M6_EXPORT_ONLY !== "1" || !process.env.PLAYWRIGHT_BASE_URL, "Explicit read/export-only verification of the preserved completed QBO browser scenario.");
  test.setTimeout(90_000);
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  await page.goto("/");
  await page.getByLabel("Email address").fill(ADMIN_EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("navigation", { name: "Main navigation" })).toBeVisible();
  const session = await read(context.request, "/auth/me");
  const users = await read(context.request, "/users");
  expect(users.items).toHaveLength(2);
  const reviewer = users.items.find((user: Record<string, any>) => user.email === "qbo-fixture-reviewer@example.test");
  expect(reviewer).toMatchObject({ role: "reviewer", active: true });
  expect(session.user.role).toBe("admin");
  const sources = await read<Array<Record<string, any>>>(context.request, "/sources");
  expect(sources).toHaveLength(1);
  expect(sources[0]).toMatchObject({ label: SOURCE_LABEL, kind: "qbo", record_count: 2, next_sync_at: null, connection_status: "disconnected" });
  const sourceId = sources[0].id;
  const connection = await read(context.request, `/sources/${sourceId}/qbo/connection`);
  expect(connection).toMatchObject({ status: "disconnected", revocation_status: "revoked", realm_id: "12345" });
  const decisions = await read(context.request, "/audit-log?action=review.disposition&limit=100");
  const reviewed = decisions.items.find((row: Record<string, any>) => row.after?.actor?.id === reviewer.id && row.after?.note === "Synthetic QBO fixture false-positive review");
  expect(Boolean(reviewed)).toBe(true);
  const original = await read(context.request, `/hits/${reviewed.after.hit_id}`);
  expect(original.party_snapshot.name).toBe(CUSTOMER_NAME);
  expect(original.entry_snapshot.name).toBe(CUSTOMER_NAME);
  expect(original.dispositions.some((decision: Record<string, any>) => decision.actor?.id === reviewer.id && decision.kind === "false_positive" && decision.note === reviewed.after.note)).toBe(true);
  const exportProof = await auditZip(page, context.request, session.user.id, [...Object.values(fixtureApp), ADMIN_PASSWORD, REVIEWER_PASSWORD, session.csrf_token, ...(await context.cookies()).map(cookie => cookie.value), "fixture-browser-code", ...fixtureTokens]);
  const after = await read(context.request, `/hits/${original.id}`);
  expect(after.party_snapshot).toEqual(original.party_snapshot);
  expect(after.entry_snapshot).toEqual(original.entry_snapshot);
  const shots = join(homedir(), "Desktop/screenshots/screening-tool");
  const artifacts = join(homedir(), "Desktop/artifacts/screening-tool");
  await mkdir(shots, { recursive: true }); await mkdir(artifacts, { recursive: true });
  await page.goto("/sources"); await top(page);
  await page.screenshot({ path: join(shots, "milestone-6-real-qbo-export-resume-sources.png") });
  await writeFile(join(artifacts, "milestone-6-real-qbo-export-resume-evidence.json"), JSON.stringify({ gate: "milestone-6-real-qbo-export-resume", resume_export_only: true, complete_fresh_gate: false, source_id: sourceId, admin_id: session.user.id, reviewer_id: reviewer.id, original_hit_id: original.id, disposition_id: reviewed.target_id, disconnected_revoked: true, original_snapshots_retained: true, export: exportProof }, null, 2) + "\n");
  console.info(`Real QBO export-only resume: two actual actors, retained decision, disconnected company and ${exportProof.verified_checksums} checksums verified.`);
  await context.close();
});
