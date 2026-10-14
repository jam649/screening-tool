import { expect, test, type Page } from "@playwright/test";

// Assumes Sources mounts AddBcSource after the CSV and QuickBooks forms and
// BcSourceControls inside each Business Central source card. All identifiers
// below are synthetic; only same-origin API routes are intercepted.
test.use({ locale: "en-US", timezoneId: "UTC" });

const CSRF = "bc-csrf-token";
const LABEL = "Synthetic BC company source";
const TENANT = "3f2504e0-4f89-41d3-9a0c-0305e82c3301";
const COMPANY_A = "11111111-1111-4111-8111-111111111111";
const COMPANY_B = "22222222-2222-4222-8222-222222222222";
const COMPANY_C = "33333333-3333-4333-8333-333333333333";
const COMPANY_D = "44444444-4444-4444-8444-444444444444";

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
const bcSettings = (configured = true) => ({
  client_id_configured: configured,
  client_secret_configured: configured,
  configured,
  webhook_base_url: null,
});
const qboFlags = {
  client_id_configured: false,
  client_secret_configured: false,
  verifier_token_configured: false,
  configured: false,
};

const bcSource = (overrides: Record<string, unknown> = {}) => ({
  id: "bc-1",
  kind: "bc",
  label: LABEL,
  record_count: 12,
  last_synced_at: "2026-10-13T09:00:00Z",
  next_sync_at: "2026-10-14T09:15:00Z",
  health: "healthy",
  last_error: null,
  column_mapping: {},
  environment: null,
  bc_environment: "Prod_EU",
  connection_status: "connected",
  ...overrides,
});
const connectionOf = (overrides: Record<string, unknown> = {}) => ({
  source_id: "bc-1",
  tenant_id: TENANT,
  bc_environment: "Prod_EU",
  company_id: COMPANY_A,
  status: "connected",
  access_token_configured: true,
  access_expires_at: "2026-10-14T10:00:00Z",
  webhooks_enabled: false,
  webhook_url: null,
  subscription_status: "disabled",
  cleanup_status: "not_required",
  cleanup_error_code: null,
  subscriptions: [] as object[],
  ...overrides,
});
const unbound = (overrides: Record<string, unknown> = {}) =>
  connectionOf({
    status: "configured",
    company_id: null,
    access_token_configured: false,
    access_expires_at: null,
    ...overrides,
  });
const subscription = (
  resource: "customers" | "vendors",
  overrides: Record<string, unknown> = {},
) => ({
  resource,
  subscription_id: `sub-${resource}-synthetic`,
  expiration_date_time: "2026-10-17T09:00:00Z",
  status: "active",
  last_error_code: null,
  ...overrides,
});
const accepted = (attempt: string, operation: string, connection?: object) => ({
  attempt_id: attempt,
  operation,
  status: "queued",
  ...(connection ? { connection } : {}),
});
const operationOf = (
  id: string,
  status: string,
  extra: Record<string, unknown> = {},
) => ({
  id,
  attempt_id: id,
  operation: "source_sync",
  source_id: "bc-1",
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
type Call = { key: string; csrf?: string; contentType?: string; body: unknown };
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
    "GET /dashboard": () => ({ body: { total_parties: 0, csl_version: null } }),
    "GET /settings/connectors/qbo": () => ({
      body: {
        sandbox: qboFlags,
        production: qboFlags,
        redirect_uri: null,
        webhook_urls: { sandbox: null, production: null },
      },
    }),
    "GET /settings/connectors/bc": () => ({ body: bcSettings() }),
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
const bcRegion = (page: Page, label = LABEL) =>
  card(page, label).getByRole("region", { name: "Business Central connection" });
const addRegion = (page: Page) =>
  page.getByRole("region", { name: "Add a Business Central source" });
const operationStatus = (page: Page) =>
  bcRegion(page).getByRole("status", {
    name: "Business Central operation status",
  });

const CONNECTION = "GET /sources/bc-1/bc/connection";
const COMPANIES = "POST /sources/bc-1/bc/companies";
const CONNECT = "POST /sources/bc-1/bc/connect";
const SYNC = "POST /sources/bc-1/sync";
const PATCH = "PATCH /sources/bc-1/bc/connection";
const RECONCILE = "POST /sources/bc-1/bc/subscriptions/reconcile";
const DISCONNECT = "POST /sources/bc-1/bc/disconnect";

test("an Admin adds a source with a canonical tenant, case-exact environment and CSRF; invalid values never post", async ({
  page,
}) => {
  const sources: object[] = [];
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: sources }),
    "POST /sources": () => {
      const created = bcSource({
        record_count: 0,
        last_synced_at: null,
        next_sync_at: null,
        health: "not_synced",
        bc_environment: "Prod_EU-2",
        connection_status: "configured",
      });
      sources.push(created);
      return { status: 201, body: created };
    },
    [CONNECTION]: () => ({ body: unbound({ bc_environment: "Prod_EU-2" }) }),
  });
  await page.goto("/sources");
  const add = addRegion(page);
  const submit = add.getByRole("button", { name: "Add Business Central source" });
  const tenant = add.getByLabel("Tenant ID");
  const environment = add.getByLabel("Environment name");
  await expect(add).toContainText("admin consent");
  await expect(add.getByRole("button", { name: /sign in|microsoft/i })).toHaveCount(0);
  await expect(add.getByRole("combobox")).toHaveCount(0);

  await add.getByLabel("Source name").fill(`  ${LABEL}  `);
  await tenant.fill("not-a-tenant-uuid");
  await environment.fill("Prod_EU-2");
  await expect(tenant).toHaveAttribute("aria-invalid", "true");
  await expect(add).toContainText("8-4-4-4-12");
  await expect(submit).toBeDisabled();
  await tenant.press("Enter");

  await tenant.fill(TENANT.toUpperCase());
  await expect(tenant).not.toHaveAttribute("aria-invalid", "true");
  for (const bad of ["9Prod", "Prod EU", "Prod.EU", `P${"a".repeat(29)}`]) {
    await environment.fill(bad);
    await expect(environment).toHaveAttribute("aria-invalid", "true");
    await expect(submit).toBeDisabled();
    await environment.press("Enter");
  }
  expect(count(calls, "POST /sources")).toBe(0);

  await environment.fill("Prod_EU-2");
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(add.getByRole("status")).toContainText(
    `“${LABEL}” was added but is not connected yet.`,
  );
  const post = calls.find((call) => call.key === "POST /sources");
  expect(post?.body).toEqual({
    kind: "bc",
    label: LABEL,
    tenant_id: TENANT,
    bc_environment: "Prod_EU-2",
  });
  expect(post?.csrf).toBe(CSRF);
  expect(post?.contentType).toBe("application/json");
  await expect(add.getByLabel("Source name")).toHaveValue("");

  const region = bcRegion(page);
  await expect(region.getByText("Not connected", { exact: true })).toBeVisible();
  await expect(region).toContainText("Prod_EU-2");
  await expect(region).toContainText("No company connected yet");
  await expect(region.getByRole("button", { name: "Discover companies", exact: true })).toBeEnabled();
  await expect(region.getByRole("button", { name: /Connect company|Sync|Disconnect|Reconnect/ })).toHaveCount(0);
  await expect(region.getByRole("combobox")).toHaveCount(0);
  await page.waitForTimeout(500);
  expect(calls.filter((call) => call.key.startsWith("POST")).map((call) => call.key)).toEqual([
    "POST /sources",
  ]);
});

test("an unconfigured app blocks adding and discovery and links to Settings", async ({ page }) => {
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [bcSource({ connection_status: "configured" })] }),
    [CONNECTION]: () => ({ body: unbound() }),
    "GET /settings/connectors/bc": () => ({ body: bcSettings(false) }),
  });
  await page.goto("/sources");
  const add = addRegion(page);
  await add.getByLabel("Source name").fill("Second synthetic source");
  await add.getByLabel("Tenant ID").fill(TENANT);
  await add.getByLabel("Environment name").fill("Sandbox2");
  await expect(add.getByRole("button", { name: "Add Business Central source" })).toBeDisabled();
  await expect(
    add.getByRole("link", { name: "Configure the Business Central app in Settings" }),
  ).toHaveAttribute("href", "/settings");
  const region = bcRegion(page);
  await expect(region.getByText("Not connected", { exact: true })).toBeVisible();
  await expect(
    region.getByRole("link", { name: "Check Business Central app settings" }),
  ).toHaveAttribute("href", "/settings");
  await expect(region.getByRole("button", { name: /Discover/ })).toHaveCount(0);
  await add.getByLabel("Source name").press("Enter");
  expect(calls.filter((call) => call.key.startsWith("POST"))).toEqual([]);
});

test("companies are discovered only on request and only the explicitly selected UUID is connected", async ({
  page,
}) => {
  let connection = unbound();
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({
      body: [bcSource({ connection_status: connection.status, record_count: 0, health: "not_synced" })],
    }),
    [CONNECTION]: () => ({ body: connection }),
    [COMPANIES]: () => ({
      body: {
        items: [
          { id: COMPANY_A, display_name: "Synthetic Trading" },
          { id: COMPANY_B, display_name: "Synthetic Trading" },
          { id: COMPANY_C, display_name: "  " },
          { id: COMPANY_D, display_name: "Synthetic Unique Co" },
        ],
      },
    }),
    [CONNECT]: () => {
      connection = connectionOf({ company_id: COMPANY_B });
      return { status: 202, body: accepted("op-connect", "source_sync", connection) };
    },
    "GET /monitoring/operations/op-connect": sequence(
      { body: operationOf("op-connect", "queued") },
      { body: operationOf("op-connect", "running") },
      {
        body: operationOf("op-connect", "success", {
          finished_at: "2026-10-14T08:01:00Z",
          outcome: {
            source_id: "bc-1",
            added: 3,
            changed: 0,
            deleted: 0,
            unchanged: 0,
            record_count: 3,
            pending_task_id: "task-synthetic-1",
            run_ids: [],
            failed_task_ids: [],
            screening_attempt_id: "screen-synthetic-1",
            screening_status: "queued",
          },
        }),
      },
    ),
  });
  await page.goto("/sources");
  const region = bcRegion(page);
  await expect(region.getByText("Not connected", { exact: true })).toBeVisible();
  await page.waitForTimeout(500);
  expect(count(calls, COMPANIES)).toBe(0);

  await region.getByRole("button", { name: "Discover companies", exact: true }).click();
  const select = region.getByLabel("Company to connect");
  await expect(select.locator("option")).toHaveText([
    "Choose a company",
    `Synthetic Trading (${COMPANY_A})`,
    `Synthetic Trading (${COMPANY_B})`,
    `${COMPANY_C} (no display name)`,
    "Synthetic Unique Co",
  ]);
  await expect(select).toHaveValue("");
  const connect = region.getByRole("button", { name: "Connect company" });
  await expect(connect).toBeDisabled();
  expect(count(calls, CONNECT)).toBe(0);
  const discovery = calls.find((call) => call.key === COMPANIES);
  expect(discovery).toMatchObject({ body: {}, csrf: CSRF });

  await select.selectOption(COMPANY_B);
  await expect(region).toContainText(`Company ID ${COMPANY_B}`);
  await connect.click();
  const status = operationStatus(page);
  await expect(status).toContainText("first full sync is queued");
  await expect(status).toContainText("Nothing is imported until it finishes.");
  await expect(status).not.toContainText("finished");
  await expect(region.getByText("Connected", { exact: true })).toBeVisible();
  await expect(region.getByRole("combobox")).toHaveCount(0);
  await expect(region.getByRole("button", { name: "Full sync" })).toBeDisabled();
  const sourceReads = count(calls, "GET /sources");

  await expect(status).toContainText("The first full sync finished.", { timeout: 10_000 });
  await expect(status).toContainText("3 added");
  await expect(status).toContainText("3 active records");
  await expect(status).toContainText("Screening of new and changed parties is queued");
  await expect(status).not.toContainText("Screening finished");
  await expect.poll(() => count(calls, "GET /sources")).toBeGreaterThan(sourceReads);
  await expect(region.getByRole("button", { name: "Full sync" })).toBeEnabled();

  const connects = calls.filter((call) => call.key === CONNECT);
  expect(connects).toHaveLength(1);
  expect(connects[0].body).toEqual({ company_id: COMPANY_B });
  expect(connects[0].csrf).toBe(CSRF);
  expect(count(calls, COMPANIES)).toBe(1);
});

test("failed and empty discovery leave the source unchanged and can be retried", async ({ page }) => {
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [bcSource({ connection_status: "configured" })] }),
    [CONNECTION]: () => ({ body: unbound() }),
    [COMPANIES]: sequence(
      { status: 409, body: { detail: "Token request to https://login.example.test/x failed with client_secret." } },
      { body: { items: [] } },
    ),
  });
  await page.goto("/sources");
  const region = bcRegion(page);
  await region.getByRole("button", { name: "Discover companies", exact: true }).click();
  const alert = region.getByRole("alert");
  await expect(alert).toContainText("Companies couldn’t be discovered.");
  await expect(alert).toContainText("tenant and environment are unchanged");
  await expect(alert).not.toContainText("https://");
  await expect(alert).not.toContainText("client_secret");
  await expect(region).toContainText(TENANT);
  await expect(region).toContainText("Prod_EU");
  await expect(region.getByText("Not connected", { exact: true })).toBeVisible();

  await region.getByRole("button", { name: "Discover companies again" }).click();
  await expect(region).toContainText("No companies were returned.");
  await expect(alert).toHaveCount(0);
  await expect(region.getByRole("combobox")).toHaveCount(0);
  await expect(region.getByRole("button", { name: "Connect company" })).toHaveCount(0);
  expect(count(calls, COMPANIES)).toBe(2);
  expect(count(calls, CONNECT)).toBe(0);
});

test("a bound company reconnects with its own ID and a different company needs a new source", async ({
  page,
}) => {
  let connection = connectionOf({
    status: "disconnected",
    access_token_configured: false,
    access_expires_at: null,
  });
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [bcSource({ connection_status: connection.status })] }),
    [CONNECTION]: () => ({ body: connection }),
    [CONNECT]: () => {
      connection = connectionOf();
      return { status: 202, body: accepted("op-reconnect", "source_sync", connection) };
    },
    "GET /monitoring/operations/op-reconnect": () => ({
      body: operationOf("op-reconnect", "running"),
    }),
  });
  await page.goto("/sources");
  let region = bcRegion(page);
  await expect(region.getByText("Disconnected", { exact: true })).toBeVisible();
  await expect(region.getByText("No subscriptions needed cleanup.", { exact: true })).toBeVisible();
  await expect(region.getByRole("button", { name: "Reconnect", exact: true })).toBeEnabled();

  connection = connectionOf({ status: "reconnect_required" });
  await page.reload();
  region = bcRegion(page);
  await expect(region.getByText("Reconnect required", { exact: true })).toBeVisible();
  await expect(region).toContainText(`company ID ${COMPANY_A}`);
  await expect(region).toContainText("add a new Business Central source");
  await expect(region.getByRole("button", { name: /Discover/ })).toHaveCount(0);
  await expect(region.getByRole("combobox")).toHaveCount(0);
  await expect(region.getByRole("button", { name: /Sync/ })).toHaveCount(0);
  await region.getByRole("button", { name: "Reconnect", exact: true }).click();
  await expect(operationStatus(page)).toContainText("is running");
  await expect(region.getByRole("button", { name: "Full sync" })).toBeDisabled();
  const connects = calls.filter((call) => call.key === CONNECT);
  expect(connects).toHaveLength(1);
  expect(connects[0]).toMatchObject({ body: { company_id: COMPANY_A }, csrf: CSRF });
  expect(count(calls, COMPANIES)).toBe(0);
});

test("a read error stops polling, Check status again reads without posting, and failures keep server counts", async ({
  page,
}) => {
  const OP = "GET /monitoring/operations/op-sync-1";
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [bcSource()] }),
    [CONNECTION]: () => ({ body: connectionOf() }),
    [SYNC]: () => ({ status: 202, body: accepted("op-sync-1", "source_sync") }),
    [OP]: sequence(
      { status: 503, body: { detail: "Operation lookup failed." } },
      { body: operationOf("op-sync-1", "running") },
      {
        body: operationOf("op-sync-1", "failed", {
          finished_at: "2026-10-14T08:01:00Z",
          error_code: "bc_timeout",
          outcome: {
            source_id: "bc-1",
            added: 2,
            changed: 1,
            deleted: 0,
            unchanged: 9,
            record_count: 14,
            run_ids: [],
            failed_task_ids: ["task-synthetic-2"],
            screening_status: "failed",
          },
        }),
      },
    ),
  });
  await page.goto("/sources");
  const region = bcRegion(page);
  await region.getByRole("button", { name: "Sync changes" }).click();
  const lookup = region.getByRole("alert").filter({ hasText: "Operation lookup failed." });
  await expect(lookup).toBeVisible();
  const status = operationStatus(page);
  await expect(status).toContainText("last reported as “queued”");
  await expect(status).toContainText("isn’t known to be complete");
  await expect(region.getByRole("button", { name: "Full sync" })).toBeDisabled();
  await expect(region.getByRole("button", { name: "Disconnect" })).toBeDisabled();
  await page.waitForTimeout(2000);
  expect(count(calls, OP)).toBe(1);

  await lookup.getByRole("button", { name: "Check status again" }).click();
  await expect(status).toContainText("The incremental sync ended with status “failed”.", {
    timeout: 10_000,
  });
  await expect(status).toContainText("bc_timeout");
  await expect(status).toContainText("2 added");
  await expect(status).toContainText("14 active records");
  await expect(status).toContainText("records were saved, but screening needs attention");
  await expect(status).not.toContainText("The incremental sync finished.");
  await expect(region.getByRole("button", { name: "Full sync" })).toBeEnabled();
  const reads = count(calls, OP);
  await page.waitForTimeout(2000);
  expect(count(calls, OP)).toBe(reads);
  const posts = calls.filter((call) => call.key === SYNC);
  expect(posts).toHaveLength(1);
  expect(posts[0]).toMatchObject({ body: { mode: "incremental" }, csrf: CSRF });
});

test("a successful screening outranks a retained task ID and only listed runs are counted", async ({ page }) => {
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [bcSource()] }),
    [CONNECTION]: () => ({ body: connectionOf() }),
    [SYNC]: () => ({ status: 202, body: accepted("op-complete", "source_sync") }),
    "GET /monitoring/operations/op-complete": () => ({
      body: operationOf("op-complete", "success", {
        outcome: {
          source_id: "bc-1",
          added: 1,
          changed: 0,
          deleted: 0,
          unchanged: 12,
          record_count: 13,
          pending_task_id: "already-completed-task",
          run_ids: ["run-synthetic-1", "run-synthetic-2"],
          failed_task_ids: [],
          screening_status: "success",
        },
      }),
    }),
  });
  await page.goto("/sources");
  await bcRegion(page).getByRole("button", { name: "Full sync" }).click();
  const status = operationStatus(page);
  await expect(status).toContainText("The full sync finished.");
  await expect(status).toContainText(
    "Screening finished for new and changed parties and recorded 2 runs",
  );
  await expect(status).not.toContainText(/is queued/);
  expect(calls.find((call) => call.key === SYNC)).toMatchObject({
    body: { mode: "full" },
    csrf: CSRF,
  });
});

test("subscriptions show only listed entries; a failed reconcile differs from a full success", async ({
  page,
}) => {
  const degraded = connectionOf({
    webhooks_enabled: true,
    webhook_url: "https://screening.example.test/api/v1/webhooks/bc/bc-1",
    subscription_status: "degraded",
    subscriptions: [
      subscription("customers"),
      subscription("vendors", {
        status: "expired",
        expiration_date_time: "2026-10-13T09:00:00Z",
        last_error_code: "subscription_expired",
      }),
    ],
  });
  let connection = degraded;
  let secondReads = 0;
  let reconciles = 0;
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [bcSource()] }),
    [CONNECTION]: () => ({ body: connection }),
    [RECONCILE]: () => {
      reconciles++;
      return {
        status: 202,
        body: accepted(`op-r${reconciles}`, "bc_subscriptions", connection),
      };
    },
    "GET /monitoring/operations/op-r1": sequence(
      { body: operationOf("op-r1", "queued", { operation: "bc_subscriptions" }) },
      {
        body: operationOf("op-r1", "failed", {
          operation: "bc_subscriptions",
          error_code: "bc_subscription_conflict",
        }),
      },
    ),
    "GET /monitoring/operations/op-r2": () => {
      secondReads++;
      if (secondReads === 1)
        return { body: operationOf("op-r2", "running", { operation: "bc_subscriptions" }) };
      connection = connectionOf({
        ...degraded,
        subscription_status: "active",
        subscriptions: [
          subscription("customers", { expiration_date_time: "2026-10-17T12:00:00Z" }),
          subscription("vendors", {
            subscription_id: "sub-vendors-renewed",
            expiration_date_time: "2026-10-17T12:00:00Z",
          }),
        ],
      });
      return { body: operationOf("op-r2", "success", { operation: "bc_subscriptions" }) };
    },
  });
  await page.goto("/sources");
  const region = bcRegion(page);
  const table = region.getByRole("table", { name: "Business Central subscriptions" });
  await expect(table.getByRole("row")).toHaveCount(3);
  await expect(table).toContainText("sub-customers-synthetic");
  await expect(table).toContainText("Oct 17, 2026");
  await expect(table).toContainText("Expired");
  await expect(table).toContainText("subscription_expired");
  await expect(region.getByText("Subscriptions degraded", { exact: true })).toBeVisible();
  await expect(region.getByRole("checkbox", { name: /change notifications/ })).toBeChecked();
  await expect(region).toContainText("https://screening.example.test/api/v1/webhooks/bc/bc-1");
  await expect(region.getByRole("textbox")).toHaveCount(0);

  const reconcile = region.getByRole("button", { name: "Reconcile subscriptions" });
  await reconcile.click();
  const status = operationStatus(page);
  await expect(status).toContainText("The subscription reconcile is queued.");
  await expect(reconcile).toBeDisabled();
  await expect(status).toContainText("ended with status “failed”", { timeout: 10_000 });
  await expect(status).toContainText("bc_subscription_conflict");
  await expect(region.getByText("Subscriptions degraded", { exact: true })).toBeVisible();
  await expect(reconcile).toBeEnabled();

  await reconcile.click();
  await expect(status).toContainText("is running");
  await expect(region.getByText("Subscriptions degraded", { exact: true })).toBeVisible();
  await expect(status).toContainText("The subscription reconcile finished.", { timeout: 10_000 });
  await expect(region.getByText("Subscriptions active", { exact: true })).toBeVisible();
  await expect(table).toContainText("sub-vendors-renewed");
  await expect(table).not.toContainText("Expired");
  await expect(table.getByRole("row")).toHaveCount(3);
  const posts = calls.filter((call) => call.key === RECONCILE);
  expect(posts).toHaveLength(2);
  expect(posts.every((call) => call.csrf === CSRF && JSON.stringify(call.body) === "{}")).toBe(true);
});

test("a rejected webhook change stays unchecked; an accepted one queues pending setup, not active", async ({
  page,
}) => {
  let connection = connectionOf();
  let reads = 0;
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [bcSource()] }),
    [CONNECTION]: () => ({ body: connection }),
    [PATCH]: sequence(
      {
        status: 422,
        body: { detail: "Webhooks need a connected company and a public HTTPS address." },
      },
      {
        status: 202,
        body: accepted(
          "op-webhooks",
          "bc_subscriptions",
          connectionOf({ webhooks_enabled: true, subscription_status: "pending" }),
        ),
      },
    ),
    "GET /monitoring/operations/op-webhooks": () => {
      reads++;
      if (reads === 1)
        return { body: operationOf("op-webhooks", "queued", { operation: "bc_subscriptions" }) };
      connection = connectionOf({
        webhooks_enabled: true,
        subscription_status: "active",
        subscriptions: [subscription("customers"), subscription("vendors")],
      });
      return { body: operationOf("op-webhooks", "success", { operation: "bc_subscriptions" }) };
    },
  });
  await page.goto("/sources");
  const region = bcRegion(page);
  const checkbox = region.getByRole("checkbox", { name: /change notifications/ });
  await expect(checkbox).not.toBeChecked();
  await expect(region.getByText("Webhooks off", { exact: true })).toBeVisible();
  await expect(region).toContainText("Scheduled polling continues either way.");
  await expect(region.getByLabel(/client ?state|verifier/i)).toHaveCount(0);

  await checkbox.click();
  const alert = region.getByRole("alert");
  await expect(alert).toContainText("The notification setting wasn’t changed.");
  await expect(alert).toContainText("public HTTPS address");
  await expect(checkbox).not.toBeChecked();
  await expect(region.getByRole("button", { name: "Sync changes" })).toBeEnabled();

  await checkbox.click();
  await expect(checkbox).toBeChecked();
  await expect(alert).toHaveCount(0);
  await expect(region.getByText("Subscriptions pending", { exact: true })).toBeVisible();
  await expect(region.getByText("Subscriptions active", { exact: true })).toHaveCount(0);
  await expect(operationStatus(page)).toContainText("The webhook change is queued.");
  await expect(operationStatus(page)).toContainText("The webhook change finished.", {
    timeout: 10_000,
  });
  await expect(region.getByText("Subscriptions active", { exact: true })).toBeVisible();
  await expect(
    region.getByRole("table", { name: "Business Central subscriptions" }).getByRole("row"),
  ).toHaveCount(3);
  const patches = calls.filter((call) => call.key === PATCH);
  expect(patches.map((call) => call.body)).toEqual([
    { webhooks_enabled: true },
    { webhooks_enabled: true },
  ]);
  expect(patches.every((call) => call.csrf === CSRF)).toBe(true);
});

test("disconnect keeps history, reports failed cleanup honestly and retries it through disconnect", async ({
  page,
}) => {
  let connection = connectionOf({
    webhooks_enabled: true,
    subscription_status: "active",
    subscriptions: [subscription("customers"), subscription("vendors")],
  });
  let posts = 0;
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [bcSource({ connection_status: connection.status })] }),
    [CONNECTION]: () => ({ body: connection }),
    [DISCONNECT]: () => {
      posts++;
      if (posts === 1) {
        const echo = connectionOf({
          ...connection,
          status: "disconnecting",
          subscription_status: "cleanup_pending",
          cleanup_status: "pending",
        });
        connection = connectionOf({
          status: "disconnected",
          access_token_configured: false,
          access_expires_at: null,
          webhooks_enabled: false,
          subscription_status: "cleanup_pending",
          cleanup_status: "failed",
          cleanup_error_code: "subscription_delete_503",
          subscriptions: [
            subscription("customers", { status: "failed", last_error_code: "subscription_delete_503" }),
            subscription("vendors", { status: "failed", last_error_code: "subscription_delete_503" }),
          ],
        });
        return { status: 202, body: accepted("op-disconnect-1", "source_disconnect", echo) };
      }
      connection = connectionOf({
        status: "disconnected",
        access_token_configured: false,
        access_expires_at: null,
        cleanup_status: "completed",
        subscriptions: [],
      });
      return { status: 202, body: accepted("op-disconnect-2", "source_disconnect") };
    },
    "GET /monitoring/operations/op-disconnect-1": sequence(
      { body: operationOf("op-disconnect-1", "running", { operation: "source_disconnect" }) },
      { body: operationOf("op-disconnect-1", "success", { operation: "source_disconnect" }) },
    ),
    "GET /monitoring/operations/op-disconnect-2": () => ({
      body: operationOf("op-disconnect-2", "success", { operation: "source_disconnect" }),
    }),
  });
  await page.goto("/sources");
  const region = bcRegion(page);
  await region.getByRole("button", { name: "Disconnect" }).click();
  const confirm = region.getByRole("group", { name: "Confirm disconnect" });
  await expect(confirm).toContainText("Scheduled polling stops immediately.");
  await expect(confirm).toContainText("Parties, screening history and audit records from this source are kept.");
  await expect(confirm).toContainText("doesn’t remove admin consent");
  expect(count(calls, DISCONNECT)).toBe(0);

  await confirm.getByRole("button", { name: "Confirm disconnect" }).click();
  await expect(region.getByText("Disconnecting", { exact: true })).toBeVisible();
  await expect(region).toContainText("Subscription cleanup is pending.");
  await expect(region.getByRole("button", { name: /Sync|Reconnect|Discover/ })).toHaveCount(0);
  await expect(region.getByRole("checkbox")).toHaveCount(0);

  const status = operationStatus(page);
  await expect(status).toContainText("The disconnect finished.", { timeout: 10_000 });
  await expect(region.getByText("Disconnected", { exact: true })).toBeVisible();
  await expect(region).toContainText("Subscription cleanup failed.");
  await expect(region).toContainText("subscription_delete_503");
  await expect(region).not.toContainText("Subscription cleanup completed.");
  await expect(region.getByRole("button", { name: /Reconnect|Discover/ })).toHaveCount(0);
  await expect(card(page).getByText("12", { exact: true })).toBeVisible();
  await expect(region).not.toContainText(/revoked|revocation/i);
  await expect(region.getByRole("button", { name: /sign in|microsoft/i })).toHaveCount(0);

  await region.getByRole("button", { name: "Retry subscription cleanup" }).click();
  const retry = region.getByRole("group", { name: "Confirm cleanup retry" });
  await retry.getByRole("button", { name: "Confirm cleanup retry" }).click();
  await expect(status).toContainText("The subscription cleanup retry finished.");
  await expect(region.getByText("Subscription cleanup completed.", { exact: true })).toBeVisible();
  await expect(region.getByRole("button", { name: "Reconnect", exact: true })).toBeEnabled();
  const disconnects = calls.filter((call) => call.key === DISCONNECT);
  expect(disconnects).toHaveLength(2);
  expect(disconnects.every((call) => call.csrf === CSRF && JSON.stringify(call.body) === "{}")).toBe(true);
});

test("a Reviewer sees public source health only and makes no Business Central requests", async ({
  page,
}) => {
  const calls = await mockApi(page, "reviewer", {
    "GET /sources": () => ({ body: [bcSource()] }),
  });
  await page.goto("/sources");
  const bc = card(page);
  await expect(bc).toContainText("Healthy");
  await expect(bc.getByText("12", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Business Central connection" })).toHaveCount(0);
  await expect(addRegion(page)).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: /Discover|Connect|Sync|Disconnect|Reconcile/ }),
  ).toHaveCount(0);
  await expect(bc).not.toContainText(TENANT);
  await page.waitForTimeout(500);
  const private_ = calls.filter((call) =>
    /\/bc\/|\/settings\/connectors|\/monitoring\/operations|\/sync$/.test(call.key),
  );
  expect(private_).toEqual([]);
});


test("Business Central connection facts and subscription table fit a narrow viewport", async ({ page }, testInfo) => {
  await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [bcSource()] }),
    [CONNECTION]: () => ({ body: connectionOf({
      webhooks_enabled: true,
      webhook_url: "https://screening.example.test/api/v1/webhooks/bc/11111111-1111-4111-8111-111111111111",
      subscription_status: "degraded",
      subscriptions: [subscription("customers"), subscription("vendors", { status: "expired" })],
    }) }),
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/sources");
  const region = bcRegion(page);
  await expect(region.getByRole("table", { name: "Business Central subscriptions" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  const tableRegion = region.getByRole("region", { name: "Business Central subscriptions" });
  await tableRegion.focus();
  await expect(tableRegion).toBeFocused();
  await card(page).evaluate((element) => element.scrollIntoView({ block: "start" }));
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
  await page.screenshot({ path: testInfo.outputPath("milestone-7-mocked-bc-source-mobile.png"), fullPage: false });
  await page.setViewportSize({ width: 1440, height: 950 });
  await card(page).evaluate((element) => element.scrollIntoView({ block: "start" }));
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
  await page.screenshot({ path: testInfo.outputPath("milestone-7-mocked-bc-source.png"), fullPage: false });
});

test("discovery-only cached app access can be cleared without binding or importing a company", async ({ page }) => {
  let connection = unbound();
  const calls = await mockApi(page, "admin", {
    "GET /sources": () => ({ body: [bcSource({ record_count: 0, connection_status: connection.status, next_sync_at: null })] }),
    [CONNECTION]: () => ({ body: connection }),
    [COMPANIES]: () => {
      connection = unbound({ access_token_configured: true, access_expires_at: "2026-10-14T10:00:00Z" });
      return { body: { items: [{ id: COMPANY_A, display_name: "Synthetic company" }] } };
    },
    [DISCONNECT]: () => {
      connection = unbound({ status: "disconnecting", access_token_configured: true });
      return { status: 202, body: accepted("op-clear-access", "source_disconnect", connection) };
    },
    "GET /monitoring/operations/op-clear-access": sequence(
      { body: operationOf("op-clear-access", "queued", { operation: "source_disconnect" }) },
      { body: operationOf("op-clear-access", "running", { operation: "source_disconnect" }) },
      { body: operationOf("op-clear-access", "success", { operation: "source_disconnect" }) },
    ),
  });
  await page.goto("/sources");
  const region = bcRegion(page);
  await expect(region.getByRole("button", { name: "Clear stored app access", exact: true })).toHaveCount(0);
  await region.getByRole("button", { name: "Discover companies", exact: true }).click();
  const clear = region.getByRole("button", { name: "Clear stored app access", exact: true });
  await expect(clear).toBeEnabled();
  await expect(region.getByRole("combobox", { name: "Company to connect" })).toHaveValue("");
  await clear.click();
  const confirm = region.getByRole("group", { name: "Confirm clear stored app access", exact: true });
  await expect(confirm).toContainText("without connecting a company or importing records");
  await expect(confirm).toContainText("doesn’t remove admin consent");
  await confirm.getByRole("button", { name: "Confirm clear stored app access", exact: true }).click();
  await expect(operationStatus(page)).toContainText("The stored app access clear is queued.");
  await expect(region.getByRole("combobox")).toHaveCount(0);
  connection = unbound({ status: "disconnected", access_token_configured: false });
  await expect(operationStatus(page)).toContainText("The stored app access clear finished.", { timeout: 10_000 });
  await expect(region.getByText("Disconnected", { exact: true })).toBeVisible();
  await expect(region.getByRole("button", { name: "Clear stored app access", exact: true })).toHaveCount(0);
  await expect(region.getByRole("button", { name: "Discover companies", exact: true })).toBeEnabled();
  expect(calls.find(call => call.key === DISCONNECT)).toMatchObject({ body: {}, csrf: CSRF });
  expect(count(calls, CONNECT)).toBe(0); expect(count(calls, SYNC)).toBe(0);
});
