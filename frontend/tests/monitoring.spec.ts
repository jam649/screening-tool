import { expect, test, type Page } from "@playwright/test";

// App route that mounts AdminSettings (and so MonitoringPanel); Home mounts MonitoringSummary.
const SETTINGS_ROUTE = "/settings";
const CSRF = "csrf-test-token";

type Reply = { status?: number; body?: unknown };
type Routes = Record<string, () => Reply>;
type Call = { key: string; csrf?: string; body: string | null };

const user = (role: "admin" | "reviewer") => ({
  id: `user-${role}`, email: `${role}@example.test`, display_name: `Test ${role}`, role, active: true,
});
const workspace = {
  display_name: "Synthetic workspace", timezone: "America/Chicago", retention_years: 5,
  auto_clear_enabled: false, auto_clear_threshold: 85, auto_clear_acknowledgment_audit_id: null,
  candidate_min: 60, review_threshold: 80, likely_threshold: 90,
  score_weights: {
    token_set: 0.4, token_sort: 0.3, partial: 0.3, country_match: 5, country_mismatch: -5,
    city_match: 3, address_max: 5, entity_mismatch: -5, short_name: -5,
  },
  legal_suffixes: [], candidate_stopwords: [], enabled_source_lists: null,
  disclaimer: "Synthetic notice.", disclaimer_url: "https://example.test/notice",
};
const monitoringSettings = {
  csl_fetch_time: "05:00", source_poll_minutes: 15, retention_weekday: "sun", retention_time: "03:00",
};
const statusFixture = (extra: object = {}) => ({
  enabled: true,
  schedules: [
    { id: "sched-1", operation: "csl_fetch", next_run_at: "2026-10-14T10:00:00Z" },
    { id: "sched-2", operation: "source_poll", next_run_at: null },
  ],
  operations: [],
  health: [],
  ...extra,
});
const operation = (id: string, status: string, extra: object = {}) => ({
  id, operation: "monitor_cycle", status, started_at: status === "queued" ? null : "2026-10-13T12:00:00Z",
  finished_at: null, error_code: null, outcome: null, ...extra,
});
const sequence = (...replies: Reply[]) => {
  let index = 0;
  return () => replies[Math.min(index++, replies.length - 1)];
};

async function mockApi(page: Page, role: "admin" | "reviewer", routes: Routes = {}) {
  const calls: Call[] = [];
  const base: Routes = {
    "GET /notifications/status": () => ({ body: { batches: [], health: [] } }),
    "GET /setup": () => ({ body: { needs_setup: false, product_name: "Screening", disclaimer: "Synthetic notice.", disclaimer_url: "https://example.test/notice" } }),
    "GET /auth/me": () => ({ body: { user: user(role), csrf_token: CSRF } }),
    "GET /settings": () => ({ body: workspace }),
    "GET /users": () => ({ body: { items: [user(role)], invitations: [] } }),
    "GET /settings/monitoring": () => ({ body: monitoringSettings }),
    "GET /monitoring/status": () => ({ body: statusFixture() }),
  };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const key = `${request.method()} ${new URL(request.url()).pathname.replace(/^\/api\/v1/, "")}`;
    calls.push({ key, csrf: request.headers()["x-csrf-token"], body: request.postData() });
    const reply = (routes[key] ?? base[key])?.() ?? { status: 404, body: { detail: "Not mocked." } };
    await route.fulfill({ status: reply.status ?? 200, contentType: "application/json", body: JSON.stringify(reply.body ?? {}) });
  });
  return calls;
}

const monitoringPanel = (page: Page) => page.getByRole("region", { name: "Monitoring and schedules" });

test("Admin schedule shows real facts, sends only changed fields and keeps edits on 422", async ({ page }) => {
  const calls = await mockApi(page, "admin", {
    "PATCH /settings/monitoring": sequence(
      { status: 422, body: { detail: [{ msg: "source_poll_minutes must be at most 1440" }] } },
      { body: { ...monitoringSettings, source_poll_minutes: 30 } },
    ),
  });
  await page.goto(SETTINGS_ROUTE);
  const panel = monitoringPanel(page);
  await expect(panel.getByText("America/Chicago", { exact: true })).toBeVisible();
  await expect(panel.getByText("Enabled", { exact: true })).toBeVisible();
  const upcoming = panel.getByRole("table", { name: "Upcoming runs" });
  await expect(upcoming.getByRole("row", { name: /Source polling/ })).toContainText("Not scheduled");
  await expect(upcoming.getByRole("row", { name: /CSL download/ })).not.toContainText("Not scheduled");

  const poll = panel.getByRole("group", { name: "Schedule settings" }).getByLabel("Source poll interval (minutes)");
  await expect(poll).toHaveValue("15");
  await poll.fill("5000");
  await panel.getByRole("button", { name: "Save schedule" }).click();
  await expect(panel.getByRole("alert")).toContainText("at most 1440");
  await expect(poll).toHaveValue("5000");

  await poll.fill("30");
  await panel.getByRole("button", { name: "Save schedule" }).click();
  await expect(panel.getByRole("status").filter({ hasText: "Schedule saved." })).toBeVisible();
  await expect(poll).toHaveValue("30");
  const patches = calls.filter((call) => call.key === "PATCH /settings/monitoring");
  expect(patches.map((call) => JSON.parse(call.body ?? "null"))).toEqual([
    { source_poll_minutes: 5000 },
    { source_poll_minutes: 30 },
  ]);
  expect(patches.every((call) => call.csrf === CSRF)).toBe(true);
});

test("Admin cycle reports queued and running before success, then refreshes health", async ({ page }) => {
  const calls = await mockApi(page, "admin", {
    "POST /monitoring/cycle": () => ({ status: 202, body: { attempt_id: "op-1", status: "queued", operation: "monitor_cycle" } }),
    "GET /monitoring/operations/op-1": sequence(
      { body: operation("op-1", "queued") },
      { body: operation("op-1", "running") },
      { body: operation("op-1", "success", { finished_at: "2026-10-13T12:01:00Z", outcome: { sources_polled: 2, deltas_screened: 3 } }) },
    ),
  });
  await page.goto(SETTINGS_ROUTE);
  const panel = monitoringPanel(page);
  await panel.getByRole("button", { name: "Run monitoring cycle" }).click();
  const cycle = panel.getByRole("status", { name: "Cycle status" });
  await expect(cycle).toContainText("queued");
  await expect(cycle).not.toContainText("successfully");
  await expect(cycle).toContainText("running");
  await expect(cycle).not.toContainText("successfully");
  await expect(cycle).toContainText("finished successfully");
  await expect(cycle).toContainText("sources polled: 2");
  expect(calls.find((call) => call.key === "POST /monitoring/cycle")?.csrf).toBe(CSRF);
  await expect.poll(() => calls.filter((call) => call.key === "GET /monitoring/status").length).toBeGreaterThan(1);
});

test("Read errors stop polling and offer retry; failed cycles are never shown as success", async ({ page }) => {
  const calls = await mockApi(page, "admin", {
    "GET /monitoring/status": sequence({ status: 500, body: { detail: "Monitoring status unavailable." } }, { body: statusFixture() }),
    "POST /monitoring/cycle": () => ({ status: 202, body: { attempt_id: "op-2", status: "queued", operation: "monitor_cycle" } }),
    "GET /monitoring/operations/op-2": sequence(
      { status: 503, body: { detail: "Operation lookup failed." } },
      { body: operation("op-2", "failed", { finished_at: "2026-10-13T12:01:00Z", error_code: "source_timeout" }) },
    ),
  });
  await page.goto(SETTINGS_ROUTE);
  const panel = monitoringPanel(page);
  await expect(panel.getByRole("alert")).toContainText("Monitoring status unavailable.");
  await panel.getByRole("button", { name: "Retry" }).click();
  await expect(panel.getByText("Enabled", { exact: true })).toBeVisible();

  await panel.getByRole("button", { name: "Run monitoring cycle" }).click();
  const lookupError = panel.getByRole("alert").filter({ hasText: "Operation lookup failed." });
  await expect(lookupError).toBeVisible();
  await page.waitForTimeout(2000);
  expect(calls.filter((call) => call.key === "GET /monitoring/operations/op-2")).toHaveLength(1);
  await lookupError.getByRole("button", { name: "Retry" }).click();
  const cycle = panel.getByRole("status", { name: "Cycle status" });
  await expect(cycle).toContainText("“failed”");
  await expect(cycle).toContainText("source_timeout");
  await expect(cycle).not.toContainText("successfully");
});

test("Reviewer sees compact Home health notices and makes no Admin requests", async ({ page }) => {
  const calls = await mockApi(page, "reviewer", {
    "GET /monitoring/status": () => ({
      body: statusFixture({
        health: [{ kind: "source", message: "Synthetic connector is unreachable.", healthy: false }],
        operations: [{ ...operation("op-3", "failed", { finished_at: "2026-10-13T12:01:00Z", error_code: "timeout" }), operation: "source_poll" }],
      }),
    }),
  });
  await page.goto("/");
  const summary = page.getByRole("region", { name: "Monitoring health" });
  await expect(summary).toContainText("Synthetic connector is unreachable.");
  await expect(summary).toContainText("Source polling last ended with status “failed”");
  await expect(summary.getByRole("table")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Run monitoring cycle" })).toHaveCount(0);
  const adminCalls = calls.filter((call) => /^(GET|PATCH) \/settings|^POST \/monitoring\/cycle|^GET \/users/.test(call.key));
  expect(adminCalls).toEqual([]);
});
