import { expect, test, type Page } from "@playwright/test";

type Json = Record<string, unknown>;
type Reply = { status: number; json: unknown } | "abort";
type Recorded = { method: string; path: string; body: unknown; raw: string | null; csrf: string | null };
type BcFlags = {
  client_id_configured: boolean;
  client_secret_configured: boolean;
  configured: boolean;
  webhook_base_url: string | null;
};
type Fixture = {
  role?: "admin" | "reviewer";
  bc?: Partial<BcFlags>;
  respond?: (method: string, path: string, body: unknown) => Reply | undefined;
};

// Synthetic values only; every request is intercepted and nothing reaches Microsoft.
const CSRF = "csrf-synthetic-token";
const ORIGIN = "https://screening.example.test";
const BC_PATH = "/settings/connectors/bc";
const WEBHOOK_BASE = `${ORIGIN}/api/v1/webhooks/bc`;
const CLIENT_ID = "00000000-0000-4000-8000-00000000bc01";
const WORKSPACE = {
  display_name: "Synthetic workspace",
  timezone: "UTC",
  retention_years: 5,
  auto_clear_enabled: false,
  auto_clear_threshold: 80,
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
const NOTIFICATIONS = {
  smtp_host: "", smtp_from: "", public_base_url: ORIGIN, smtp_port: 587, smtp_tls: "starttls",
  report_recipients: [], report_time: "07:00", likely_alerts_enabled: false,
  smtp_username_configured: false, smtp_password_configured: false,
};
const EMPTY_STATUS = { batches: [], health: [] };

async function mockApi(page: Page, fixture: Fixture = {}) {
  const requests: Recorded[] = [];
  const role = fixture.role ?? "admin";
  const user = { id: "user-1", email: `${role}@example.test`, display_name: `Synthetic ${role}`, role, active: true };
  const bc: BcFlags = {
    client_id_configured: true,
    client_secret_configured: true,
    configured: true,
    webhook_base_url: WEBHOOK_BASE,
    ...fixture.bc,
  };
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
    requests.push({ method, path, body, raw, csrf: request.headers()["x-csrf-token"] ?? null });
    const reply = (status: number, json: unknown) =>
      route.fulfill({ status, contentType: "application/json", body: JSON.stringify(json) });
    const custom = fixture.respond?.(method, path, body);
    if (custom === "abort") return route.abort("failed");
    if (custom) return reply(custom.status, custom.json);
    if (method === "GET") {
      if (path === "/setup")
        return reply(200, { needs_setup: false, product_name: "Screening Tool", disclaimer: WORKSPACE.disclaimer, disclaimer_url: WORKSPACE.disclaimer_url });
      if (path === "/auth/me") return reply(200, { user, csrf_token: CSRF });
      if (path === "/settings") return reply(200, WORKSPACE);
      if (path === "/users") return reply(200, { items: [user], invitations: [] });
      if (path === "/settings/notifications") return reply(200, NOTIFICATIONS);
      if (path === "/notifications/status") return reply(200, EMPTY_STATUS);
      if (path === "/reports/morning/status") return reply(200, { ...EMPTY_STATUS, last_report_at: null });
      if (path === BC_PATH) return reply(200, bc);
    }
    if (method === "PATCH" && path === BC_PATH) {
      const changes = body as Json;
      if (changes.clear_credentials) Object.assign(bc, { client_id_configured: false, client_secret_configured: false });
      if (changes.client_id) bc.client_id_configured = true;
      if (changes.client_secret) bc.client_secret_configured = true;
      bc.configured = bc.client_id_configured && bc.client_secret_configured;
      return reply(200, bc);
    }
    // Other panels (QuickBooks, sources, monitoring) get a contained failure.
    return reply(404, { detail: "Not available in this test fixture." });
  });
  return { requests };
}

const patches = (requests: Recorded[]) => requests.filter((r) => r.method === "PATCH" && r.path === BC_PATH);
const bcRequests = (requests: Recorded[]) =>
  requests.filter((r) => r.path.startsWith(BC_PATH) || r.path.includes("/bc/"));
const panel = (page: Page) => page.getByRole("region", { name: "Business Central app settings" });
const fact = (page: Page, label: string) =>
  panel(page).getByRole("region", { name: "Entra app" }).getByRole("listitem").filter({ has: page.getByText(label, { exact: true }) });
const idInput = (page: Page) => panel(page).getByLabel("New application (client) ID", { exact: true });
const secretInput = (page: Page) => panel(page).getByLabel("New client secret", { exact: true });
const saveButton = (page: Page) => panel(page).getByRole("button", { name: "Save Business Central app settings" });

test("saved flags are facts only and a configured app is not a connected company", async ({ page }) => {
  const { requests } = await mockApi(page);
  await page.goto("/settings");
  const region = panel(page);
  await expect(fact(page, "Client ID")).toHaveText("Client ID Saved");
  await expect(fact(page, "Client secret")).toHaveText("Client secret Saved");
  await expect(fact(page, "App configuration")).toContainText("Configured.");
  await expect(region.getByText("A configured app is not a connected company.", { exact: false })).toBeVisible();
  await expect(region.getByRole("link", { name: "Open Sources" })).toHaveAttribute("href", "/sources");
  await expect(region.locator("code", { hasText: WEBHOOK_BASE })).toHaveText(WEBHOOK_BASE);
  await expect(region.getByText("API.ReadWrite.All", { exact: true })).toBeVisible();
  await expect(region).toContainText("Don’t assign SUPER.");
  await expect(region).toContainText("There is no user sign-in or redirect.");
  await expect(region).toContainText("Live validation against a Business Central tenant is still pending");

  for (const input of [idInput(page), secretInput(page)]) {
    await expect(input).toHaveAttribute("type", "password");
    await expect(input).toHaveAttribute("autocomplete", "new-password");
    await expect(input).toHaveValue("");
    expect(await input.getAttribute("placeholder")).toBeNull();
  }
  expect(patches(requests)).toHaveLength(0);
  expect(requests.some((r) => r.path.includes("/bc/"))).toBe(false);
});

test("without a public base URL, webhooks are explained as optional and nothing is invented", async ({ page }) => {
  await mockApi(page, {
    bc: { client_id_configured: false, client_secret_configured: false, configured: false, webhook_base_url: null },
  });
  await page.goto("/settings");
  const region = panel(page);
  await expect(fact(page, "Client ID")).toHaveText("Client ID Not saved");
  await expect(fact(page, "App configuration")).toContainText("Not configured.");
  await expect(region).toContainText("set the Public base URL in the Email notifications settings");
  await expect(region).toContainText("Company discovery, connecting and polling don’t need a public URL.");
  await expect(region.locator("code", { hasText: "webhooks" })).toHaveCount(0);
  await expect(region.getByLabel("Clear saved client ID and client secret")).toHaveCount(0);
});

test("secret spaces are sent unchanged, the client ID is canonicalized and nothing is kept", async ({ page }) => {
  const secret = "  synthetic bc secret 0001  ";
  const rotated = " synthetic-bc-secret-0002";
  const logs: string[] = [];
  page.on("console", (message) => logs.push(message.text()));
  const { requests } = await mockApi(page);
  await page.goto("/settings");
  const region = panel(page);

  await idInput(page).fill(`  ${CLIENT_ID.toUpperCase()} `);
  await secretInput(page).fill(secret);
  await expect(region.getByText("The secret starts or ends with spaces.", { exact: false })).toBeVisible();
  await saveButton(page).click();

  await expect.poll(() => patches(requests).length).toBe(1);
  const first = patches(requests)[0];
  expect(first.body).toEqual({ client_id: CLIENT_ID, client_secret: secret });
  expect(JSON.parse(first.raw ?? "{}").client_secret).toBe(secret);
  expect(first.csrf).toBe(CSRF);
  await expect(region.getByRole("status").filter({ hasText: "saved" })).toHaveText(
    "Business Central app settings saved. Saved values are never shown.",
  );
  await expect(idInput(page)).toHaveValue("");
  await expect(secretInput(page)).toHaveValue("");

  // Same-ID secret rotation sends only the secret, leading space intact.
  await secretInput(page).fill(rotated);
  await saveButton(page).click();
  await expect.poll(() => patches(requests).length).toBe(2);
  expect(patches(requests)[1].body).toEqual({ client_secret: rotated });

  await saveButton(page).click();
  await expect(region.getByRole("status").filter({ hasText: "No changes" })).toHaveText("No changes to save.");
  expect(patches(requests)).toHaveLength(2);

  const html = await page.content();
  for (const value of [secret.trim(), rotated.trim(), CLIENT_ID]) expect(html).not.toContain(value);
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }));
  expect(stored).not.toContain("synthetic");
  expect(logs.join("\n")).not.toContain("synthetic bc secret");
});

test("an invalid client ID, control characters or an unpaired ID are blocked before sending", async ({ page }) => {
  const { requests } = await mockApi(page, { bc: { client_id_configured: false, client_secret_configured: false, configured: false } });
  await page.goto("/settings");
  const region = panel(page);
  const alert = region.getByRole("alert");

  await idInput(page).fill("synthetic-not-a-uuid");
  await secretInput(page).fill("synthetic-bc-secret-0003");
  await saveButton(page).click();
  await expect(alert).toContainText("Nothing was sent");
  await expect(idInput(page)).toHaveAttribute("aria-invalid", "true");
  await expect(region).toContainText("as a UUID");
  await expect(idInput(page)).toHaveValue("");
  await expect(secretInput(page)).toHaveValue("");

  await idInput(page).fill(CLIENT_ID);
  await secretInput(page).fill("synthetic\tbc-secret-0004");
  await saveButton(page).click();
  await expect(secretInput(page)).toHaveAttribute("aria-invalid", "true");
  await expect(region).toContainText("The secret contains a control character");
  await expect(secretInput(page)).toHaveValue("");

  await idInput(page).fill(CLIENT_ID);
  await saveButton(page).click();
  await expect(region).toContainText("No client secret is saved yet. Enter it together with the client ID.");

  expect(patches(requests)).toHaveLength(0);
  const html = await page.content();
  expect(html).not.toContain("synthetic-bc-secret-0003");
  expect(html).not.toContain("bc-secret-0004");
});

test("an explicit clear excludes replacements and is sent only when chosen", async ({ page }) => {
  const { requests } = await mockApi(page);
  await page.goto("/settings");
  const clear = panel(page).getByLabel("Clear saved client ID and client secret");

  await secretInput(page).fill("synthetic-bc-secret-0005");
  await expect(clear).toBeDisabled();
  await secretInput(page).fill("");
  await expect(clear).toBeEnabled();
  await clear.check();
  await expect(idInput(page)).toBeDisabled();
  await expect(secretInput(page)).toBeDisabled();
  await saveButton(page).click();

  await expect.poll(() => patches(requests).length).toBe(1);
  expect(patches(requests)[0].body).toEqual({ clear_credentials: true });
  await expect(fact(page, "Client ID")).toHaveText("Client ID Not saved");
  await expect(fact(page, "Client secret")).toHaveText("Client secret Not saved");
  await expect(fact(page, "App configuration")).toContainText("Not configured.");
  await expect(clear).toHaveCount(0);
});

test("409, 422 and network failures clear entered values, never echo them and are not retried", async ({ page }) => {
  const secret = "  synthetic bc secret 0006 ";
  let attempt = 0;
  const { requests } = await mockApi(page, {
    respond: (method, path) => {
      if (method !== "PATCH" || path !== BC_PATH) return undefined;
      attempt += 1;
      if (attempt === 1) return { status: 409, json: { detail: "Synthetic source lease is held." } };
      if (attempt === 2) return { status: 422, json: { detail: `Rejected client secret ${secret.trim()}` } };
      return "abort";
    },
  });
  await page.goto("/settings");
  const alert = panel(page).getByRole("alert");

  await idInput(page).fill(CLIENT_ID);
  await secretInput(page).fill(secret);
  await saveButton(page).click();
  await expect(alert).toContainText("Synthetic source lease is held.");
  await expect(alert).toContainText("disconnect those sources in Sources");
  await expect(alert).toContainText("Entered values were cleared");
  await expect(idInput(page)).toHaveValue("");
  await expect(secretInput(page)).toHaveValue("");

  await secretInput(page).fill(secret);
  await saveButton(page).click();
  await expect(alert).toContainText("The server rejected the submitted values.");
  await expect(alert).not.toContainText("secret 0006");
  await expect(secretInput(page)).toHaveValue("");

  await secretInput(page).fill(secret);
  await saveButton(page).click();
  await expect(alert).toContainText("it is unknown whether the change was saved");
  await expect(alert).toContainText("check the saved flags below before retrying");
  await expect(secretInput(page)).toHaveValue("");

  await expect(fact(page, "Client secret")).toHaveText("Client secret Saved");
  expect(patches(requests).map((r) => r.body)).toEqual([
    { client_id: CLIENT_ID, client_secret: secret },
    { client_secret: secret },
    { client_secret: secret },
  ]);
  const html = await page.content();
  expect(html).not.toContain(secret.trim());
  expect(html).not.toContain(CLIENT_ID);
});

test("a failed load is reported honestly and can be retried", async ({ page }) => {
  let failing = true;
  await mockApi(page, {
    respond: (method, path) =>
      failing && method === "GET" && path === BC_PATH
        ? { status: 503, json: { detail: "Synthetic outage." } }
        : undefined,
  });
  await page.goto("/settings");
  const region = panel(page);
  await expect(region.getByRole("alert")).toContainText("current state is unknown", { timeout: 15_000 });
  await expect(region.getByRole("alert")).toContainText("Synthetic outage.");
  await expect(idInput(page)).toHaveCount(0);

  failing = false;
  await region.getByRole("button", { name: "Try again" }).click();
  await expect(fact(page, "Client ID")).toHaveText("Client ID Saved");
  await expect(region.getByRole("alert")).toHaveCount(0);
});

test("Reviewer gets no Business Central panel and makes no Business Central requests", async ({ page }) => {
  const { requests } = await mockApi(page, { role: "reviewer" });
  await page.goto("/settings");
  await expect.poll(() => requests.some((r) => r.path === "/auth/me")).toBe(true);
  await page.waitForLoadState("networkidle");
  await expect(panel(page)).toHaveCount(0);
  expect(bcRequests(requests)).toHaveLength(0);
  expect(requests.some((r) => r.method !== "GET")).toBe(false);
});

// Depends on the root integration invalidating ["adminConnectors", "bc"] after a public base URL save.
test("changing the public base URL refreshes the Business Central webhook base URL", async ({ page }) => {
  let origin = ORIGIN;
  const nextOrigin = "https://updated-screening.example.test";
  const { requests } = await mockApi(page, {
    respond: (method, path, body) => {
      if (method === "GET" && path === BC_PATH)
        return { status: 200, json: { client_id_configured: true, client_secret_configured: true, configured: true, webhook_base_url: `${origin}/api/v1/webhooks/bc` } };
      if (method === "PATCH" && path === "/settings/notifications") {
        origin = (body as Json).public_base_url as string;
        return { status: 200, json: { ...NOTIFICATIONS, public_base_url: origin } };
      }
      return undefined;
    },
  });
  await page.goto("/settings");
  await expect(panel(page).getByText(WEBHOOK_BASE, { exact: true })).toBeVisible();
  const email = page.getByRole("region", { name: "Email notifications", exact: true });
  await email.getByLabel("Public base URL", { exact: true }).fill(nextOrigin);
  await email.getByRole("button", { name: "Save notification settings" }).click();
  await expect(panel(page).getByText(`${nextOrigin}/api/v1/webhooks/bc`, { exact: true })).toBeVisible();
  expect(requests.find((r) => r.method === "PATCH" && r.path === "/settings/notifications")).toMatchObject({ body: { public_base_url: nextOrigin }, csrf: CSRF });
});


test("a client ID-only patch preserves the already configured secret", async ({ page }) => {
  const { requests } = await mockApi(page);
  await page.goto("/settings");
  await idInput(page).fill(CLIENT_ID);
  await saveButton(page).click();
  await expect(panel(page).getByRole("status").filter({ hasText: "Business Central app settings saved." })).toBeVisible();
  expect(patches(requests).map(request => request.body)).toEqual([{ client_id: CLIENT_ID }]);
  expect(patches(requests)[0].csrf).toBe(CSRF);
  await expect(fact(page, "Client secret")).toHaveText("Client secret Saved");
  await expect(idInput(page)).toHaveValue("");
});


test("Business Central settings fit a narrow viewport with blank credential inputs", async ({ page }, testInfo) => {
  await mockApi(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/settings");
  await expect(secretInput(page)).toHaveValue("");
  await expect(idInput(page)).toHaveValue("");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await panel(page).evaluate((element) => element.scrollIntoView({ block: "start" }));
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
  await page.screenshot({ path: testInfo.outputPath("milestone-7-mocked-bc-settings-mobile.png"), fullPage: false });
  await page.setViewportSize({ width: 1440, height: 950 });
  await panel(page).evaluate((element) => element.scrollIntoView({ block: "start" }));
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
  await page.screenshot({ path: testInfo.outputPath("milestone-7-mocked-bc-settings.png"), fullPage: false });
});
