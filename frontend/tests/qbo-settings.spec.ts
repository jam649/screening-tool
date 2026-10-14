import { expect, test, type Page } from "@playwright/test";

type Json = Record<string, unknown>;
type Reply = { status: number; json: unknown };
type Recorded = { method: string; path: string; body: unknown; raw: string | null; csrf: string | null };
type Flags = {
  client_id_configured: boolean;
  client_secret_configured: boolean;
  verifier_token_configured: boolean;
  configured: boolean;
};
type Fixture = {
  role?: "admin" | "reviewer";
  qbo?: Partial<{ sandbox: Flags; production: Flags; redirect_uri: string | null; webhook_urls: Json }>;
  respond?: (method: string, path: string, body: unknown) => Reply | undefined;
};

// Synthetic values only; every request is intercepted and nothing reaches Intuit.
const CSRF = "csrf-synthetic-token";
const ORIGIN = "https://screening.example.test";
const REDIRECT = `${ORIGIN}/api/v1/connectors/qbo/oauth/callback`;
const SANDBOX_HOOK = `${ORIGIN}/api/v1/webhooks/qbo/sandbox`;
const QBO_PATH = "/settings/connectors/qbo";
const flags = (id: boolean, secret: boolean, verifier: boolean): Flags => ({
  client_id_configured: id,
  client_secret_configured: secret,
  verifier_token_configured: verifier,
  configured: id && secret,
});
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
  const qbo = structuredClone({
    sandbox: flags(true, true, true),
    production: flags(true, false, false),
    redirect_uri: REDIRECT,
    webhook_urls: { sandbox: SANDBOX_HOOK, production: `${ORIGIN}/api/v1/webhooks/qbo/production` },
    ...fixture.qbo,
  });
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
      if (path === QBO_PATH) return reply(200, qbo);
    }
    if (method === "PATCH" && path === QBO_PATH) {
      const changes = body as Json;
      const env = qbo[changes.environment as "sandbox" | "production"];
      if (changes.clear_credentials) Object.assign(env, { client_id_configured: false, client_secret_configured: false });
      if (changes.clear_verifier_token) env.verifier_token_configured = false;
      if (changes.client_id) env.client_id_configured = true;
      if (changes.client_secret) env.client_secret_configured = true;
      if (changes.verifier_token) env.verifier_token_configured = true;
      env.configured = env.client_id_configured && env.client_secret_configured;
      return reply(200, qbo);
    }
    // Other panels (sources, monitoring, auto-clear) get a contained failure.
    return reply(404, { detail: "Not available in this test fixture." });
  });
  return { requests };
}

const patches = (requests: Recorded[]) => requests.filter((r) => r.method === "PATCH" && r.path === QBO_PATH);
const panel = (page: Page) => page.getByRole("region", { name: "QuickBooks app settings" });
const fact = (page: Page, env: "Sandbox" | "Production", label: string) =>
  panel(page).getByRole("region", { name: `${env} app` }).getByRole("listitem").filter({ has: page.getByText(label, { exact: true }) });

test("each environment reports its own flags and an app configuration is not a connected company", async ({ page }) => {
  const { requests } = await mockApi(page, {
    qbo: { webhook_urls: { sandbox: SANDBOX_HOOK, production: null } },
  });
  await page.goto("/settings");
  const region = panel(page);
  await expect(region.getByRole("radio", { name: "Sandbox" })).toBeChecked();
  await expect(fact(page, "Sandbox", "Client secret")).toHaveText("Client secret Saved");
  await expect(fact(page, "Sandbox", "App configuration")).toContainText("Configured.");
  await expect(region.getByText("A configured app is not a connected company.")).toBeVisible();
  await expect(region.getByText(REDIRECT, { exact: true })).toBeVisible();
  await expect(region.getByText(SANDBOX_HOOK, { exact: true })).toBeVisible();

  await region.getByRole("radio", { name: "Production" }).check();
  await expect(region.getByRole("region", { name: "Sandbox app" })).toHaveCount(0);
  await expect(fact(page, "Production", "Client ID")).toHaveText("Client ID Saved");
  await expect(fact(page, "Production", "Client secret")).toHaveText("Client secret Not saved");
  await expect(fact(page, "Production", "App configuration")).toContainText("Not configured.");
  await expect(region.getByText("app assessment")).toBeVisible();
  const production = region.getByRole("region", { name: "Production app" });
  await expect(production).toContainText("Set the Public base URL in the Email notifications settings");
  await expect(production.getByText(SANDBOX_HOOK)).toHaveCount(0);

  expect(patches(requests)).toHaveLength(0);
  expect(requests.some((r) => r.path.includes("/qbo/connection"))).toBe(false);
});

test("omitted credentials are preserved and only the entered replacement is sent", async ({ page }) => {
  const secret = "synthetic-qbo-secret-0001";
  const { requests } = await mockApi(page);
  await page.goto("/settings");
  const region = panel(page);
  await region.getByRole("radio", { name: "Production" }).check();
  for (const name of ["New client ID", "New client secret", "New webhook verifier token"]) {
    const input = region.getByLabel(name, { exact: true });
    await expect(input).toHaveAttribute("type", "password");
    await expect(input).toHaveAttribute("autocomplete", "new-password");
    await expect(input).toHaveValue("");
  }
  const secretInput = region.getByLabel("New client secret", { exact: true });
  await secretInput.fill(secret);
  await region.getByRole("button", { name: "Save production app settings" }).click();

  await expect.poll(() => patches(requests).length).toBe(1);
  expect(patches(requests)[0].body).toEqual({ environment: "production", client_secret: secret });
  expect(patches(requests)[0].csrf).toBe(CSRF);
  await expect(region.getByRole("status")).toHaveText("Production app settings saved. Saved values are never shown.");
  await expect(secretInput).toHaveValue("");
  await expect(fact(page, "Production", "Client secret")).toHaveText("Client secret Saved");
  await expect(fact(page, "Production", "App configuration")).toContainText("Configured.");
  expect(await page.content()).not.toContain(secret);

  await region.getByRole("button", { name: "Save production app settings" }).click();
  await expect(region.getByRole("status")).toHaveText("No changes to save.");
  expect(patches(requests)).toHaveLength(1);
});

test("explicit clears exclude replacements and are sent only when chosen", async ({ page }) => {
  const { requests } = await mockApi(page);
  await page.goto("/settings");
  const region = panel(page);
  const clientId = region.getByLabel("New client ID", { exact: true });
  const clientSecret = region.getByLabel("New client secret", { exact: true });
  const verifier = region.getByLabel("New webhook verifier token", { exact: true });
  const clearCredentials = region.getByLabel("Clear saved client ID and client secret");
  const clearVerifier = region.getByLabel("Clear saved webhook verifier token");

  await clientId.fill("synthetic-qbo-client-0001");
  await expect(clearCredentials).toBeDisabled();
  await clientId.fill("");
  await verifier.fill("synthetic-qbo-verifier-0001");
  await expect(clearVerifier).toBeDisabled();
  await verifier.fill("");

  await clearCredentials.check();
  await expect(clientId).toBeDisabled();
  await expect(clientSecret).toBeDisabled();
  await expect(verifier).toBeEnabled();
  await clearVerifier.check();
  await expect(verifier).toBeDisabled();
  await region.getByRole("button", { name: "Save sandbox app settings" }).click();

  await expect.poll(() => patches(requests).length).toBe(1);
  expect(patches(requests)[0].body).toEqual({ environment: "sandbox", clear_credentials: true, clear_verifier_token: true });
  await expect(fact(page, "Sandbox", "Client ID")).toHaveText("Client ID Not saved");
  await expect(fact(page, "Sandbox", "App configuration")).toContainText("Not configured.");
  await expect(clearCredentials).toHaveCount(0);
  await expect(clearVerifier).toHaveCount(0);
});

test("409 and 422 failures clear entered values and never echo them", async ({ page }) => {
  const clientId = "synthetic-qbo-client-0002";
  const secret = "synthetic-qbo-secret-0003";
  let attempt = 0;
  const { requests } = await mockApi(page, {
    respond: (method, path) => {
      if (method !== "PATCH" || path !== QBO_PATH) return undefined;
      attempt += 1;
      return attempt === 1
        ? { status: 409, json: { detail: "Client identity cannot change while connected sources retain credentials." } }
        : { status: 422, json: { detail: `Rejected client secret ${secret}` } };
    },
  });
  await page.goto("/settings");
  const region = panel(page);
  const save = region.getByRole("button", { name: "Save sandbox app settings" });
  const idInput = region.getByLabel("New client ID", { exact: true });
  const secretInput = region.getByLabel("New client secret", { exact: true });

  await idInput.fill(clientId);
  await save.click();
  const alert = region.getByRole("alert");
  await expect(alert).toContainText("Client identity cannot change while connected sources retain credentials.");
  await expect(alert).toContainText("disconnect those sources in Sources first");
  await expect(alert).toContainText("Entered values were cleared");
  await expect(idInput).toHaveValue("");

  await secretInput.fill(secret);
  await save.click();
  await expect(alert).toContainText("The server rejected the submitted values.");
  await expect(alert).not.toContainText(secret);
  await expect(secretInput).toHaveValue("");
  // Saved flags are untouched by failures, and secrets were never retried.
  await expect(fact(page, "Sandbox", "Client secret")).toHaveText("Client secret Saved");
  expect(patches(requests).map((r) => r.body)).toEqual([
    { environment: "sandbox", client_id: clientId },
    { environment: "sandbox", client_secret: secret },
  ]);
  const html = await page.content();
  expect(html).not.toContain(clientId);
  expect(html).not.toContain(secret);
});

test("switching environment discards unsent values and the previous outcome", async ({ page }) => {
  const sandboxSecret = "synthetic-qbo-secret-0004";
  const verifier = "synthetic-qbo-verifier-0005";
  const { requests } = await mockApi(page, {
    respond: (method, path, body) =>
      method === "PATCH" && path === QBO_PATH && (body as Json).environment === "sandbox"
        ? { status: 422, json: { detail: "Synthetic sandbox validation failure." } }
        : undefined,
  });
  await page.goto("/settings");
  const region = panel(page);
  const secretInput = region.getByLabel("New client secret", { exact: true });
  const verifierInput = region.getByLabel("New webhook verifier token", { exact: true });

  await region.getByLabel("New client ID", { exact: true }).fill("synthetic-qbo-client-0004");
  await region.getByRole("button", { name: "Save sandbox app settings" }).click();
  await expect(region.getByRole("alert")).toContainText("Synthetic sandbox validation failure.");

  await secretInput.fill(sandboxSecret);
  await region.getByRole("radio", { name: "Production" }).check();
  await expect(region.getByRole("alert")).toHaveCount(0);
  await expect(secretInput).toHaveValue("");
  await region.getByRole("radio", { name: "Sandbox" }).check();
  await expect(secretInput).toHaveValue("");

  await region.getByRole("radio", { name: "Production" }).check();
  await verifierInput.fill(verifier);
  await region.getByRole("button", { name: "Save production app settings" }).click();
  await expect.poll(() => patches(requests).length).toBe(2);
  expect(patches(requests)[1].body).toEqual({ environment: "production", verifier_token: verifier });
  expect(requests.some((r) => r.raw?.includes(sandboxSecret))).toBe(false);
  await expect(fact(page, "Production", "Webhook verifier token")).toContainText("Saved (optional");
});

test("a failed load is reported honestly and can be retried", async ({ page }) => {
  let failing = true;
  await mockApi(page, {
    respond: (method, path) =>
      failing && method === "GET" && path === QBO_PATH
        ? { status: 503, json: { detail: "Synthetic outage." } }
        : undefined,
  });
  await page.goto("/settings");
  const region = panel(page);
  await expect(region.getByRole("alert")).toContainText("current state is unknown", { timeout: 15_000 });
  await expect(region.getByRole("alert")).toContainText("Synthetic outage.");
  await expect(region.getByLabel("New client ID", { exact: true })).toHaveCount(0);

  failing = false;
  await region.getByRole("button", { name: "Try again" }).click();
  await expect(fact(page, "Sandbox", "Client ID")).toHaveText("Client ID Saved");
  await expect(region.getByRole("alert")).toHaveCount(0);
});

test("Reviewer gets no QuickBooks panel and makes no QuickBooks requests", async ({ page }) => {
  const { requests } = await mockApi(page, { role: "reviewer" });
  await page.goto("/settings");
  await expect.poll(() => requests.some((r) => r.path === "/auth/me")).toBe(true);
  await page.waitForLoadState("networkidle");
  await expect(panel(page)).toHaveCount(0);
  expect(requests.some((r) => r.path.startsWith("/settings/connectors"))).toBe(false);
  expect(requests.some((r) => r.method !== "GET")).toBe(false);
});


test("changing the public base URL refreshes the registered QuickBooks redirect and webhook URLs", async ({ page }) => {
  let origin = ORIGIN;
  const nextOrigin = "https://updated-screening.example.test";
  const { requests } = await mockApi(page, {
    respond: (method, path, body) => {
      if (method === "GET" && path === QBO_PATH)
        return { status: 200, json: { sandbox: flags(true, true, true), production: flags(false, false, false), redirect_uri: `${origin}/api/v1/connectors/qbo/oauth/callback`, webhook_urls: { sandbox: `${origin}/api/v1/webhooks/qbo/sandbox`, production: `${origin}/api/v1/webhooks/qbo/production` } } };
      if (method === "PATCH" && path === "/settings/notifications") {
        origin = (body as Json).public_base_url as string;
        return { status: 200, json: { ...NOTIFICATIONS, public_base_url: origin } };
      }
      return undefined;
    },
  });
  await page.goto("/settings");
  await expect(panel(page).getByText(REDIRECT, { exact: true })).toBeVisible();
  const email = page.getByRole("region", { name: "Email notifications", exact: true });
  await email.getByLabel("Public base URL", { exact: true }).fill(nextOrigin);
  await email.getByRole("button", { name: "Save notification settings" }).click();
  await expect(panel(page).getByText(`${nextOrigin}/api/v1/connectors/qbo/oauth/callback`, { exact: true })).toBeVisible();
  await expect(panel(page).getByText(`${nextOrigin}/api/v1/webhooks/qbo/sandbox`, { exact: true })).toBeVisible();
  expect(requests.find((r) => r.method === "PATCH" && r.path === "/settings/notifications")).toMatchObject({ body: { public_base_url: nextOrigin }, csrf: CSRF });
});
