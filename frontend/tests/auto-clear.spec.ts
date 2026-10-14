import { expect, test, type Locator, type Page } from "@playwright/test";

// Synthetic fixtures only: no real accounts, secrets or customer data.
const CSRF = "synthetic-csrf-token";
const PHRASE = "I understand";
const admin = {
  id: "00000000-0000-4000-8000-000000000001",
  email: "admin@example.test",
  display_name: "Test Admin",
  role: "admin",
  active: true,
};
const reviewer = {
  ...admin,
  id: "00000000-0000-4000-8000-000000000002",
  email: "reviewer@example.test",
  display_name: "Test Reviewer",
  role: "reviewer",
};

type Json = Record<string, unknown>;
const settingsFixture = (overrides: Json = {}): Json => ({
  display_name: "Test workspace",
  timezone: "UTC",
  retention_years: null,
  auto_clear_enabled: false,
  auto_clear_threshold: 60,
  auto_clear_acknowledgment_audit_id: null,
  pending_screening_task_id: null,
  candidate_min: 40,
  review_threshold: 55,
  likely_threshold: 85,
  score_weights: {
    token_set: 0.4,
    token_sort: 0.3,
    partial: 0.3,
    country_match: 5,
    country_mismatch: -10,
    city_match: 3,
    address_max: 5,
    entity_mismatch: -10,
    short_name: -5,
  },
  legal_suffixes: [],
  candidate_stopwords: [],
  enabled_source_lists: null,
  disclaimer: "Synthetic disclaimer.",
  disclaimer_url: "https://example.test/notice",
  ...overrides,
});

async function mockApi(
  page: Page,
  options: { user?: Json; settings?: Json; reject?: string } = {},
) {
  const user = options.user ?? admin;
  let settings = options.settings ?? settingsFixture();
  const patches: { body: Json; csrf: string | undefined }[] = [];
  const calls: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/^\/api\/v1/, "");
    if (route.request().method() === "GET" && (path === "/notifications/status" || path === "/api/v1/notifications/status")) return route.fulfill({ json: { batches: [], health: [] } });
    if (route.request().method() === "GET" && (path === "/monitoring/status" || path === "/api/v1/monitoring/status")) return route.fulfill({ json: { enabled: true, schedules: [], operations: [], health: [] } });
    if (route.request().method() === "GET" && (path === "/settings/monitoring" || path === "/api/v1/settings/monitoring")) return route.fulfill({ json: { csl_fetch_time: "05:00", source_poll_minutes: 15, retention_weekday: "sun", retention_time: "03:00" } });
    const method = request.method();
    calls.push(`${method} ${path}`);
    const json = (body: unknown, status = 200) => route.fulfill({ status, json: body });
    if (path === "/setup")
      return json({
        needs_setup: false,
        product_name: "Screening Tool",
        disclaimer: "Synthetic disclaimer.",
        disclaimer_url: "https://example.test/notice",
      });
    if (path === "/auth/me") return json({ user, csrf_token: CSRF });
    if (path === "/users") return json({ items: [user], invitations: [] });
    if (path === "/settings" && method === "GET") return json(settings);
    if (path === "/settings" && method === "PATCH") {
      const body = request.postDataJSON() as Json;
      patches.push({ body, csrf: request.headers()["x-csrf-token"] });
      if (options.reject) return json({ detail: options.reject }, 422);
      const { auto_clear_acknowledgment: phrase, ...changes } = body;
      settings = {
        ...settings,
        ...changes,
        auto_clear_acknowledgment_audit_id: phrase
          ? `synthetic-audit-${patches.length}`
          : settings.auto_clear_acknowledgment_audit_id,
      };
      return json(settings);
    }
    return json({ detail: "Not available in this test." }, 404);
  });
  return { patches, calls };
}

async function openAutoClear(page: Page) {
  await page.goto("/");
  await page
    .getByRole("navigation")
    .getByRole("link", { name: /^(admin )?settings$/i })
    .click();
  const region = page.getByRole("region", { name: "Auto-clear" });
  await expect(region).toBeVisible();
  return region;
}

const controls = (region: Locator) => ({
  enable: region.getByRole("checkbox", { name: "Enable auto-clear" }),
  threshold: region.getByRole("spinbutton", { name: "Auto-clear threshold", exact: true }),
  slider: region.getByRole("slider", { name: "Adjust auto-clear threshold" }),
  phrase: region.getByRole("textbox", { name: /^Type "I understand"/ }),
  apply: region.getByRole("button", { name: "Apply auto-clear settings" }),
  status: region.getByRole("status"),
});

test("turning auto-clear on needs the exact phrase, sent once in a flat PATCH", async ({ page }) => {
  const api = await mockApi(page);
  const region = await openAutoClear(page);
  const c = controls(region);
  await expect(c.enable).not.toBeChecked();
  await c.enable.check();
  await expect(c.apply).toBeDisabled();
  for (const wrong of ["i understand", "I understand.", " I understand"]) {
    await c.phrase.fill(wrong);
    await expect(c.apply).toBeDisabled();
  }
  await c.phrase.fill(PHRASE);
  await c.apply.click();
  await expect(c.status).toHaveText(/saved/i);
  // The proof reference is server-owned: never written by the client or shown.
  expect(api.patches).toEqual([
    { body: { auto_clear_enabled: true, auto_clear_acknowledgment: PHRASE }, csrf: CSRF },
  ]);
  await expect(c.enable).toBeChecked();
  await expect(c.phrase).toHaveCount(0);
  await expect(region).not.toContainText("synthetic-audit");
});

test("enabled threshold changes need no phrase and reject out-of-range values", async ({ page }) => {
  const api = await mockApi(page, {
    settings: settingsFixture({
      auto_clear_enabled: true,
      auto_clear_acknowledgment_audit_id: "synthetic-audit-0",
    }),
  });
  const c = controls(await openAutoClear(page));
  for (const invalid of ["54", "86", "60.5", ""]) {
    await c.threshold.fill(invalid);
    await expect(c.apply).toBeDisabled();
  }
  await c.slider.fill("70");
  await expect(c.threshold).toHaveValue("70");
  await expect(c.phrase).toHaveCount(0);
  await c.apply.click();
  await expect(c.status).toHaveText(/saved/i);
  expect(api.patches).toEqual([{ body: { auto_clear_threshold: 70 }, csrf: CSRF }]);
});

test("disabling then re-enabling requires a fresh phrase", async ({ page }) => {
  const api = await mockApi(page, {
    settings: settingsFixture({
      auto_clear_enabled: true,
      auto_clear_acknowledgment_audit_id: "synthetic-audit-0",
    }),
  });
  const c = controls(await openAutoClear(page));
  await c.enable.uncheck();
  await c.apply.click();
  await expect(c.status).toHaveText(/saved/i);
  await c.enable.check();
  await expect(c.phrase).toHaveValue("");
  await expect(c.apply).toBeDisabled();
  await c.phrase.fill(PHRASE);
  await c.apply.click();
  await expect(c.status).toHaveText(/saved/i);
  expect(api.patches.map((p) => p.body)).toEqual([
    { auto_clear_enabled: false },
    { auto_clear_enabled: true, auto_clear_acknowledgment: PHRASE },
  ]);
});

test("a server 422 keeps the edits and phrase without retrying", async ({ page }) => {
  const api = await mockApi(page, { reject: "Acknowledgment was not accepted." });
  const region = await openAutoClear(page);
  const c = controls(region);
  await c.threshold.fill("65");
  await c.enable.check();
  await c.phrase.fill(PHRASE);
  await c.apply.click();
  await expect(region.getByRole("alert")).toHaveText("Acknowledgment was not accepted.");
  await expect(c.enable).toBeChecked();
  await expect(c.threshold).toHaveValue("65");
  await expect(c.phrase).toHaveValue(PHRASE);
  await expect(page.getByRole("dialog")).not.toContainText("Auto-clear settings saved.");
  expect(api.patches).toHaveLength(1);
});

test("missing auto-clear fields show no controls", async ({ page }) => {
  const partial = settingsFixture();
  delete partial.auto_clear_enabled;
  delete partial.auto_clear_threshold;
  await mockApi(page, { settings: partial });
  const region = await openAutoClear(page);
  const c = controls(region);
  await expect(region.getByRole("alert")).toContainText(/unavailable/i);
  await expect(c.enable).toHaveCount(0);
  await expect(c.apply).toHaveCount(0);
});

test("reviewers get no settings navigation or settings requests", async ({ page }) => {
  const api = await mockApi(page, { user: reviewer });
  await page.goto("/");
  const nav = page.getByRole("navigation");
  await expect(nav).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expect(nav.getByRole("link", { name: /^(admin )?settings$/i })).toHaveCount(0);
  expect(api.calls.filter((call) => /^\w+ \/(settings|users)(\/|$)/.test(call))).toEqual([]);
});


test("Escape cancels the acknowledgment dialog and restores focus without a request", async ({ page }) => {
  const api = await mockApi(page);
  const c = controls(await openAutoClear(page));
  await c.enable.check();
  const dialog = page.getByRole("dialog", { name: "Turn on auto-clear?" });
  await expect(dialog).toBeVisible();
  await expect(c.phrase).toBeFocused();
  await c.phrase.fill(PHRASE);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(c.enable).not.toBeChecked();
  await expect(c.enable).toBeFocused();
  expect(api.patches).toHaveLength(0);
  await c.enable.check();
  await expect(c.phrase).toHaveValue("");
  await expect(c.apply).toBeDisabled();
});

test("fractional review bounds retain valid whole-number auto-clear choices", async ({ page }) => {
  const api = await mockApi(page, { settings: settingsFixture({ review_threshold: 55.5, likely_threshold: 85.5 }) });
  const region = await openAutoClear(page);
  const ui = controls(region);
  await expect(ui.slider).toHaveAttribute("min", "56");
  await expect(ui.slider).toHaveAttribute("max", "85");
  await expect(region.getByText(/Saved review: 55.5; likely: 85.5/)).toBeVisible();
  await ui.threshold.fill("55");
  await expect(ui.apply).toBeDisabled();
  expect(api.patches).toHaveLength(0);
  await ui.threshold.fill("56");
  await ui.apply.click();
  await expect(ui.status).toHaveText("Auto-clear settings saved.");
  expect(api.patches).toEqual([{ csrf: CSRF, body: { auto_clear_threshold: 56 } }]);
});
