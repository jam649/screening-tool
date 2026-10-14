import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

// Fresh coordinator-provisioned fixture-only packaged server; no interception,
// vendor navigation, runtime management, resumes, database edits or retries.
// SCREENING_TOOL_M7_GATE=1 PLAYWRIGHT_BASE_URL=http://localhost:8017
// npm test -- tests/real-bc.spec.ts --workers=1
// Challenge/notification callbacks use actual local TCP; fixed Microsoft HTTP
// is replaced only by the fixture server's process-memory transport.
test.use({ trace: "off", video: "off" });
test.describe.configure({ retries: 0 });
type Party = { id: string; external_id: string; name: string; record_hash: string; role: string; party_type: string; deleted_at: string | null; raw: unknown };
type Operation = { id: string; operation: string; status: string; error_code: string | null; outcome: Record<string, unknown> | null };
const SOURCE_LABEL = "Synthetic BC browser fixture";
const ADMIN_EMAIL = "bc-fixture-admin@example.test";
const ADMIN_PASSWORD = "Synthetic BC gate password2026!";
const REVIEWER_PASSWORD = "Synthetic BC reviewer password2026!";
const CUSTOMER_NAME = "Example Manufacturing 42";
const NOTE = "Synthetic BC fixture false-positive review";
const TENANT = "11111111-1111-1111-1111-111111111111";
const COMPANY = "22222222-2222-2222-2222-222222222222";
const OTHER_COMPANY = "44444444-4444-4444-4444-444444444444";
const ENTITY = "33333333-3333-3333-3333-333333333333";
const ENVIRONMENT = "Sandbox_US";
const APP_ID = "55555555-5555-5555-5555-555555555555";
const APP_SECRET = "synthetic-bc-app-secret";
const BEARER = "synthetic-bc-runtime-bearer";
const PUBLIC_BASE = "https://screening.example.test";
const digest = (value: unknown) => createHash("sha256").update(JSON.stringify(value)).digest("hex");

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
    if (["queued", "running"].includes(last.status)) return "active";
    if (last.operation === "source_sync" && last.status === "success" && !last.outcome?.screening_status) return "active";
    if (last.status === "success" && (last.outcome?.cleanup_status === "failed" || last.outcome?.screening_status === "failed" || last.outcome?.error_code)) return "active";
    return "settled";
  }, { timeout: 90_000, intervals: [100, 250, 500, 1000] }).toBe("settled");
  return last!;
}

const privacyScript = `
import hashlib,json,re,sys,zipfile
path=sys.argv[1]
needles=[s.encode() for s in json.loads(sys.stdin.read()) if s]
credential_keys={"client_id","client_secret","verifier_token","access_token","refresh_token","credentials_enc","password","password_hash","csrf_token","session_token","token_hash","api_key","smtp_password","smtp_username","client_state","clientState","app_secret","bearer_token","token_credentials","cursor"}
oauth_keys={"state_hash","authorization_code","code","oauth_state","raw_state","authorization_url"}
markers=0
json_documents=0
jsonl_rows=0
geographic_states=0
def inspect(value,trail):
    global markers,geographic_states
    if isinstance(value,dict):
        for key,item in value.items():
            if key in oauth_keys: raise RuntimeError("OAuth field present in export")
            if key=='state':
                address=bool(re.search(r"(?:^|[.])addresses\\[\\d+\\]$",trail))
                party_raw=({'id','displayName','country','lastModifiedDateTime'}.issubset(value) and isinstance(item,str) and item in {'MA','WA'})
                if not (address or party_raw): raise RuntimeError("Unexpected state field at "+trail+".state ("+type(item).__name__+")")
                geographic_states+=1
            if key in credential_keys or key.endswith("_enc"):
                audit=(".audit_log[" in trail or trail.startswith("audit.jsonl[")) and bool(re.search(r"[.](?:before|after)(?:[.\\[]|$)",trail))
                if not audit or (item is not None and item!="[redacted]"): raise RuntimeError("Private field at "+trail+"."+key+" ("+type(item).__name__+") outside exact redacted audit evidence")
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
                if name=='sources.jsonl' and ((credential_keys|oauth_keys|{'state','last_error'}).intersection(row) or any(key.endswith("_enc") for key in row)): raise RuntimeError("Private field present in public source export")
                inspect(row,name+'['+str(index)+']')
                jsonl_rows+=1
    index=json.loads(archive.read('manifest.json'))['files']
    for name,metadata in index.items():
        if hashlib.sha256(archive.read(name)).hexdigest()!=metadata['sha256']: raise RuntimeError("Export checksum mismatch")
    print(json.dumps({"members":len(archive.namelist()),"verified_checksums":len(index),"party_json":sum(n.startswith('parties/') and n.endswith('.json') for n in archive.namelist()),"json_documents_checked":json_documents,"jsonl_rows_checked":jsonl_rows,"exact_audit_redactions_checked":markers,"private_values_absent":True,"oauth_fields_absent":True,"public_source_private_fields_absent":True,"geographic_state_fields_checked":geographic_states}))
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
  expect(privacy.geographic_state_fields_checked).toBeGreaterThan(0);
  const audit = await read(request, "/audit-log?action=export.created&limit=100");
  expect(audit.items.some((item: Record<string, any>) => item.actor?.id === actorId && item.after?.sha256 === sha256)).toBe(true);
  return { bytes: bytes.length, sha256, ...privacy };
}

test("real BC fixture browser verifies discovery, durable sync, human history, subscriptions, cleanup and export privacy", async ({ browser, baseURL }) => {
  test.skip(process.env.SCREENING_TOOL_M7_GATE !== "1" || !process.env.PLAYWRIGHT_BASE_URL, "Requires the coordinator's explicitly released fresh BC fixture runtime.");
  test.setTimeout(420_000);
  const origin = new URL(baseURL!).origin;
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const browserErrors: string[] = [];
  const externalOrigins: string[] = [];
  function observe(target: Page) {
    target.on("request", request => { const url = new URL(request.url()); if (url.origin !== origin) externalOrigins.push(url.origin); });
    target.on("pageerror", error => browserErrors.push(error.name));
  }
  observe(page);
  const setup = await read(context.request, "/setup");
  expect(setup.needs_setup, "This gate requires a fresh isolated volume; no partial scenario is resumed.").toBe(true);
  await page.goto("/");
  await page.getByLabel("Full name").fill("Fixture BC Admin");
  await page.getByLabel("Email address").fill(ADMIN_EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText(setup.disclaimer, { exact: true })).toBeVisible();
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Create workspace" }).click();
  await expect(page.getByRole("navigation", { name: "Main navigation" })).toBeVisible();
  const session = await read(context.request, "/auth/me");
  expect(session.user.role).toBe("admin");
  const headers = { "X-CSRF-Token": session.csrf_token, Origin: origin };
  const seeded = await read(context.request, "/csl/status");
  expect(seeded.current_version.entry_count).toBe(301);
  console.info("Real BC browser gate: fresh native Admin setup and actual 301-entry fixture CSL verified.");

  const shots = join(homedir(), "Desktop/screenshots/screening-tool");
  const artifacts = join(homedir(), "Desktop/artifacts/screening-tool");
  await mkdir(shots, { recursive: true }); await mkdir(artifacts, { recursive: true });
  await page.goto("/settings");
  const app = page.getByRole("region", { name: "Business Central app settings", exact: true });
  await app.getByLabel("New application (client) ID", { exact: true }).fill(APP_ID);
  await app.getByLabel("New client secret", { exact: true }).fill(APP_SECRET);
  const appResponse = page.waitForResponse(r => new URL(r.url()).pathname === "/api/v1/settings/connectors/bc" && r.request().method() === "PATCH");
  await app.getByRole("button", { name: "Save Business Central app settings", exact: true }).click();
  expect((await appResponse).status()).toBe(200);
  await expect(app.getByRole("status").filter({ hasText: "Business Central app settings saved." })).toBeVisible();
  for (const label of ["New application (client) ID", "New client secret"]) await expect(app.getByLabel(label, { exact: true })).toHaveValue("");
  const flags = await read(context.request, "/settings/connectors/bc");
  expect(flags).toEqual({ client_id_configured: true, client_secret_configured: true, configured: true, webhook_base_url: null });
  expect(Boolean(JSON.stringify(flags).includes(APP_SECRET) || JSON.stringify(flags).includes(APP_ID))).toBe(false);
  await app.evaluate(element => element.scrollIntoView({ block: "start" }));
  await page.screenshot({ path: join(shots, "milestone-7-real-bc-settings-cleared.png") });

  await page.goto("/sources");
  const add = page.getByRole("region", { name: "Add a Business Central source", exact: true });
  await add.getByLabel("Source name", { exact: true }).fill(SOURCE_LABEL);
  await add.getByLabel("Tenant ID", { exact: true }).fill(TENANT);
  await add.getByLabel("Environment name", { exact: true }).fill(ENVIRONMENT);
  await add.getByRole("button", { name: "Add Business Central source", exact: true }).click();
  const card = page.getByRole("article", { name: SOURCE_LABEL, exact: true });
  const controls = card.getByRole("region", { name: "Business Central connection", exact: true });
  await expect(controls.getByText("Not connected", { exact: true })).toBeVisible();
  let sources = await read<Array<Record<string, any>>>(context.request, "/sources");
  expect(sources).toHaveLength(1);
  const sourceId = sources[0].id as string;
  expect(sources[0]).toMatchObject({ kind: "bc", bc_environment: ENVIRONMENT, connection_status: "configured", record_count: 0, next_sync_at: null });
  const connectionPath = `/sources/${sourceId}/bc/connection`;
  const beforeDiscovery = await read(context.request, connectionPath);
  expect(beforeDiscovery).toMatchObject({ tenant_id: TENANT, bc_environment: ENVIRONMENT, company_id: null, status: "configured", access_token_configured: false });
  const discoveryResponse = page.waitForResponse(r => new URL(r.url()).pathname === `/api/v1/sources/${sourceId}/bc/companies` && r.request().method() === "POST");
  await controls.getByRole("button", { name: "Discover companies", exact: true }).click();
  const discovery = await discoveryResponse;
  expect(discovery.status()).toBe(200);
  const companies = await discovery.json();
  expect(companies.items.map((company: Record<string, any>) => company.id).sort()).toEqual([COMPANY, OTHER_COMPANY].sort());
  expect(companies.items.every((company: Record<string, any>) => Object.keys(company).sort().join(",") === "display_name,id")).toBe(true);
  const choice = controls.getByRole("combobox", { name: "Company to connect", exact: true });
  await expect(choice).toHaveValue("");
  await expect(controls.getByRole("button", { name: "Connect company", exact: true })).toBeDisabled();
  expect((await read(context.request, connectionPath)).company_id).toBeNull();
  expect((await read<Array<Record<string, any>>>(context.request, "/sources"))[0].record_count).toBe(0);
  // Discovery caches app access, but it must not force company activation just
  // to clear that access before an app identity change.
  expect((await read(context.request, connectionPath)).access_token_configured).toBe(true);
  await controls.getByRole("button", { name: "Clear stored app access", exact: true }).click();
  const clearGroup = controls.getByRole("group", { name: "Confirm clear stored app access", exact: true });
  await expect(clearGroup).toContainText("without connecting a company or importing records");
  const clearResponse = page.waitForResponse(r => new URL(r.url()).pathname === `/api/v1/sources/${sourceId}/bc/disconnect` && r.request().method() === "POST");
  await clearGroup.getByRole("button", { name: "Confirm clear stored app access", exact: true }).click();
  const clearAccepted = await clearResponse; expect(clearAccepted.status()).toBe(202);
  const clearQueued = await clearAccepted.json(); expect(clearQueued).toMatchObject({ status: "queued", operation: "source_disconnect" });
  expect((await finished(context.request, clearQueued.attempt_id)).status).toBe("success");
  await expect.poll(async () => (await read(context.request, connectionPath)).access_token_configured).toBe(false);
  expect(await read(context.request, connectionPath)).toMatchObject({ status: "disconnected", company_id: null, access_token_configured: false, cleanup_status: "not_required" });
  expect((await read(context.request, "/parties?limit=100")).total).toBe(0);
  expect((await read<Array<Record<string, any>>>(context.request, "/sources"))[0]).toMatchObject({ record_count: 0, next_sync_at: null });
  expect((await read(context.request, "/settings/connectors/bc")).configured).toBe(true);
  await expect(controls.getByRole("status", { name: "Business Central operation status", exact: true })).toContainText("The stored app access clear finished.");
  const rediscoveryResponse = page.waitForResponse(r => new URL(r.url()).pathname === `/api/v1/sources/${sourceId}/bc/companies` && r.request().method() === "POST");
  await controls.getByRole("button", { name: "Discover companies", exact: true }).click();
  const rediscovery = await rediscoveryResponse; expect(rediscovery.status()).toBe(200);
  expect(await rediscovery.json()).toEqual(companies);
  await expect(choice).toHaveValue("");
  expect((await read(context.request, "/parties?limit=100")).total).toBe(0);
  await choice.selectOption(COMPANY);
  const connectResponse = page.waitForResponse(r => new URL(r.url()).pathname === `/api/v1/sources/${sourceId}/bc/connect` && r.request().method() === "POST");
  await controls.getByRole("button", { name: "Connect company", exact: true }).click();
  const connectedResponse = await connectResponse;
  expect(connectedResponse.status()).toBe(202);
  const connected = await connectedResponse.json();
  expect(connected).toMatchObject({ status: "queued", operation: "source_sync", connection: { company_id: COMPANY, status: "connected" } });
  const connectOperation = await finished(context.request, connected.attempt_id);
  expect(connectOperation.status).toBe("success");
  expect(connectOperation.outcome).toMatchObject({ added: 2, changed: 0, deleted: 0, unchanged: 0, record_count: 2 });
  await expect(controls.getByRole("status", { name: "Business Central operation status", exact: true })).toContainText("The first full sync finished.", { timeout: 30_000 });
  await expect.poll(async () => (await read(context.request, "/dashboard")).coverage_latest_version, { timeout: 90_000 }).toBe(2);
  sources = await read(context.request, "/sources");
  expect(sources[0].next_sync_at).not.toBeNull();
  expect(sources[0].connection_status).toBe("connected");
  const operations: Record<string, unknown>[] = [{ phase: "clear-discovered-access", attempt_id: clearQueued.attempt_id, status: "success" }, { phase: "connect", attempt_id: connected.attempt_id, status: connectOperation.status, added: connectOperation.outcome?.added }];
  async function phase(value: string) {
    const response = await context.request.post("/_fixture/bc/phase", { headers, data: { phase: value } });
    expect(response.status()).toBe(200); expect(await response.json()).toEqual({ phase: value });
  }
  async function remote() {
    const response = await context.request.get("/_fixture/bc/status");
    expect(response.status()).toBe(200); return response.json();
  }
  async function parties() { return (await read<{ items: Party[]; total: number }>(context.request, `/parties?source_id=${sourceId}&include_deleted=true&limit=100`)).items; }
  async function enqueuedTasks() { return (await read(context.request, "/audit-log?action=screening.enqueued&target_table=screening_tasks&limit=100")).items.filter((row: Record<string, any>) => row.after?.source_id === sourceId).map((row: Record<string, any>) => row.target_id).sort(); }
  async function sync(mode: "full" | "incremental", phaseName: string, expectedStatus = "success") {
    await page.goto("/sources");
    const button = controls.getByRole("button", { name: mode === "full" ? "Full sync" : "Sync changes", exact: true });
    await expect(button).toBeEnabled();
    const response = page.waitForResponse(r => new URL(r.url()).pathname === `/api/v1/sources/${sourceId}/sync` && r.request().method() === "POST");
    await button.click();
    const accepted = await response;
    expect(accepted.status()).toBe(202);
    const queued = await accepted.json();
    expect(queued).toMatchObject({ operation: "source_sync", status: "queued" });
    const done = await finished(context.request, queued.attempt_id);
    expect(done.status).toBe(expectedStatus);
    if (expectedStatus === "success" && typeof done.outcome?.pending_task_id === "string") {
      await expect.poll(async () => (await read(context.request, `/audit-log?action=screening.task_completed&target_table=screening_tasks&target_id=${done.outcome!.pending_task_id}&limit=100`)).items.some((row: Record<string, any>) => Boolean(row.after?.run_id)), { timeout: 90_000 }).toBe(true);
    }
    operations.push({ phase: phaseName, attempt_id: queued.attempt_id, status: done.status, ...Object.fromEntries(["added", "changed", "deleted", "unchanged"].filter(key => typeof done.outcome?.[key] === "number").map(key => [key, done.outcome![key]])) });
    await expect(controls.getByRole("status", { name: "Business Central operation status", exact: true })).toContainText(expectedStatus === "success" ? "finished." : "ended with status “failed”", { timeout: 30_000 });
    return done;
  }
  const first = await parties();
  expect(first).toHaveLength(2);
  expect(first.map(row => row.external_id).sort()).toEqual([`Customer:${ENTITY}`, `Vendor:${ENTITY}`].sort());
  const customer = first.find(row => row.role === "customer")!;
  const vendor = first.find(row => row.role === "vendor")!;
  expect(customer.name).toBe(CUSTOMER_NAME); expect(customer.party_type).toBe("company");
  expect(vendor.name).toBe("Synthetic Fixture Logistics");
  expect(customer.id).not.toBe(vendor.id);
  expect(first.every(row => row.deleted_at === null)).toBe(true); // provider blocked=All is not deletion
  const initialTasks = await enqueuedTasks();
  const unchanged = await sync("full", "initial-repeat");
  expect(unchanged.outcome).toMatchObject({ added: 0, changed: 0, unchanged: 2, deleted: 0, record_count: 2, pending_task_id: null });
  expect(await enqueuedTasks()).toEqual(initialTasks);
  expect(fingerprints(await parties())).toEqual(fingerprints(first));
  console.info("Real BC browser gate: actual paged discovery, explicit company activation, namespaced parties and unchanged no-task sync verified.");

  const inviteResponse = await context.request.post("/api/v1/users/invitations", { headers, data: { email: "bc-fixture-reviewer@example.test", role: "reviewer" } });
  expect(inviteResponse.status()).toBe(201);
  const invitation = await inviteResponse.json();
  const invitationToken = new URL(invitation.invite_url, baseURL).hash.slice("#token=".length);
  const reviewerContext = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1000 } });
  const reviewerPage = await reviewerContext.newPage(); observe(reviewerPage);
  const reviewerPaths: string[] = [];
  reviewerPage.on("request", request => reviewerPaths.push(new URL(request.url()).pathname));
  try { await reviewerPage.goto(invitation.invite_url); } catch { throw new Error("Synthetic invitation navigation failed; private fragment omitted."); }
  await expect.poll(() => new URL(reviewerPage.url()).hash === "").toBe(true);
  await reviewerPage.getByRole("textbox", { name: /name/i }).fill("Fixture BC Reviewer");
  await reviewerPage.getByLabel("Password", { exact: true }).fill(REVIEWER_PASSWORD);
  await reviewerPage.getByRole("button", { name: "Create account", exact: true }).click();
  await expect(reviewerPage.getByRole("navigation", { name: "Main navigation" })).toBeVisible();
  const reviewer = await read(reviewerContext.request, "/auth/me");
  expect(reviewer.user.role).toBe("reviewer");
  const reviewerHeaders = { "X-CSRF-Token": reviewer.csrf_token, Origin: origin };
  await reviewerPage.goto("/sources");
  const readonly = reviewerPage.getByRole("article", { name: SOURCE_LABEL, exact: true });
  await expect(readonly.getByText("Connected", { exact: true })).toBeVisible();
  await expect(readonly).toContainText(ENVIRONMENT);
  await expect(readonly.getByRole("region", { name: "Business Central connection" })).toHaveCount(0);
  await expect(readonly.getByRole("button")).toHaveCount(0);
  expect(reviewerPaths.some(path => path.includes("/bc/") || path.includes("/settings/connectors/bc"))).toBe(false);
  expect((await reviewerContext.request.get("/api/v1/settings/connectors/bc")).status()).toBe(403);
  expect((await reviewerContext.request.get(`/api/v1${connectionPath}`)).status()).toBe(403);
  expect((await reviewerContext.request.post(`/api/v1/sources/${sourceId}/sync`, { headers: reviewerHeaders, data: { mode: "full" } })).status()).toBe(403);
  expect((await reviewerContext.request.post("/_fixture/bc/phase", { headers: reviewerHeaders, data: { phase: "changed" } })).status()).toBe(403);
  const queue = await read(reviewerContext.request, "/queue");
  const selected = queue.items.find((hit: Record<string, any>) => hit.party_id === customer.id && hit.entry_name === CUSTOMER_NAME);
  expect(Boolean(selected), "The actual fixture customer must have a queued control comparison.").toBe(true);
  expect(selected.bucket).toBe("likely");
  const original = await read(reviewerContext.request, `/hits/${selected.id}`);
  const originalSnapshots = { party: digest(original.party_snapshot), entry: digest(original.entry_snapshot) };
  await reviewerPage.goto(`/queue?hit=${selected.id}`);
  const note = reviewerPage.getByRole("textbox", { name: /note/i });
  await expect(note).toBeVisible(); await note.fill(NOTE); await reviewerPage.locator("h1").click();
  const decisionResponse = reviewerPage.waitForResponse(r => r.request().method() === "POST" && new URL(r.url()).pathname === `/api/v1/hits/${selected.id}/dispositions`);
  await reviewerPage.keyboard.press("f");
  const decisionResult = await decisionResponse; expect(decisionResult.status()).toBe(201);
  const decision = await decisionResult.json();
  expect(decision.disposition).toMatchObject({ actor: { id: reviewer.user.id }, kind: "false_positive", note: NOTE });
  expect(decision.hit_id).toBe(selected.id);
  const humanAudit = await read(reviewerContext.request, "/audit-log?action=review.disposition&limit=100");
  const reviewGroup = humanAudit.items.filter((row: Record<string, any>) => row.after?.queue_hit_id === selected.id);
  expect(reviewGroup.length).toBe(decision.observations_dispositioned); expect(reviewGroup.length).toBeGreaterThan(0);
  const reviewGroupIds = reviewGroup.map((row: Record<string, any>) => row.target_id);
  expect(reviewGroupIds).toContain(decision.disposition.id);
  for (const row of reviewGroup) {
    expect(row.after).toMatchObject({ actor: { id: reviewer.user.id }, note: NOTE, kind: "false_positive", party_id: original.party_id, csl_entry_id: original.csl_entry_id, party_hash: original.party_hash, entry_hash: original.entry_hash, queue_hit_id: selected.id });
    const covered = await read(reviewerContext.request, `/hits/${row.after.hit_id}`);
    expect(covered.party_snapshot).toEqual(original.party_snapshot); expect(covered.entry_snapshot).toEqual(original.entry_snapshot);
    expect(covered.dispositions.some((item: Record<string, any>) => item.id === row.target_id && item.actor?.id === reviewer.user.id && item.kind === "false_positive" && item.note === NOTE)).toBe(true);
  }
  const repeat = await context.request.post("/api/v1/screen", { headers, data: { all: true }, timeout: 90_000 });
  expect(repeat.ok()).toBe(true); expect((await repeat.json()).hits_new).toBe(0);
  expect((await read(context.request, "/queue")).items.filter((hit: Record<string, any>) => hit.party_id === customer.id && hit.csl_entry_id === selected.csl_entry_id)).toHaveLength(0);
  await phase("changed");
  const changed = await sync("incremental", "changed");
  expect(changed.outcome).toMatchObject({ added: 0, changed: 1, deleted: 0, unchanged: 1 });
  let current = await parties();
  const changedCustomer = current.find(row => row.id === customer.id)!;
  expect(changedCustomer.external_id).toBe(customer.external_id); expect(changedCustomer.name).toBe("Example Manufacturing 42 Updated");
  expect(changedCustomer.record_hash).not.toBe(customer.record_hash);
  expect(fingerprints([current.find(row => row.id === vendor.id)!])).toEqual(fingerprints([vendor]));
  const reopenedRow = (await read(reviewerContext.request, "/queue")).items.find((hit: Record<string, any>) => hit.party_id === customer.id && hit.csl_entry_id === selected.csl_entry_id);
  expect(Boolean(reopenedRow), "Changed meaningful hash must reopen the actual reviewed pairing.").toBe(true);
  const reopened = await read(reviewerContext.request, `/hits/${reopenedRow.id}`);
  expect(reopened.prior_disposition).toMatchObject({ actor: { id: reviewer.user.id }, kind: "false_positive", note: NOTE });
  expect(reviewGroupIds).toContain(reopened.prior_disposition.id);
  const priorAudit = reviewGroup.find((row: Record<string, any>) => row.target_id === reopened.prior_disposition.id);
  expect(reopened.prior_disposition.hit_id).toBe(priorAudit.after.hit_id);
  const priorHit = await read(reviewerContext.request, `/hits/${reopened.prior_disposition.hit_id}`);
  expect(priorHit.party_snapshot).toEqual(original.party_snapshot); expect(priorHit.entry_snapshot).toEqual(original.entry_snapshot);
  expect(reopened.party_hash).not.toBe(original.party_hash); expect(reopened.entry_hash).toBe(original.entry_hash);
  await reviewerPage.goto(`/queue?hit=${reopenedRow.id}`);
  const earlier = reviewerPage.getByRole("region", { name: "Earlier decision on this pairing", exact: true });
  await expect(earlier).toContainText("Fixture BC Reviewer"); await expect(earlier).toContainText(NOTE);
  if (Array.isArray(changed.outcome?.run_ids)) for (const id of changed.outcome.run_ids) expect((await read(context.request, `/screen/runs/${id}`)).parties_screened).toBe(1);
  console.info("Real BC browser gate: actual Reviewer keyboard decision, immutable audit group and changed-party prior attribution verified.");

  const beforeType = (await parties()).find(row => row.id === customer.id)!;
  expect(beforeType.party_type).toBe("company");
  await phase("type_changed");
  const typeChanged = await sync("incremental", "type-changed");
  expect(typeChanged.outcome).toMatchObject({ changed: 1, unchanged: 1 });
  current = await parties();
  expect(current.find(row => row.id === customer.id)?.name).toBe(beforeType.name);
  expect(current.find(row => row.id === customer.id)?.party_type).toBe("individual");
  expect(fingerprints([current.find(row => row.id === vendor.id)!])).toEqual(fingerprints([vendor]));
  expect(current.find(row => row.id === customer.id)?.record_hash).not.toBe(beforeType.record_hash);
  const typedRow = (await read(reviewerContext.request, "/queue")).items.find((hit: Record<string, any>) => hit.party_id === customer.id && hit.csl_entry_id === selected.csl_entry_id);
  expect(Boolean(typedRow), "The pure type change must retain a current comparison against the reviewed entry.").toBe(true);
  const typedHit = await read(reviewerContext.request, `/hits/${typedRow.id}`);
  expect(typedHit.party_hash).toBe(current.find(row => row.id === customer.id)!.record_hash);
  expect(typedHit.entry_hash).toBe(original.entry_hash);
  expect(typedHit.party_snapshot.party_type).toBe("individual");
  expect(typedHit.prior_disposition).toMatchObject({ actor: { id: reviewer.user.id }, kind: "false_positive", note: NOTE });
  expect(reviewGroupIds).toContain(typedHit.prior_disposition.id);
  await phase("deleted");
  const deleted = await sync("full", "deleted");
  expect(deleted.outcome).toMatchObject({ deleted: 1, record_count: 1 });
  expect(Boolean((await parties()).find(row => row.id === customer.id)?.deleted_at)).toBe(true);
  await reviewerPage.goto(`/parties/${customer.id}`);
  await expect(reviewerPage.locator(".page-header").getByText("Inactive", { exact: true })).toBeVisible();
  await phase("restored");
  await sync("full", "restored");
  expect((await parties()).find(row => row.id === customer.id)?.deleted_at).toBeNull();
  const beforeMalformed = fingerprints(await parties());
  const beforeSource = (await read<Array<Record<string, any>>>(context.request, "/sources"))[0];
  const beforeTasks = await enqueuedTasks();
  await phase("malformed");
  const malformed = await sync("incremental", "malformed", "failed");
  expect(malformed.error_code).toBe("operation_failed");
  expect(fingerprints(await parties())).toEqual(beforeMalformed); expect(await enqueuedTasks()).toEqual(beforeTasks);
  const afterSource = (await read<Array<Record<string, any>>>(context.request, "/sources"))[0];
  expect(afterSource.record_count).toBe(beforeSource.record_count); expect(afterSource.last_synced_at).toBe(beforeSource.last_synced_at);
  expect(afterSource.last_error).toMatch(/invalid_collection_response/);
  await phase("restored"); await sync("incremental", "recovery");
  expect((await read<Array<Record<string, any>>>(context.request, "/sources"))[0].last_error).toBeNull();

  // HTTP is allowed for local workspace URLs, but not for vendor webhooks.
  await page.goto("/sources");
  const webhook = controls.getByRole("checkbox", { name: "Receive Business Central change notifications (webhooks)", exact: true });
  const guardedResponse = page.waitForResponse(r => new URL(r.url()).pathname === `/api/v1${connectionPath}` && r.request().method() === "PATCH");
  await webhook.click(); expect((await guardedResponse).status()).toBe(422); await expect(webhook).not.toBeChecked();
  await page.goto("/settings");
  const email = page.getByRole("region", { name: "Email notifications", exact: true });
  await email.getByLabel("Public base URL", { exact: true }).fill(PUBLIC_BASE);
  await email.getByRole("button", { name: "Save notification settings", exact: true }).click();
  await expect(email.getByRole("status").filter({ hasText: "Notification settings saved." })).toBeVisible();
  expect((await read(context.request, "/settings/connectors/bc")).webhook_base_url).toBe(`${PUBLIC_BASE}/api/v1/webhooks/bc`);
  await page.goto("/sources");
  await expect(webhook).not.toBeChecked();
  const enableResponse = page.waitForResponse(r => new URL(r.url()).pathname === `/api/v1${connectionPath}` && r.request().method() === "PATCH");
  await webhook.click();
  const enable = await enableResponse; expect(enable.status()).toBe(202);
  const enableQueued = await enable.json(); expect(enableQueued).toMatchObject({ operation: "bc_subscriptions", status: "queued" });
  expect((await finished(context.request, enableQueued.attempt_id)).status).toBe("success");
  await expect.poll(async () => (await read(context.request, connectionPath)).subscription_status, { timeout: 30_000 }).toBe("active");
  let connection = await read(context.request, connectionPath);
  expect(connection.subscriptions.map((row: Record<string, any>) => row.resource).sort()).toEqual(["customers", "vendors"]);
  expect(connection.subscriptions.every((row: Record<string, any>) => row.status === "active" && typeof row.subscription_id === "string" && Date.parse(row.expiration_date_time) > Date.now())).toBe(true);
  await expect(controls.getByText("Subscriptions active", { exact: true })).toBeVisible();
  await expect(controls.getByRole("table", { name: "Business Central subscriptions" }).getByRole("row")).toHaveCount(3);
  const firstExpiry = connection.subscriptions.map((row: Record<string, any>) => ({ resource: row.resource, expires_at: row.expiration_date_time }));
  const remoteBefore = await remote(); expect(remoteBefore.subscription_count).toBe(2); expect(remoteBefore.handshakes).toBeGreaterThanOrEqual(2);
  const reconcileResponse = page.waitForResponse(r => new URL(r.url()).pathname === `/api/v1/sources/${sourceId}/bc/subscriptions/reconcile` && r.request().method() === "POST");
  await controls.getByRole("button", { name: "Reconcile subscriptions", exact: true }).click();
  const reconcile = await reconcileResponse; expect(reconcile.status()).toBe(202);
  const reconcileQueued = await reconcile.json(); expect(reconcileQueued).toMatchObject({ operation: "bc_subscriptions", status: "queued" });
  expect((await finished(context.request, reconcileQueued.attempt_id)).status).toBe("success");
  await expect(controls.getByRole("status", { name: "Business Central operation status", exact: true })).toContainText("The subscription reconcile finished.");
  console.info("Real BC browser gate: type hash, full deletion/restoration, malformed atomicity and actual TCP subscription handshakes verified.");

  const preserved = fingerprints(await parties());
  async function disconnect(retry: boolean, expectedStatus: string) {
    await page.goto("/sources");
    await controls.getByRole("button", { name: retry ? "Retry subscription cleanup" : "Disconnect", exact: true }).click();
    const groupName = retry ? "Confirm cleanup retry" : "Confirm disconnect";
    const group = controls.getByRole("group", { name: groupName, exact: true });
    await expect(group).toContainText("Parties, screening history and audit records from this source are kept");
    await expect(group).toContainText("doesn’t remove admin consent");
    const response = page.waitForResponse(r => new URL(r.url()).pathname === `/api/v1/sources/${sourceId}/bc/disconnect` && r.request().method() === "POST");
    await group.getByRole("button", { name: groupName, exact: true }).click();
    const accepted = await response; expect(accepted.status()).toBe(202);
    const queued = await accepted.json(); expect(queued).toMatchObject({ operation: "source_disconnect", status: "queued", connection: { status: "disconnecting" } });
    const done = await finished(context.request, queued.attempt_id); expect(done.status).toBe(expectedStatus);
    await expect.poll(async () => (await read(context.request, connectionPath)).status, { timeout: 30_000 }).toBe("disconnected");
    operations.push({ phase: retry ? "cleanup-retry" : "cleanup-failed", attempt_id: queued.attempt_id, status: done.status });
    return queued.attempt_id as string;
  }
  await phase("cleanup_failed");
  const failedDisconnect = await disconnect(false, "failed");
  connection = await read(context.request, connectionPath); expect(connection.cleanup_status).toBe("failed");
  await expect(controls.getByText("Subscription cleanup failed.", { exact: true })).toBeVisible();
  expect((await read<Array<Record<string, any>>>(context.request, "/sources"))[0].next_sync_at).toBeNull();
  expect(fingerprints(await parties())).toEqual(preserved); expect((await remote()).subscription_count).toBe(2);
  await phase("initial");
  const retriedDisconnect = await disconnect(true, "success"); expect(retriedDisconnect).not.toBe(failedDisconnect);
  connection = await read(context.request, connectionPath); expect(connection.cleanup_status).toBe("completed");
  await expect(controls.getByText("Subscription cleanup completed.", { exact: true })).toBeVisible();
  expect((await remote()).subscription_count).toBe(0); expect(fingerprints(await parties())).toEqual(preserved);
  expect((await read<Array<Record<string, any>>>(context.request, "/sources"))[0].next_sync_at).toBeNull();
  console.info("Real BC browser gate: disconnected polling, retained records, failed subscription cleanup and explicit successful retry verified.");

  await page.goto("/sources"); await top(page);
  await page.screenshot({ path: join(shots, "milestone-7-real-bc-sources.png") });
  await card.evaluate(element => element.scrollIntoView({ block: "start" }));
  await page.screenshot({ path: join(shots, "milestone-7-real-bc-source-card.png") });
  await reviewerPage.goto(`/parties/${customer.id}`);
  await expect(reviewerPage.getByRole("heading", { level: 1, name: (await parties()).find(row => row.id === customer.id)!.name })).toBeVisible();
  await top(reviewerPage);
  await reviewerPage.screenshot({ path: join(shots, "milestone-7-real-bc-reviewer-party.png") });
  const exportProof = await auditZip(page, context.request, session.user.id, [APP_ID, APP_SECRET, BEARER, ADMIN_PASSWORD, REVIEWER_PASSWORD, session.csrf_token, reviewer.csrf_token, invitationToken, ...(await context.cookies()).map(cookie => cookie.value), ...(await reviewerContext.cookies()).map(cookie => cookie.value)]);
  const frozen = await read(reviewerContext.request, `/hits/${selected.id}`);
  expect(digest(frozen.party_snapshot)).toBe(originalSnapshots.party); expect(digest(frozen.entry_snapshot)).toBe(originalSnapshots.entry);
  expect(frozen.dispositions.some((row: Record<string, any>) => reviewGroupIds.includes(row.id) && row.actor?.id === reviewer.user.id && row.note === NOTE)).toBe(true);
  const remoteEnd = await remote();
  expect(remoteEnd.handshakes).toBeGreaterThanOrEqual(2); expect(remoteEnd.writer_checks).toBeGreaterThan(0);
  expect(browserErrors).toEqual([]); expect(externalOrigins).toEqual([]);
  const evidence = { gate: "milestone-7-real-bc-browser", fixture_only: true, fresh_complete_gate: true, admin_id: session.user.id, reviewer_id: reviewer.user.id, source_id: sourceId, csl_entries: seeded.current_version.entry_count, party_ids: first.map(row => row.id), entity_id_namespaces: first.map(row => row.external_id).sort(), original_hit_id: selected.id, reopened_hit_id: reopenedRow.id, disposition_id: decision.disposition.id, review_group_ids: reviewGroupIds, observations_dispositioned: decision.observations_dispositioned, prior_disposition_id: reopened.prior_disposition.id, snapshot_sha256: originalSnapshots, original_snapshots_retained: true, unchanged_no_new_task: true, discovery_access_cleared_before_activation: true, malformed_atomic: true, role_guards: true, subscription_expiries: firstExpiry, actual_tcp_handshakes: remoteEnd.handshakes, writer_free_http_checks: remoteEnd.writer_checks, local_disconnect_retained_records: true, remote_subscription_cleanup_retry_succeeded: true, final_subscription_count: remoteEnd.subscription_count, operations, export: exportProof, browser_errors: browserErrors };
  await writeFile(join(artifacts, "milestone-7-real-bc-browser-evidence.json"), JSON.stringify(evidence, null, 2) + "\n");
  console.info(`Real BC browser gate: ${exportProof.verified_checksums} export checksums, strict privacy and original human snapshots verified; safe evidence saved.`);
  await reviewerContext.close(); await context.close();
});

// Local parser regression: no API/runtime requests. Original snapshot wrappers
// may nest a provider record beneath raw.original, entities, or similar fields.
test("BC ZIP privacy preserves structured geography while rejecting private fields, values and invalid checksum evidence", () => {
  const proofScript = String.raw`
import hashlib,json,tempfile,zipfile,subprocess,sys
checker=sys.stdin.read()
region={'id':'synthetic-entity','displayName':'Synthetic company','country':'US','lastModifiedDateTime':'2026-10-14T00:00:00Z','state':'MA'}
valid={'party':{'raw':{'original':region}},'audit_log':[{'after':{'raw':{'entities':{'customers':[region]}},'clientState':'[redacted]'},'before':{'credentials_enc':None}}]}
cases=[
 ('nested-geography',valid,{},True,False),
 ('oauth-state',{'party':{'state':'opaque'}},{},False,False),
 ('wrong-geography',{**region,'state':'synthetic-private-needle'},{},False,False),
 ('client-state',{'party':{'client_state':'opaque'}},{},False,False),
 ('audit-private-value',{'audit_log':[{'after':{'clientState':'opaque'}}]},{},False,False),
 ('audit-wrong-scope',{'audit_log':[{'client_state':'[redacted]'}]},{},False,False),
 ('unrecognized-encrypted-field',{'party':{'cleanup_enc':'opaque'}},{},False,False),
 ('source-private-marker',{}, {'client_state':'[redacted]'},False,False),
 ('known-value',{'party':{'name':'synthetic-private-needle'}},{},False,False),
 ('oauth-code',{'party':{'code':None}},{},False,False),
 ('checksum-mismatch',valid,{},False,True),
]
for label,payload,source,good,bad_hash in cases:
 with tempfile.TemporaryDirectory() as tmp:
  path=tmp+'/fixture.zip';data=json.dumps(payload).encode();name='parties/fixture.json'
  with zipfile.ZipFile(path,'w') as archive:
   archive.writestr(name,data);archive.writestr('sources.jsonl',json.dumps({'id':'source',**source}));archive.writestr('manifest.json',json.dumps({'files':{name:{'sha256':'0'*64 if bad_hash else hashlib.sha256(data).hexdigest()}}}))
  result=subprocess.run(['python3','-c',checker,path],input=json.dumps(['synthetic-private-needle']),text=True,capture_output=True)
  assert (result.returncode==0)==good,label
  if good: assert json.loads(result.stdout)['geographic_state_fields_checked']==2
print(json.dumps({'cases':len(cases),'structured_geography_preserved':True,'invalid_private_fields_scopes_values_and_checksum_rejected':True}))
`;
  const proof = JSON.parse(execFileSync("python3", ["-c", proofScript], { input: privacyScript, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] }));
  expect(proof).toEqual({ cases: 11, structured_geography_preserved: true, invalid_private_fields_scopes_values_and_checksum_rejected: true });
});
