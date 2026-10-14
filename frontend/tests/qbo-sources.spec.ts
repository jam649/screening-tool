import { expect, test, type Page } from "@playwright/test";

// Assumes Sources mounts QboSourceControls inside each QBO source card and
// AddQboSource after the CSV form. All data below is synthetic.
test.use({ locale: "en-US", timezoneId: "UTC" });

const CSRF = "qbo-csrf-token";
const LABEL = "Synthetic QBO books";

const user = (role: "admin" | "reviewer") => ({
  id: `user-${role}`,
  email: `${role}@example.test`,
  display_name: `Test ${role}`,
  role,
  active: true,
});
const setup = {
  needs_setup: false,
  product_name: "screening-tool",
  disclaimer: "Synthetic notice.",
  disclaimer_url: "https://example.test/notice",
};
const flags = (configured: boolean) => ({
  client_id_configured: configured,
  client_secret_configured: configured,
  verifier_token_configured: false,
  configured,
});
const qboSettings = {
  sandbox: flags(true),
  production: flags(false),
  redirect_uri: "https://screening.example.test/api/v1/connectors/qbo/oauth/callback",
  webhook_urls: { sandbox: null, production: null },
};

const qboSource = (overrides: Record<string, unknown> = {}) => ({
  id: "qbo-1",
  kind: "qbo",
  label: LABEL,
  record_count: 12,
  last_synced_at: "2026-10-13T09:00:00Z",
  next_sync_at: "2026-10-14T09:15:00Z",
  health: "healthy",
  last_error: null,
  column_mapping: {},
  environment: "sandbox",
  connection_status: "connected",
  ...overrides,
});
const connectionOf = (overrides: Record<string, unknown> = {}) => ({
  source_id: "qbo-1",
  environment: "sandbox",
  status: "connected",
  realm_id: "9130-0000-synthetic",
  access_expires_at: "2026-10-14T10:00:00Z",
  refresh_expires_at: "2027-01-20T00:00:00Z",
  webhooks_enabled: false,
  revocation_status: null,
  revocation_error_code: null,
  ...overrides,
});
const unconnected = connectionOf({
  status: "configured",
  realm_id: null,
  access_expires_at: null,
  refresh_expires_at: null,
});
const operationOf = (
  id: string,
  status: string,
  extra: Record<string, unknown> = {},
) => ({
  id,
  attempt_id: id,
  operation: "source_sync",
  source_id: "qbo-1",
  party_id: null,
  status,
  created_at: "2026-10-14T08:00:00Z",
  started_at: status === "queued" ? null : "2026-10-14T08:00:01Z",
  finished_at: null,
  error_code: null,
  outcome: null,
  ...extra,
});

type Reply = { status?: number; body?: unknown };
type Call = {
  key: string;
  csrf?: string;
  contentType?: string;
  body: unknown;
};
type Routes = Record<string, (call: Call) => Reply>;

const sequence = (...replies: Reply[]) => {
  let index = 0;
  return () => replies[Math.min(index++, replies.length - 1)];
};
const count = (calls: Call[], key: string) =>
  calls.filter((call) => call.key === key).length;

async function mockApi(page: Page, role: "admin" | "reviewer", routes: Routes) {
  const calls: Call[] = [];
  const base: Routes = {
    "GET /setup": () => ({ body: setup }),
    "GET /auth/me": () => ({ body: { user: user(role), csrf_token: CSRF } }),
    "GET /notifications/status": () => ({ body: { batches: [], health: [] } }),
    "GET /monitoring/status": () => ({
      body: { enabled: true, schedules: [], operations: [], health: [] },
    }),
    "GET /settings/monitoring": () => ({
      body: {
        csl_fetch_time: "05:00",
        source_poll_minutes: 15,
        retention_weekday: "sun",
        retention_time: "03:00",
      },
    }),
    "GET /dashboard": () => ({ body: { total_parties: 0, csl_version: null } }),
    "GET /settings/connectors/qbo": () => ({ body: qboSettings }),
  };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const key = `${request.method()} ${new URL(request.url()).pathname.replace(/^\/api\/v1/, "")}`;
    const raw = request.postData();
    let body: unknown = raw;
    try {
      body = raw ? JSON.parse(raw) : null;
    } catch {
      // Keep non-JSON bodies as text.
    }
    const call: Call = {
      key,
      csrf: request.headers()["x-csrf-token"],
      contentType: request.headers()["content-type"],
      body,
    };
    calls.push(call);
    const reply = (routes[key] ?? base[key])?.(call) ?? {
      status: 404,
      body: { detail: "Not mocked." },
    };
    await route.fulfill({
      status: reply.status ?? 200,
      contentType: "application/json",
      body: JSON.stringify(reply.body ?? {}),
    });
  });
  return calls;
}

const card = (page: Page, label = LABEL) =>
  page.getByRole("article", { name: label });
const qboRegion = (page: Page, label = LABEL) =>
  card(page, label).getByRole("region", { name: "QuickBooks connection" });
const addRegion = (page: Page) =>
  page.getByRole("region", { name: "Add a QuickBooks source" });
const operationStatus = (page: Page) =>
  qboRegion(page).getByRole("status", { name: "QuickBooks operation status" });

const START = "POST /sources/qbo-1/qbo/oauth/start";
const CONNECTION = "GET /sources/qbo-1/qbo/connection";

test("an Admin adds a QuickBooks source with environment and CSRF, blocked while that app is unconfigured", async ({
  page,
}) => {
  const sources: object[] = [];
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: sources }),
    "POST /sources": () => {
      const created = qboSource({
        id: "qbo-new",
        record_count: 0,
        last_synced_at: null,
        next_sync_at: null,
        health: "not_synced",
        connection_status: "configured",
      });
      sources.push(created);
      return { status: 201, body: created };
    },
    "GET /sources/qbo-new/qbo/connection": () => ({
      body: { ...unconnected, source_id: "qbo-new" },
    }),
  });
  await page.goto("/sources");
  const add = addRegion(page);
  const submit = add.getByRole("button", { name: "Add QuickBooks source" });

  await add.getByLabel("QuickBooks environment").selectOption("production");
  await add.getByLabel("Source name").fill("Synthetic production books");
  await expect(add.getByText(/Production Intuit app isn.t configured/)).toBeVisible();
  await expect(
    add.getByRole("link", { name: "Configure your Intuit app in Settings" }),
  ).toHaveAttribute("href", "/settings");
  await expect(submit).toBeDisabled();
  await add.getByLabel("Source name").press("Enter");
  expect(count(calls, "POST /sources")).toBe(0);

  await add.getByLabel("QuickBooks environment").selectOption("sandbox");
  await expect(add.getByText(/Intuit app isn.t configured/)).toHaveCount(0);
  await add.getByLabel("Source name").fill(`  ${LABEL}  `);
  await submit.click();
  await expect(add.getByRole("status")).toContainText(
    `“${LABEL}” was added but is not connected yet.`,
  );
  const post = calls.find((call) => call.key === "POST /sources");
  expect(post?.body).toEqual({ kind: "qbo", label: LABEL, environment: "sandbox" });
  expect(post?.csrf).toBe(CSRF);
  expect(post?.contentType).toBe("application/json");
  await expect(add.getByLabel("Source name")).toHaveValue("");

  const region = qboRegion(page);
  await expect(region.getByText("Not connected", { exact: true })).toBeVisible();
  await expect(region.getByRole("button", { name: "Connect to QuickBooks" })).toBeEnabled();
  await expect(region.getByRole("button", { name: /Sync|Disconnect/ })).toHaveCount(0);
  expect(calls.filter((call) => call.key.startsWith("POST")).map((call) => call.key)).toEqual([
    "POST /sources",
  ]);
});

test("Connect posts an empty CSRF request and opens only the verified Intuit page", async ({
  page,
}) => {
  const consoleText: string[] = [];
  page.on("console", (message) => consoleText.push(message.text()));
  const authorization =
    "https://appcenter.intuit.com/connect/oauth2?client_id=synthetic-client&response_type=code" +
    "&scope=com.intuit.quickbooks.accounting&redirect_uri=https%3A%2F%2Fscreening.example.test%2Fcallback" +
    "&state=synthetic-state-value";
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [qboSource({ connection_status: "configured" })] }),
    [CONNECTION]: () => ({ body: unconnected }),
    [START]: () => ({
      body: { authorization_url: authorization, expires_at: "2026-10-14T10:10:00Z" },
    }),
  });
  // Stub the external page so no real Intuit request is made.
  const intuit: string[] = [];
  await page.route(/^https:\/\/appcenter\.intuit\.com\//, (route) => {
    intuit.push(route.request().url());
    return route.fulfill({
      contentType: "text/html",
      body: "<title>Synthetic Intuit stub</title><p>Stub</p>",
    });
  });
  await page.goto("/sources");
  const region = qboRegion(page);
  await expect(region.getByText("Not connected", { exact: true })).toBeVisible();
  await expect(region).toContainText("exactly one company");
  await expect(region).not.toContainText("Connected");
  await region.getByRole("button", { name: "Connect to QuickBooks" }).click();

  await expect(page).toHaveURL(/^https:\/\/appcenter\.intuit\.com\/connect\/oauth2\?/);
  expect(intuit).toEqual([authorization]);
  const starts = calls.filter((call) => call.key === START);
  expect(starts).toHaveLength(1);
  expect(starts[0].body).toEqual({});
  expect(starts[0].csrf).toBe(CSRF);
  expect(consoleText.join("\n")).not.toContain("synthetic-state-value");
});

test("Connect refuses any authorization URL that isn't the exact Intuit page", async ({
  page,
}) => {
  const bad = [
    "https://appcenter.intuit.com.evil.test/connect/oauth2?state=synthetic",
    "https://synthetic:secret@appcenter.intuit.com/connect/oauth2?state=synthetic",
    "https://appcenter.intuit.com:8443/connect/oauth2?state=synthetic",
    "https://appcenter.intuit.com/connect/oauth2/other?state=synthetic",
    "http://appcenter.intuit.com/connect/oauth2?state=synthetic",
    "https://appcenter.intuit.com/connect/oauth2?state=synthetic#fragment",
    "javascript:alert(1)",
  ];
  let index = 0;
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [qboSource({ connection_status: "configured" })] }),
    [CONNECTION]: () => ({ body: unconnected }),
    [START]: () => ({
      body: { authorization_url: bad[index++], expires_at: "2026-10-14T10:10:00Z" },
    }),
  });
  const requested: string[] = [];
  page.on("request", (request) => requested.push(request.url()));
  await page.goto("/sources");
  const appUrl = page.url();
  const appHost = new URL(appUrl).host;
  const region = qboRegion(page);
  const connect = region.getByRole("button", { name: "Connect to QuickBooks" });

  for (let attempt = 1; attempt <= bad.length; attempt++) {
    await connect.click();
    await expect.poll(() => count(calls, START)).toBe(attempt);
    await expect(region.getByRole("alert")).toContainText(
      "QuickBooks sign-in wasn’t opened.",
    );
    await expect(connect).toBeEnabled();
  }
  expect(page.url()).toBe(appUrl);
  expect(requested.filter((url) => new URL(url).host !== appHost)).toEqual([]);
  await expect(region).not.toContainText("evil.test");
  await expect(region).not.toContainText("state=");
});

test("reconnect stays tied to the same company and sync is offered only when connected", async ({
  page,
}) => {
  let connection = connectionOf({ status: "reconnect_required" });
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [qboSource({ connection_status: connection.status })] }),
    [CONNECTION]: () => ({ body: connection }),
  });
  await page.goto("/sources");
  let region = qboRegion(page);
  await expect(region.getByText("Reconnect required", { exact: true })).toBeVisible();
  await expect(region.getByRole("button", { name: "Reconnect to QuickBooks" })).toBeEnabled();
  await expect(region).toContainText("same QuickBooks company (company ID 9130-0000-synthetic)");
  await expect(region).toContainText("add a new QuickBooks source");
  await expect(region.getByRole("button", { name: /Sync/ })).toHaveCount(0);
  await expect(region.getByRole("button", { name: "Disconnect" })).toBeVisible();

  connection = connectionOf();
  await page.reload();
  region = qboRegion(page);
  await expect(region.getByText("Connected", { exact: true })).toBeVisible();
  await expect(region.getByRole("button", { name: "Sync changes" })).toBeEnabled();
  await expect(region.getByRole("button", { name: "Full sync" })).toBeEnabled();
  await expect(region.getByRole("button", { name: /Connect|Reconnect/ })).toHaveCount(0);

  connection = connectionOf({
    status: "disconnected",
    access_expires_at: null,
    refresh_expires_at: null,
    revocation_status: "revoked",
  });
  await page.reload();
  region = qboRegion(page);
  await expect(region.getByText("Disconnected", { exact: true })).toBeVisible();
  await expect(region).toContainText("Intuit confirmed the authorization was revoked.");
  await expect(region.getByRole("button", { name: "Reconnect to QuickBooks" })).toBeEnabled();
  await expect(region.getByRole("button", { name: /Sync|Disconnect/ })).toHaveCount(0);
  expect(calls.filter((call) => call.key.startsWith("POST"))).toEqual([]);
});

test("an incremental sync is queued and running before success, showing only supplied counts", async ({
  page,
}) => {
  const OP = "GET /monitoring/operations/op-sync-1";
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [qboSource()] }),
    [CONNECTION]: () => ({ body: connectionOf() }),
    "POST /sources/qbo-1/sync": () => ({
      status: 202,
      body: { attempt_id: "op-sync-1", operation: "source_sync", status: "queued" },
    }),
    [OP]: sequence(
      { body: operationOf("op-sync-1", "queued") },
      { body: operationOf("op-sync-1", "running") },
      {
        body: operationOf("op-sync-1", "success", {
          finished_at: "2026-10-14T08:01:00Z",
          outcome: {
            source_id: "qbo-1",
            added: 2,
            changed: 1,
            deleted: 0,
            record_count: 14,
            pending_task_id: "task-synthetic-1",
            run_ids: [],
            failed_task_ids: [],
            screening_status: "queued",
          },
        }),
      },
    ),
  });
  await page.goto("/sources");
  const region = qboRegion(page);
  await region.getByRole("button", { name: "Sync changes" }).click();
  const status = operationStatus(page);
  await expect(status).toContainText("queued");
  await expect(status).not.toContainText("finished");
  await expect(region.getByRole("button", { name: "Full sync" })).toBeDisabled();
  await expect(region.getByRole("button", { name: "Disconnect" })).toBeDisabled();
  await expect(status).toContainText("is running");
  await expect(status).not.toContainText("finished");
  const sourceReads = count(calls, "GET /sources");

  await expect(status).toContainText("The incremental sync finished.", { timeout: 10_000 });
  await expect(status).toContainText("2 added");
  await expect(status).toContainText("1 changed");
  await expect(status).toContainText("0 deleted");
  await expect(status).not.toContainText("unchanged");
  await expect(status).toContainText("14 active records");
  await expect(status).toContainText("Screening of new and changed parties is queued");
  await expect(status).not.toContainText(/screening (finished|complete)/i);
  await expect.poll(() => count(calls, "GET /sources")).toBeGreaterThan(sourceReads);
  await expect(region.getByRole("button", { name: "Full sync" })).toBeEnabled();

  const reads = count(calls, OP);
  expect(reads).toBe(3);
  await page.waitForTimeout(2000);
  expect(count(calls, OP)).toBe(reads);
  const posts = calls.filter((call) => call.key === "POST /sources/qbo-1/sync");
  expect(posts).toHaveLength(1);
  expect(posts[0].body).toEqual({ mode: "incremental" });
  expect(posts[0].csrf).toBe(CSRF);
});

test("read errors stop polling, retry reads without posting again, and failures keep server counts", async ({
  page,
}) => {
  const OP = "GET /monitoring/operations/op-sync-2";
  const SYNC = "POST /sources/qbo-1/sync";
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [qboSource()] }),
    [CONNECTION]: sequence(
      { status: 500, body: { detail: "Connection status is temporarily unavailable." } },
      { body: connectionOf() },
    ),
    [SYNC]: sequence(
      { status: 409, body: { detail: "A sync for this source is already running." } },
      {
        status: 202,
        body: { attempt_id: "op-sync-2", operation: "source_sync", status: "queued" },
      },
    ),
    [OP]: sequence(
      { status: 503, body: { detail: "Operation lookup failed." } },
      {
        body: operationOf("op-sync-2", "failed", {
          finished_at: "2026-10-14T08:01:00Z",
          error_code: "qbo_timeout",
        }),
      },
    ),
  });
  await page.goto("/sources");
  const region = qboRegion(page);
  await expect(region.getByRole("alert")).toContainText(
    "Connection status is temporarily unavailable.",
  );
  await expect(region.getByRole("button", { name: /Sync|Connect|Disconnect/ })).toHaveCount(0);
  await region.getByRole("button", { name: "Check connection again" }).click();
  await expect(region.getByText("Connected", { exact: true })).toBeVisible();

  const fullSync = region.getByRole("button", { name: "Full sync" });
  await fullSync.click();
  const conflict = region.getByRole("alert").filter({ hasText: "already running" });
  await expect(conflict).toContainText("The sync wasn’t queued.");
  expect(count(calls, SYNC)).toBe(1);

  await fullSync.click();
  const lookup = region.getByRole("alert").filter({ hasText: "Operation lookup failed." });
  await expect(lookup).toBeVisible();
  await expect(conflict).toHaveCount(0);
  await expect(fullSync).toBeDisabled();
  await page.waitForTimeout(2000);
  expect(count(calls, OP)).toBe(1);

  await lookup.getByRole("button", { name: "Check status again" }).click();
  const status = operationStatus(page);
  await expect(status).toContainText("The full sync ended with status “failed”.");
  await expect(status).toContainText("qbo_timeout");
  await expect(status).not.toContainText("finished");
  await expect(status).not.toContainText(/unchanged/i);
  await expect(card(page).getByText("12", { exact: true })).toBeVisible();
  await expect(fullSync).toBeEnabled();
  await page.waitForTimeout(2000);
  expect(count(calls, OP)).toBe(2);
  expect(count(calls, SYNC)).toBe(2);
  expect(calls.filter((call) => call.key === SYNC).map((call) => call.body)).toEqual([
    { mode: "full" },
    { mode: "full" },
  ]);
});

test("disconnect explains retained history, stops actions at once and reports a failed remote revocation", async ({
  page,
}) => {
  const DISCONNECT = "POST /sources/qbo-1/qbo/disconnect";
  let connection = connectionOf();
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({
      body: [qboSource({ connection_status: connection.status })],
    }),
    [CONNECTION]: () => ({ body: connection }),
    [DISCONNECT]: () => {
      const accepted = connectionOf({ status: "disconnecting", revocation_status: "pending" });
      connection = connectionOf({
        status: "disconnected",
        access_expires_at: null,
        refresh_expires_at: null,
        revocation_status: "failed",
        revocation_error_code: "revocation_http_503",
      });
      return {
        status: 202,
        body: {
          attempt_id: "op-disconnect-1",
          operation: "source_disconnect",
          status: "queued",
          connection: accepted,
        },
      };
    },
    "GET /monitoring/operations/op-disconnect-1": sequence(
      { body: operationOf("op-disconnect-1", "running", { operation: "source_disconnect" }) },
      {
        body: operationOf("op-disconnect-1", "success", {
          operation: "source_disconnect",
          finished_at: "2026-10-14T08:01:00Z",
        }),
      },
    ),
  });
  await page.goto("/sources");
  const region = qboRegion(page);
  await region.getByRole("button", { name: "Disconnect" }).click();
  const confirm = region.getByRole("group", { name: "Confirm disconnect" });
  await expect(confirm).toContainText("Scheduled polling stops immediately.");
  await expect(confirm).toContainText(
    "Parties, screening history and audit records from this source are kept.",
  );
  expect(count(calls, DISCONNECT)).toBe(0);
  const connectionReads = count(calls, CONNECTION);
  const sourceReads = count(calls, "GET /sources");

  await confirm.getByRole("button", { name: "Confirm disconnect" }).click();
  await expect(region.getByText("Disconnecting", { exact: true })).toBeVisible();
  await expect(region.getByRole("button", { name: /Sync|Reconnect|Connect to/ })).toHaveCount(0);
  await expect(region.getByRole("checkbox")).toHaveCount(0);

  const status = operationStatus(page);
  await expect(status).toContainText("The disconnect finished.", { timeout: 10_000 });
  await expect(region.getByText("Disconnected", { exact: true })).toBeVisible();
  await expect(region).toContainText("Intuit did not confirm revoking the authorization.");
  await expect(region).toContainText("revocation_http_503");
  await expect(region).toContainText("still disconnected here");
  await expect(region).not.toContainText("confirmed the authorization was revoked");
  await expect(region.getByRole("button", { name: "Reconnect to QuickBooks" })).toBeEnabled();
  await expect.poll(() => count(calls, CONNECTION)).toBeGreaterThan(connectionReads);
  await expect.poll(() => count(calls, "GET /sources")).toBeGreaterThan(sourceReads);

  const posts = calls.filter((call) => call.key === DISCONNECT);
  expect(posts).toHaveLength(1);
  expect(posts[0].body).toEqual({});
  expect(posts[0].csrf).toBe(CSRF);
});

test("a rejected webhook change keeps the server's setting and polling stays on", async ({
  page,
}) => {
  const PATCH = "PATCH /sources/qbo-1/qbo/connection";
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [qboSource()] }),
    [CONNECTION]: () => ({ body: connectionOf() }),
    [PATCH]: sequence(
      {
        status: 422,
        body: { detail: "Webhooks need a verifier token and a public HTTPS address." },
      },
      { body: connectionOf({ webhooks_enabled: true }) },
    ),
  });
  await page.goto("/sources");
  const region = qboRegion(page);
  const checkbox = region.getByRole("checkbox", { name: /change notifications/ });
  await expect(checkbox).not.toBeChecked();
  await expect(region).toContainText("Scheduled polling continues either way.");

  await checkbox.click();
  const alert = region.getByRole("alert");
  await expect(alert).toContainText("The notification setting wasn’t changed.");
  await expect(alert).toContainText("Webhooks need a verifier token and a public HTTPS address.");
  await expect(checkbox).not.toBeChecked();
  await expect(region.getByText("Connected", { exact: true })).toBeVisible();
  await expect(region.getByRole("button", { name: "Sync changes" })).toBeEnabled();

  await checkbox.click();
  await expect(checkbox).toBeChecked();
  await expect(alert).toHaveCount(0);
  const patches = calls.filter((call) => call.key === PATCH);
  expect(patches.map((call) => call.body)).toEqual([
    { webhooks_enabled: true },
    { webhooks_enabled: true },
  ]);
  expect(patches.every((call) => call.csrf === CSRF)).toBe(true);
});

test("a Reviewer sees public source health only and makes no QuickBooks requests", async ({
  page,
}) => {
  const calls = await mockApi(page, "reviewer", {
    "GET /sources": () => ({ body: [qboSource()] }),
  });
  await page.goto("/sources");
  const qbo = card(page);
  await expect(qbo).toContainText("Healthy");
  await expect(qbo.getByText("12", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "QuickBooks connection" })).toHaveCount(0);
  await expect(addRegion(page)).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: /Connect|Sync|Disconnect|QuickBooks/ }),
  ).toHaveCount(0);
  await page.waitForTimeout(500);
  const qboCalls = calls.filter((call) =>
    /\/qbo|\/settings\/connectors|\/monitoring\/operations|\/sync$/.test(call.key),
  );
  expect(qboCalls).toEqual([]);
});


test("a completed screening task ID is provenance and does not make a successful sync appear pending", async ({ page }) => {
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [qboSource()] }),
    [CONNECTION]: () => ({ body: connectionOf() }),
    "POST /sources/qbo-1/sync": () => ({ status: 202, body: { attempt_id: "op-complete", operation: "source_sync", status: "queued" } }),
    "GET /monitoring/operations/op-complete": () => ({ body: operationOf("op-complete", "success", { outcome: { source_id: "qbo-1", added: 1, changed: 0, deleted: 0, unchanged: 12, record_count: 13, pending_task_id: "already-completed-task", screening_status: "success", run_ids: ["synthetic-run"], failed_task_ids: [] } }) }),
  });
  await page.goto("/sources");
  await qboRegion(page).getByRole("button", { name: "Full sync" }).click();
  const status = operationStatus(page);
  await expect(status).toContainText("The full sync finished.");
  await expect(status).toContainText("Screening finished for new and changed parties and recorded 1 run");
  await expect(status).not.toContainText(/Screening of new and changed parties is queued/i);
  expect(calls.find((call) => call.key === "POST /sources/qbo-1/sync")).toMatchObject({ body: { mode: "full" }, csrf: CSRF });
});

test("a failed overall operation preserves actual applied counts and separates screening failure", async ({ page }) => {
  await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [qboSource({ record_count: 15 })] }),
    [CONNECTION]: () => ({ body: connectionOf() }),
    "POST /sources/qbo-1/sync": () => ({ status: 202, body: { attempt_id: "op-partial", operation: "source_sync", status: "queued" } }),
    "GET /monitoring/operations/op-partial": () => ({ body: operationOf("op-partial", "failed", { error_code: "screening_failed", outcome: { source_id: "qbo-1", added: 3, changed: 1, deleted: 0, unchanged: 11, record_count: 15, pending_task_id: "pending-task", screening_status: "failed", failed_task_ids: ["pending-task"], run_ids: [] } }) }),
  });
  await page.goto("/sources");
  await qboRegion(page).getByRole("button", { name: "Sync changes" }).click();
  const status = operationStatus(page);
  await expect(status).toContainText("ended with status “failed”");
  await expect(status).toContainText("3 added");
  await expect(status).toContainText("15 active records");
  await expect(status).toContainText("records were saved, but screening needs attention");
  await expect(status).not.toContainText("The incremental sync finished.");
});

test("an existing unconnected source cannot begin OAuth while its app configuration is unconfirmed", async ({ page }) => {
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [qboSource({ connection_status: "configured" })] }),
    [CONNECTION]: () => ({ body: unconnected }),
    "GET /settings/connectors/qbo": () => ({ body: { ...qboSettings, sandbox: { client_id_configured: false, client_secret_configured: false, verifier_token_configured: false, configured: false } } }),
  });
  await page.goto("/sources");
  await expect(qboRegion(page).getByText("Not connected", { exact: true })).toBeVisible();
  await expect(qboRegion(page).getByRole("button", { name: "Connect to QuickBooks" })).toHaveCount(0);
  await expect(qboRegion(page).getByRole("link", { name: "Check QuickBooks app settings" })).toBeVisible();
  expect(count(calls, START)).toBe(0);
});

test("OAuth callback failures use fixed safe messages and never echo unknown query values", async ({ page }) => {
  await mockApi(page, "admin", { "GET /sources": () => ({ body: [qboSource()] }), [CONNECTION]: () => ({ body: connectionOf() }) });
  await page.goto("/sources?connection_error=company_binding_mismatch");
  await expect(page.getByRole("alert").filter({ hasText: "different QuickBooks company" })).toBeVisible();
  await page.goto("/sources?connection_error=revocation_cleanup_pending");
  await expect(page.getByRole("alert").filter({ hasText: "still being cleaned up" })).toBeVisible();
  await page.goto("/sources?connection_error=__proto__&source_id=synthetic-untrusted-query");
  await expect(page.getByRole("alert").filter({ hasText: "authorization could not be completed" })).toBeVisible();
  await expect(page.locator("body")).not.toContainText("__proto__");
  await expect(page.locator("body")).not.toContainText("synthetic-untrusted-query");
});


test("QuickBooks connection and app settings stay readable on a narrow screen", async ({ page }) => {
  const root = process.env.SCREENING_TOOL_PREVIEW_SCREENSHOTS;
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [qboSource()] }),
    [CONNECTION]: () => ({ body: connectionOf() }),
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/sources");
  await expect(qboRegion(page).getByRole("button", { name: "Full sync" })).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  if (root) await page.screenshot({ path: `${root}/milestone-6-qbo-sources-mocked.png` });
  await page.setViewportSize({ width: 390, height: 844 });
  await qboRegion(page).scrollIntoViewIfNeeded();
  await expect(qboRegion(page).getByRole("button", { name: "Full sync" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  if (root) await page.screenshot({ path: `${root}/milestone-6-qbo-sources-mobile-mocked.png` });
  await page.goto("/settings");
  const settings = page.getByRole("region", { name: "QuickBooks app settings" });
  await expect(settings.getByLabel("New client ID", { exact: true })).toHaveValue("");
  await expect(settings.getByLabel("New client secret", { exact: true })).toHaveValue("");
  await expect(settings.getByLabel("New webhook verifier token", { exact: true })).toHaveValue("");
  await settings.scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  if (root) await settings.screenshot({ path: `${root}/milestone-6-qbo-app-settings-mobile-mocked.png` });
  await page.setViewportSize({ width: 1440, height: 1000 });
  if (root) await settings.screenshot({ path: `${root}/milestone-6-qbo-app-settings-mocked.png` });
  expect(calls.some((call) => call.key.startsWith("POST") || call.key.startsWith("PATCH"))).toBe(false);
});


test("disconnect without a stored grant never claims Intuit confirmed revocation", async ({ page }) => {
  await mockApi(page, "admin", { "GET /sources": () => ({ body: [qboSource({ connection_status: "disconnected" })] }), [CONNECTION]: () => ({ body: connectionOf({ status: "disconnected", realm_id: null, revocation_status: "not_required" }) }) });
  await page.goto("/sources");
  const region = qboRegion(page);
  await expect(region.getByText("No stored QuickBooks authorization needed revocation.", { exact: true })).toBeVisible();
  await expect(region).not.toContainText("Intuit confirmed the authorization was revoked");
  await expect(region.getByRole("button", { name: "Retry Intuit revocation" })).toHaveCount(0);
});

test("an unconnected source can retry failed orphan-grant cleanup without claiming a company is connected", async ({ page }) => {
  let connection = connectionOf({ status: "configured", realm_id: null, revocation_status: "failed", revocation_error_code: "qbo_revocation_failed" });
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [qboSource({ connection_status: "configured" })] }), [CONNECTION]: () => ({ body: connection }),
    "POST /sources/qbo-1/qbo/disconnect": () => { connection = connectionOf({ status: "disconnected", realm_id: null, revocation_status: "revoked" }); return { status: 202, body: { attempt_id: "orphan-cleanup", operation: "source_disconnect", status: "queued", connection: { ...connection, status: "disconnecting", revocation_status: "pending" } } }; },
    "GET /monitoring/operations/orphan-cleanup": () => ({ body: operationOf("orphan-cleanup", "success", { operation: "source_disconnect" }) }),
  });
  await page.goto("/sources");
  const region = qboRegion(page);
  await expect(region.getByText("Not connected", { exact: true })).toBeVisible();
  await region.getByRole("button", { name: "Retry Intuit revocation" }).click();
  await expect(region.getByRole("button", { name: "Cancel retry" })).toBeVisible();
  await region.getByRole("button", { name: "Confirm disconnect" }).click();
  await expect(region.getByText("Intuit confirmed the authorization was revoked.", { exact: true })).toBeVisible();
  expect(calls.find((call) => call.key === "POST /sources/qbo-1/qbo/disconnect")).toMatchObject({ body: {}, csrf: CSRF });
});
