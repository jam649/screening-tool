import { expect, test, type Page } from "@playwright/test";

type Json = Record<string, unknown>;
type Reply = { status: number; json: unknown };
type Recorded = { method: string; path: string; body: unknown; csrf: string | null };
type Fixture = {
  role?: "admin" | "reviewer";
  config?: Json;
  notificationStatus?: () => Json;
  reportStatus?: () => Json;
  respond?: (method: string, path: string, body: unknown) => Reply | undefined;
};

// Synthetic values only; nothing here reaches a real mail server.
const CSRF = "csrf-synthetic-token";
const EMPTY_STATUS = { batches: [], health: [] };
const DEFAULT_CONFIG = {
  smtp_host: "",
  smtp_from: "",
  public_base_url: "",
  smtp_port: 587,
  smtp_tls: "starttls",
  report_recipients: [],
  report_time: "07:00",
  likely_alerts_enabled: false,
  smtp_username_configured: false,
  smtp_password_configured: false,
};
const WORKSPACE = {
  display_name: "Synthetic workspace",
  timezone: "UTC",
  retention_years: 5,
  auto_clear_enabled: false,
  auto_clear_threshold: 50,
  auto_clear_acknowledgment_audit_id: null,
  pending_screening_task_id: null,
  candidate_min: 60,
  review_threshold: 75,
  likely_threshold: 90,
  score_weights: {
    token_set: 0.4, token_sort: 0.3, partial: 0.3, country_match: 5, country_mismatch: -5,
    city_match: 3, address_max: 5, entity_mismatch: -5, short_name: -5,
  },
  legal_suffixes: ["llc"],
  candidate_stopwords: ["the"],
  enabled_source_lists: null,
  disclaimer: "Synthetic disclaimer.",
  disclaimer_url: "https://example.test/disclaimer",
};
const batch = (id: string, status: string, recipient: string, attempts = 0, error_code: string | null = null) => ({
  batch_id: id,
  status,
  deliveries: [{ recipient, status, attempts, error_code }],
});

async function mockApi(page: Page, fixture: Fixture = {}) {
  const requests: Recorded[] = [];
  const role = fixture.role ?? "admin";
  const user = { id: "user-1", email: `${role}@example.test`, display_name: `Synthetic ${role}`, role, active: true };
  const config: Json = { ...DEFAULT_CONFIG, ...fixture.config };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/^\/api\/v1/, "");
    const method = request.method();
    const raw = request.postData();
    let body: unknown = null;
    try {
      body = raw ? JSON.parse(raw) : null;
    } catch {
      body = raw;
    }
    requests.push({ method, path, body, csrf: request.headers()["x-csrf-token"] ?? null });
    const reply = (status: number, json: unknown) =>
      route.fulfill({ status, contentType: "application/json", body: JSON.stringify(json) });
    const custom = fixture.respond?.(method, path, body);
    if (custom) return reply(custom.status, custom.json);
    if (method === "GET") {
      if (path === "/setup")
        return reply(200, { needs_setup: false, product_name: "Screening Tool", disclaimer: WORKSPACE.disclaimer, disclaimer_url: WORKSPACE.disclaimer_url });
      if (path === "/auth/me") return reply(200, { user, csrf_token: CSRF });
      if (path === "/settings") return reply(200, WORKSPACE);
      if (path === "/users") return reply(200, { items: [user], invitations: [] });
      if (path === "/settings/notifications") return reply(200, config);
      if (path === "/notifications/status") return reply(200, fixture.notificationStatus?.() ?? EMPTY_STATUS);
      if (path === "/reports/morning/status")
        return reply(200, fixture.reportStatus?.() ?? { ...EMPTY_STATUS, last_report_at: null });
    }
    if (method === "PATCH" && path === "/settings/notifications") {
      const { smtp_username, smtp_password, clear_credentials, ...publicFields } = body as Json;
      Object.assign(config, publicFields);
      if (clear_credentials) Object.assign(config, { smtp_username_configured: false, smtp_password_configured: false });
      if (smtp_username) config.smtp_username_configured = true;
      if (smtp_password) config.smtp_password_configured = true;
      return reply(200, config);
    }
    // Other panels (auto-clear, monitoring, sources) get a contained failure.
    return reply(404, { detail: "Not available in this test fixture." });
  });
  return { requests };
}

const patches = (requests: Recorded[]) =>
  requests.filter((r) => r.method === "PATCH" && r.path === "/settings/notifications");

test("saves changed public fields, explicit credential replacement and clear with CSRF", async ({ page }) => {
  const { requests } = await mockApi(page, {
    config: { smtp_host: "smtp.example.test", smtp_from: "screening@example.test", smtp_username_configured: true, smtp_password_configured: true },
  });
  await page.goto("/settings");
  const smtp = page.getByRole("region", { name: "SMTP server" });
  const username = smtp.getByLabel("New SMTP username", { exact: true });
  const password = smtp.getByLabel("New SMTP password", { exact: true });
  const save = smtp.getByRole("button", { name: "Save notification settings" });

  await smtp.getByLabel("SMTP port", { exact: true }).fill("2525");
  await save.click();
  await expect.poll(() => patches(requests).length).toBe(1);
  expect(patches(requests)[0].body).toEqual({ smtp_port: 2525 });

  await username.fill("synthetic-user");
  await password.fill("synthetic-pass-123");
  await save.click();
  await expect.poll(() => patches(requests).length).toBe(2);
  expect(patches(requests)[1].body).toEqual({ smtp_username: "synthetic-user", smtp_password: "synthetic-pass-123" });
  await expect(username).toHaveValue("");
  await expect(password).toHaveValue("");

  await smtp.getByLabel("Clear saved SMTP username and password").check();
  await expect(username).toBeDisabled();
  await save.click();
  await expect.poll(() => patches(requests).length).toBe(3);
  expect(patches(requests)[2].body).toEqual({ clear_credentials: true });
  await expect(smtp.getByRole("status")).toHaveText("Notification settings saved.");
  for (const request of patches(requests)) expect(request.csrf).toBe(CSRF);
});

test("server 422 keeps the nonsecret draft and requires the password again", async ({ page }) => {
  const { requests } = await mockApi(page, {
    respond: (method, path) =>
      method === "PATCH" && path === "/settings/notifications"
        ? { status: 422, json: { detail: "SMTP host was rejected by the server." } }
        : undefined,
  });
  await page.goto("/settings");
  const smtp = page.getByRole("region", { name: "SMTP server" });
  await smtp.getByLabel("SMTP host", { exact: true }).fill("smtp.example.test");
  await smtp.getByLabel("New SMTP password", { exact: true }).fill("synthetic-pass-123");
  await smtp.getByRole("button", { name: "Save notification settings" }).click();

  const alert = smtp.getByRole("alert");
  await expect(alert).toContainText("SMTP host was rejected by the server.");
  await expect(alert).toContainText("Re-enter the new SMTP username or password");
  await expect(smtp.getByLabel("SMTP host", { exact: true })).toHaveValue("smtp.example.test");
  await expect(smtp.getByLabel("New SMTP password", { exact: true })).toHaveValue("");
  expect(patches(requests).map((r) => r.body)).toEqual([{ smtp_host: "smtp.example.test", smtp_password: "synthetic-pass-123" }]);
});

test("test email posts the explicit recipient and follows pending to sent", async ({ page }) => {
  let sent = false;
  let reads = 0;
  const { requests } = await mockApi(page, {
    config: { smtp_host: "smtp.example.test", smtp_from: "screening@example.test" },
    notificationStatus: () => {
      if (!sent) return EMPTY_STATUS;
      reads += 1;
      const status = reads < 2 ? "pending" : "sent";
      return { batches: [batch("batch-test-1", status, "ops@example.test", status === "sent" ? 1 : 0)], health: [] };
    },
    respond: (method, path) => {
      if (method !== "POST" || path !== "/notifications/test") return undefined;
      sent = true;
      return { status: 202, json: batch("batch-test-1", "pending", "ops@example.test") };
    },
  });
  await page.goto("/settings");
  const region = page.getByRole("region", { name: "Test email" });
  await region.getByLabel("Test recipient", { exact: true }).fill("ops@example.test");
  await region.getByRole("button", { name: "Send test email" }).click();

  const delivery = region.getByRole("listitem").filter({ hasText: "ops@example.test" });
  await expect(delivery).toContainText("Queued, not yet delivered");
  await expect(delivery).toContainText("Accepted by the SMTP server", { timeout: 10_000 });
  const posts = requests.filter((r) => r.method === "POST" && r.path === "/notifications/test");
  expect(posts).toHaveLength(1);
  expect(posts[0].body).toEqual({ recipient: "ops@example.test" });
  expect(posts[0].csrf).toBe(CSRF);
});

test("partially failed report keeps the last complete boundary and per-recipient errors", async ({ page }) => {
  const { requests } = await mockApi(page, {
    reportStatus: () => ({
      last_report_at: "2026-10-12T11:00:00Z",
      batches: [{
        batch_id: "report-2",
        status: "failed",
        deliveries: [
          { recipient: "a@example.test", status: "sent", attempts: 1, error_code: null },
          { recipient: "b@example.test", status: "failed", attempts: 3, error_code: "smtp_recipient_rejected" },
        ],
      }],
      health: [],
    }),
    respond: (method, path) =>
      method === "POST" && path === "/reports/morning/send"
        ? { status: 202, json: batch("report-3", "pending", "a@example.test") }
        : undefined,
  });
  await page.goto("/settings");
  const region = page.getByRole("region", { name: "Morning report" });
  await expect(region.getByText("Last report accepted by SMTP for every recipient")).toBeVisible();
  const failed = region.getByRole("listitem").filter({ hasText: "b@example.test" });
  await expect(failed).toContainText("Failed · Attempts: 3 · Error: smtp_recipient_rejected");
  await expect(region.getByRole("listitem").filter({ hasText: "a@example.test" })).toContainText("Accepted by the SMTP server");
  await expect(region.getByRole("button", { name: "Retry failed deliveries" })).toBeEnabled();

  await region.getByRole("button", { name: "Send morning report now" }).click();
  await expect(region.getByText("The server returned this delivery status; a refreshed status will appear when available.")).toBeVisible();
  await expect(region.getByText("Last report accepted by SMTP for every recipient")).toBeVisible();
  const posts = requests.filter((r) => r.method === "POST" && r.path === "/reports/morning/send");
  expect(posts).toHaveLength(1);
  expect(posts[0].body).toBeNull();
  expect(posts[0].csrf).toBe(CSRF);
});

test("uncertain delivery is retried only by the explicit labelled button", async ({ page }) => {
  const { requests } = await mockApi(page, {
    notificationStatus: () => ({
      batches: [batch("alert-7", "uncertain", "c@example.test", 1, "smtp_timeout_after_data")],
      health: [{ kind: "delivery_uncertain", message: "One delivery may or may not have been accepted." }],
    }),
    respond: (method, path) =>
      method === "POST" && path === "/notifications/batches/alert-7/retry"
        ? { status: 202, json: batch("alert-7", "pending", "c@example.test", 1) }
        : undefined,
  });
  await page.goto("/settings");
  const region = page.getByRole("region", { name: "Delivery status" });
  await expect(region.getByText("another attempt may deliver a duplicate")).toBeVisible();
  await expect(region.getByRole("button", { name: "Retry failed deliveries" })).toHaveCount(0);
  const retries = () => requests.filter((r) => r.path.endsWith("/retry"));
  expect(retries()).toHaveLength(0);

  await region.getByRole("button", { name: "Retry uncertain delivery" }).click();
  await expect.poll(() => retries().length).toBe(1);
  expect(retries()[0]).toMatchObject({ method: "POST", body: { retry_uncertain: true }, csrf: CSRF });
});

test("Reviewer sees a read-only delivery banner without Admin reads or writes", async ({ page }) => {
  const { requests } = await mockApi(page, {
    role: "reviewer",
    notificationStatus: () => ({
      batches: [],
      health: [
        { kind: "smtp_not_configured", message: "SMTP is not configured." },
        { kind: "delivery_failed", message: "Morning report delivery failed for one recipient." },
      ],
    }),
  });
  await page.goto("/");
  const banner = page.getByRole("region", { name: "Email delivery status" });
  await expect(banner).toContainText("Morning report delivery failed for one recipient.");
  await expect(banner).toContainText("An Admin can review email delivery in Settings.");
  await expect(banner).not.toContainText("SMTP is not configured.");
  await expect(banner.getByRole("link")).toHaveCount(0);
  expect(requests.some((r) => r.path === "/settings/notifications")).toBe(false);
  expect(requests.some((r) => r.method !== "GET")).toBe(false);
});
