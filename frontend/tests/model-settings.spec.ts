import { expect, test, type Page } from "@playwright/test";

type Json = Record<string, unknown>;
type Reply = { status: number; json: unknown } | "abort";
type Recorded = {
  method: string;
  path: string;
  body: unknown;
  raw: string | null;
  csrf: string | null;
  contentType: string | null;
};
type ModelState = {
  provider: "none" | "jev" | "laya";
  generation: number;
  w_det: number;
  w_model: number;
  jev_model: string;
  laya_base_url: string;
  laya_model: string;
  laya_max_tokens: number;
  jev_api_key_configured: boolean;
  laya_api_key_configured: boolean;
  hosted_acknowledgment_valid: boolean;
  privacy_notice: string;
};
type Fixture = {
  role?: "admin" | "reviewer";
  model?: Partial<ModelState>;
  respond?: (method: string, path: string, body: Json, model: ModelState) => Reply | undefined;
};

// Synthetic values only; every request is intercepted and nothing reaches a model provider.
const CSRF = "csrf-synthetic-token";
const NOTICE =
  "Enabling Jev sends your customer and vendor names and addresses to TypeSafe AI for scoring. Laya keeps all data on this server.";
const MODEL_PATH = "/settings/model";
const TEST_PATH = "/settings/model/test";
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
  smtp_host: "", smtp_from: "", public_base_url: "https://screening.example.test", smtp_port: 587,
  smtp_tls: "starttls", report_recipients: [], report_time: "07:00", likely_alerts_enabled: false,
  smtp_username_configured: false, smtp_password_configured: false,
};
const EMPTY_STATUS = { batches: [], health: [] };

const connected = (model: ModelState) => ({
  status: "connected",
  provider: model.provider,
  generation: model.generation,
  requested_model: model.provider === "jev" ? model.jev_model : model.laya_model,
  returned_model: null,
  model_prob: 0,
  usage: { input_tokens: null, output_tokens: 12 },
  attempts: 1,
  elapsed_ms: 840,
  error_code: null,
});

function applyPatch(model: ModelState, changes: Json) {
  const state = model as unknown as Json;
  let changed = false;
  for (const key of ["provider", "w_det", "w_model", "laya_base_url"]) {
    if (key in changes && changes[key] !== state[key]) {
      state[key] = changes[key];
      changed = true;
    }
  }
  if (typeof changes.jev_api_key === "string") {
    model.jev_api_key_configured = true;
    model.hosted_acknowledgment_valid = changes.hosted_acknowledgment === NOTICE;
    changed = true;
  }
  if (typeof changes.laya_api_key === "string") {
    model.laya_api_key_configured = true;
    changed = true;
  }
  if (changes.clear_jev_api_key) {
    model.jev_api_key_configured = false;
    model.hosted_acknowledgment_valid = false;
    changed = true;
  }
  if (changes.clear_laya_api_key) {
    model.laya_api_key_configured = false;
    changed = true;
  }
  if (changed) model.generation += 1;
  return { ...model, pending_screening_task_id: changed ? `task-synthetic-${model.generation}` : null };
}

async function mockApi(page: Page, fixture: Fixture = {}) {
  const requests: Recorded[] = [];
  const role = fixture.role ?? "admin";
  const user = { id: "user-1", email: `${role}@example.test`, display_name: `Synthetic ${role}`, role, active: true };
  const model: ModelState = {
    provider: "none",
    generation: 3,
    w_det: 1,
    w_model: 0,
    jev_model: "jev-1.13.0",
    laya_base_url: "http://127.0.0.1:8009",
    laya_model: "multilingual",
    laya_max_tokens: 1024,
    jev_api_key_configured: false,
    laya_api_key_configured: false,
    hosted_acknowledgment_valid: false,
    privacy_notice: NOTICE,
    ...fixture.model,
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
    const headers = request.headers();
    requests.push({
      method, path, body, raw,
      csrf: headers["x-csrf-token"] ?? null,
      contentType: headers["content-type"] ?? null,
    });
    const reply = (status: number, json: unknown) =>
      route.fulfill({ status, contentType: "application/json", body: JSON.stringify(json) });
    const custom = fixture.respond?.(method, path, (body ?? {}) as Json, model);
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
      if (path === MODEL_PATH) return reply(200, model);
    }
    if (method === "PATCH" && path === MODEL_PATH) {
      const changes = body as Json;
      if (changes.expected_generation !== model.generation) return reply(409, { detail: "Stale generation." });
      return reply(200, applyPatch(model, changes));
    }
    if (method === "POST" && path === TEST_PATH) return reply(200, connected(model));
    // Other panels (connectors, sources, monitoring) get a contained failure.
    return reply(404, { detail: "Not available in this test fixture." });
  });
  return { requests, model };
}

const patches = (requests: Recorded[]) => requests.filter((r) => r.method === "PATCH" && r.path === MODEL_PATH);
const tests = (requests: Recorded[]) => requests.filter((r) => r.method === "POST" && r.path === TEST_PATH);
const modelGets = (requests: Recorded[]) => requests.filter((r) => r.method === "GET" && r.path === MODEL_PATH).length;
const panel = (page: Page) => page.getByRole("region", { name: "Model settings", exact: true });
const form = (page: Page) => panel(page).locator("form");
const formAlert = (page: Page) => form(page).getByRole("alert");
const formStatus = (page: Page) => form(page).getByRole("status");
const providerSelect = (page: Page) => panel(page).getByLabel("Model provider", { exact: true });
const detWeight = (page: Page) => panel(page).getByLabel("Deterministic weight", { exact: true });
const modelWeight = (page: Page) => panel(page).getByLabel("Model weight", { exact: true });
const endpoint = (page: Page) => panel(page).getByLabel("Laya endpoint", { exact: true });
const jevKey = (page: Page) => panel(page).getByLabel("New Jev API key", { exact: true });
const layaKey = (page: Page) => panel(page).getByLabel("New Laya API key", { exact: true });
const ack = (page: Page) => panel(page).getByRole("checkbox", { name: /I have read the notice above/ });
const clearJev = (page: Page) => panel(page).getByRole("checkbox", { name: "Clear the saved Jev API key" });
const clearLaya = (page: Page) => panel(page).getByRole("checkbox", { name: "Clear the saved Laya API key" });
const saveButton = (page: Page) => panel(page).getByRole("button", { name: "Save model settings" });
const reloadButton = (page: Page) => panel(page).getByRole("button", { name: "Reload configuration" });
const testButton = (page: Page) => panel(page).getByRole("button", { name: /^Test saved/ });
const testRegion = (page: Page) => panel(page).getByRole("region", { name: "Connection test" });
const fact = (page: Page, label: string) =>
  panel(page).getByRole("region", { name: "Saved configuration" }).getByRole("listitem")
    .filter({ has: page.getByText(label, { exact: true }) }).locator("span");
const testFact = (page: Page, label: string) =>
  testRegion(page).getByRole("listitem").filter({ has: page.getByText(label, { exact: true }) }).locator("span");

async function setWeights(page: Page, det: string, model: string) {
  await detWeight(page).fill(det);
  await modelWeight(page).fill(model);
}

test("loads the actual saved defaults with read-only pins and nothing to test", async ({ page }) => {
  const { requests } = await mockApi(page);
  await page.goto("/settings");
  await expect(detWeight(page)).toHaveValue("1");
  await expect(modelWeight(page)).toHaveValue("0");
  await expect(providerSelect(page)).toHaveValue("none");
  await expect(fact(page, "Configuration generation")).toHaveText("3");
  await expect(fact(page, "Jev model")).toContainText("jev-1.13.0");
  await expect(fact(page, "Laya model")).toContainText("multilingual, 1024-token");
  await expect(fact(page, "Jev API key")).toHaveText("Not saved");
  // Only the two weights are editable text; no model, context or token controls exist.
  await expect(panel(page).getByRole("textbox")).toHaveCount(2);
  await expect(providerSelect(page).locator("option")).toHaveCount(3);
  await expect(testButton(page)).toBeDisabled();
  expect(requests.filter((r) => r.method !== "GET")).toEqual([]);
});

test("saves only changed fields at the loaded generation with JSON and CSRF headers", async ({ page }) => {
  const { requests } = await mockApi(page);
  await page.goto("/settings");
  await saveButton(page).click();
  await expect(formStatus(page)).toHaveText("No changes to save.");
  expect(patches(requests)).toHaveLength(0);

  await setWeights(page, "0.75", "0.25");
  await saveButton(page).click();
  await expect(formStatus(page)).toContainText("generation 4");
  await expect(formStatus(page)).toContainText("task-synthetic-4");
  const [first] = patches(requests);
  expect(first.body).toEqual({ expected_generation: 3, w_det: 0.75, w_model: 0.25 });
  expect(first.contentType).toContain("application/json");
  expect(first.csrf).toBe(CSRF);
  await expect(fact(page, "Configuration generation")).toHaveText("4");

  await setWeights(page, "0.5", "0.5");
  await saveButton(page).click();
  await expect(formStatus(page)).toContainText("generation 5");
  expect(patches(requests)[1].body).toEqual({ expected_generation: 4, w_det: 0.5, w_model: 0.5 });
});

test("rejects out-of-range or unbalanced weights and sends legitimate decimals exactly", async ({ page }) => {
  const { requests } = await mockApi(page);
  await page.goto("/settings");
  await setWeights(page, "1.2", "-0.2");
  await saveButton(page).click();
  await expect(detWeight(page)).toHaveAttribute("aria-invalid", "true");
  await expect(modelWeight(page)).toHaveAttribute("aria-invalid", "true");

  await setWeights(page, "Infinity", "0");
  await saveButton(page).click();
  await expect(detWeight(page)).toHaveAttribute("aria-invalid", "true");

  await setWeights(page, "0.6", "0.3");
  await saveButton(page).click();
  await expect(panel(page).getByText(/must add up to 1/)).toBeVisible();
  await expect(formAlert(page)).toBeVisible();
  expect(patches(requests)).toHaveLength(0);

  await setWeights(page, "0.7", "0.3");
  await saveButton(page).click();
  await expect(formStatus(page)).toContainText("generation 4");
  expect(patches(requests)[0].body).toEqual({ expected_generation: 3, w_det: 0.7, w_model: 0.3 });
});

test("a new Jev key needs the exact server notice and a fresh acknowledgment every time", async ({ page }) => {
  const { requests } = await mockApi(page);
  await page.goto("/settings");
  await providerSelect(page).selectOption("jev");
  await expect(panel(page).getByText(NOTICE, { exact: true })).toBeVisible();

  await saveButton(page).click();
  await expect(jevKey(page)).toHaveAttribute("aria-invalid", "true");
  await expect(ack(page)).toBeDisabled();

  const firstKey = "  jev-synthetic-key-001 ";
  await jevKey(page).fill(firstKey);
  await saveButton(page).click();
  await expect(formAlert(page)).toBeVisible();
  await expect(jevKey(page)).toHaveValue("");
  expect(patches(requests)).toHaveLength(0);

  await jevKey(page).fill(firstKey);
  await ack(page).check();
  await saveButton(page).click();
  await expect(formStatus(page)).toContainText("generation 4");
  expect(patches(requests)[0].body).toEqual({
    expected_generation: 3, provider: "jev", jev_api_key: firstKey, hosted_acknowledgment: NOTICE,
  });
  await expect(jevKey(page)).toHaveValue("");
  await expect(ack(page)).not.toBeChecked();
  await expect(fact(page, "Jev API key")).toHaveText("Saved");

  // A replacement is never covered by the earlier acknowledgment.
  await jevKey(page).fill("jev-synthetic-key-002");
  await expect(ack(page)).not.toBeChecked();
  await saveButton(page).click();
  await expect(formAlert(page)).toBeVisible();
  expect(patches(requests)).toHaveLength(1);
  await jevKey(page).fill("jev-synthetic-key-002");
  await ack(page).check();
  await saveButton(page).click();
  await expect(formStatus(page)).toContainText("generation 5");
  expect(patches(requests)[1].body).toEqual({
    expected_generation: 4, jev_api_key: "jev-synthetic-key-002", hosted_acknowledgment: NOTICE,
  });
});

test("non-ASCII replacement keys are rejected locally and cleared without saving other drafts", async ({ page }) => {
  const { requests } = await mockApi(page);
  await page.goto("/settings");
  await providerSelect(page).selectOption("laya");
  await setWeights(page, "0.7", "0.3");
  // U+00A0 is not a C0/C1 control: it still cannot travel in a Bearer header.
  for (const value of ["synthetic\u00a0key", "synthetic\u00e9key", "synthetic\u{1f512}key"]) {
    await layaKey(page).fill(value);
    await saveButton(page).click();
    await expect(panel(page).getByText("Key must use printable ASCII characters", { exact: true })).toBeVisible();
    await expect(layaKey(page)).toHaveValue("");
    await expect(detWeight(page)).toHaveValue("0.7");
    await expect(modelWeight(page)).toHaveValue("0.3");
    expect(patches(requests)).toHaveLength(0);
    expect(tests(requests)).toHaveLength(0);
  }
  await providerSelect(page).selectOption("jev");
  await jevKey(page).fill("synthetic\u00a0key");
  await ack(page).check();
  await saveButton(page).click();
  await expect(panel(page).getByText("Key must use printable ASCII characters", { exact: true })).toBeVisible();
  await expect(jevKey(page)).toHaveValue("");
  await expect(ack(page)).not.toBeChecked();
  expect(patches(requests)).toHaveLength(0);
  expect(tests(requests)).toHaveLength(0);
});

test("a saved Jev key with valid proof can be selected; without valid proof it cannot", async ({ page }) => {
  const { requests } = await mockApi(page, { model: { jev_api_key_configured: true, hosted_acknowledgment_valid: true } });
  await page.goto("/settings");
  await providerSelect(page).selectOption("jev");
  await saveButton(page).click();
  await expect(formStatus(page)).toContainText("generation 4");
  expect(patches(requests)[0].body).toEqual({ expected_generation: 3, provider: "jev" });
});

test("an invalid acknowledgment forces a fresh Jev key before Jev can be selected", async ({ page }) => {
  const { requests } = await mockApi(page, { model: { jev_api_key_configured: true, hosted_acknowledgment_valid: false } });
  await page.goto("/settings");
  await providerSelect(page).selectOption("jev");
  await saveButton(page).click();
  await expect(jevKey(page)).toHaveAttribute("aria-invalid", "true");
  expect(patches(requests)).toHaveLength(0);
});

test("a 422 never echoes the key, clears it, keeps other edits and is not retried", async ({ page }) => {
  const secret = " laya-synthetic-secret-7 ";
  const { requests } = await mockApi(page, {
    model: { jev_api_key_configured: true, hosted_acknowledgment_valid: true },
    respond: (method, path, body) =>
      method === "PATCH" && path === MODEL_PATH
        ? { status: 422, json: { detail: `Rejected key ${String(body.laya_api_key).trim()}` } }
        : undefined,
  });
  await page.goto("/settings");
  await providerSelect(page).selectOption("laya");
  await layaKey(page).fill(secret);
  await setWeights(page, "0.5", "0.5");
  await saveButton(page).click();
  await expect(formAlert(page)).toBeVisible();
  await expect(formAlert(page)).not.toContainText("laya-synthetic-secret-7");
  await expect(layaKey(page)).toHaveValue("");
  await expect(providerSelect(page)).toHaveValue("laya");
  await expect(detWeight(page)).toHaveValue("0.5");
  const sent = patches(requests);
  expect(sent).toHaveLength(1);
  expect(sent[0].body).toEqual({ expected_generation: 3, provider: "laya", laya_api_key: secret, w_det: 0.5, w_model: 0.5 });
  expect(await page.content()).not.toContain("laya-synthetic-secret-7");
  const stored = await page.evaluate(() =>
    JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }) + location.href,
  );
  expect(stored).not.toContain("laya-synthetic-secret-7");
});

test("clearing is explicit, can target the inactive provider and excludes replacement", async ({ page }) => {
  const { requests } = await mockApi(page, {
    model: { provider: "laya", jev_api_key_configured: true, hosted_acknowledgment_valid: true, laya_api_key_configured: true },
  });
  await page.goto("/settings");
  await layaKey(page).fill("laya-synthetic-key");
  await expect(clearLaya(page)).toBeDisabled();
  await layaKey(page).fill("");
  await clearLaya(page).check();
  await expect(layaKey(page)).toBeDisabled();
  await clearLaya(page).uncheck();

  await clearJev(page).check();
  await saveButton(page).click();
  await expect(formStatus(page)).toContainText("generation 4");
  expect(patches(requests)[0].body).toEqual({ expected_generation: 3, clear_jev_api_key: true });
  await expect(fact(page, "Jev API key")).toHaveText("Not saved");
  await expect(fact(page, "Laya API key")).toHaveText("Saved");
  await expect(clearJev(page)).toHaveCount(0);
});

test("a 409 shows the latest facts and requires Reload configuration before saving again", async ({ page }) => {
  let first = true;
  const { requests } = await mockApi(page, {
    respond: (method, path, _body, model) => {
      if (method !== "PATCH" || path !== MODEL_PATH || !first) return undefined;
      first = false;
      Object.assign(model, { generation: 5, w_det: 0.9, w_model: 0.1 });
      return { status: 409, json: { detail: "Stale generation." } };
    },
  });
  await page.goto("/settings");
  await providerSelect(page).selectOption("laya");
  await layaKey(page).fill("laya-synthetic-key");
  await setWeights(page, "0.6", "0.4");
  await saveButton(page).click();
  await expect(reloadButton(page)).toBeVisible();
  await expect(fact(page, "Configuration generation")).toHaveText("5");
  await expect(saveButton(page)).toBeDisabled();
  await expect(layaKey(page)).toHaveValue("");
  // The old draft is not rebased onto generation 5.
  await expect(detWeight(page)).toHaveValue("0.6");
  expect(patches(requests)).toHaveLength(1);

  await reloadButton(page).click();
  await expect(detWeight(page)).toHaveValue("0.9");
  await expect(modelWeight(page)).toHaveValue("0.1");
  await expect(providerSelect(page)).toHaveValue("none");
  await expect(saveButton(page)).toBeEnabled();
  await setWeights(page, "0.8", "0.2");
  await saveButton(page).click();
  await expect(formStatus(page)).toContainText("generation 6");
  expect(patches(requests)[1].body).toEqual({ expected_generation: 5, w_det: 0.8, w_model: 0.2 });
});

test("a lost response is reported as uncertain, clears keys and refetches", async ({ page }) => {
  const { requests } = await mockApi(page, {
    respond: (method, path) => (method === "PATCH" && path === MODEL_PATH ? "abort" : undefined),
  });
  await page.goto("/settings");
  await providerSelect(page).selectOption("laya");
  await layaKey(page).fill("laya-synthetic-key");
  await expect(detWeight(page)).toHaveValue("1");
  const before = modelGets(requests);
  await saveButton(page).click();
  await expect(formAlert(page)).toContainText("unknown whether the change was saved");
  await expect(layaKey(page)).toHaveValue("");
  await expect.poll(() => modelGets(requests)).toBeGreaterThan(before);
  expect(patches(requests)).toHaveLength(1);
});

test("the Laya endpoint must be local and is sent exactly as entered", async ({ page }) => {
  const { requests } = await mockApi(page, { model: { provider: "laya" } });
  await page.goto("/settings");
  const rejected = [
    "http://192.168.1.20:8009",
    "https://laya.example.com",
    "http://127.0.0.1:8009/v1",
    "http://user@localhost:8009",
    "http://localhost:8009?debug=1",
    "http://localhost:8009#top",
    "http://127.000.0.1:8009",
    "http://localhost:0",
    "ftp://localhost:8009",
  ];
  for (const url of rejected) {
    await endpoint(page).fill(url);
    await saveButton(page).click();
    await expect(endpoint(page)).toHaveAttribute("aria-invalid", "true");
  }
  expect(patches(requests)).toHaveLength(0);

  const accepted = ["http://localhost:8009", "http://[::1]:8009", "http://host.docker.internal:8009", "https://127.0.0.2:8443/", "HTTP://LOCALHOST:08009", "http://[0:0:0:0:0:0:0:1]:8009"];
  for (const [index, url] of accepted.entries()) {
    await endpoint(page).fill(url);
    await saveButton(page).click();
    await expect(formStatus(page)).toContainText(`generation ${4 + index}`);
    expect(patches(requests)[index].body).toEqual({ expected_generation: 3 + index, laya_base_url: url });
  }
});

test("the connection test posts only the saved generation and shows reported facts honestly", async ({ page }) => {
  const { requests } = await mockApi(page, { model: { provider: "laya", generation: 7 } });
  await page.goto("/settings");
  await expect(testButton(page)).toHaveText("Test saved Laya connection");
  await detWeight(page).fill("0.9");
  await expect(testButton(page)).toBeDisabled();
  await detWeight(page).fill("1");
  await expect(testButton(page)).toBeEnabled();

  await testButton(page).click();
  await expect(testFact(page, "Tested generation")).toHaveText("7");
  const [sent] = tests(requests);
  expect(sent.raw).toBe(JSON.stringify({ expected_generation: 7 }));
  expect(sent.contentType).toContain("application/json");
  expect(sent.csrf).toBe(CSRF);
  await expect(testFact(page, "Returned model")).toHaveText("Not reported");
  await expect(testFact(page, "Model probability")).toHaveText("0");
  await expect(testFact(page, "Input tokens")).toHaveText("Not reported");
  await expect(testFact(page, "Output tokens")).toHaveText("12");
  await expect(testFact(page, "Attempts")).toHaveText("1");
  await expect(testFact(page, "Elapsed")).toHaveText("840 ms");
  expect(patches(requests)).toHaveLength(0);

  // A provider draft change discards the result; so does a saved configuration change.
  await providerSelect(page).selectOption("none");
  await expect(testRegion(page).getByRole("listitem")).toHaveCount(0);
  await providerSelect(page).selectOption("laya");
  await testButton(page).click();
  await expect(testFact(page, "Tested generation")).toHaveText("7");
  await setWeights(page, "0.9", "0.1");
  await saveButton(page).click();
  await expect(formStatus(page)).toContainText("generation 8");
  await expect(testRegion(page).getByRole("listitem")).toHaveCount(0);
});

test("a failed 200 result differs from HTTP errors, which never show raw payloads", async ({ page }) => {
  let mode: "failed" | "409" | "403" | "abort" = "failed";
  const raw = "synthetic private upstream detail";
  await mockApi(page, {
    model: { provider: "jev", jev_api_key_configured: true, hosted_acknowledgment_valid: true },
    respond: (method, path, _body, model) => {
      if (method !== "POST" || path !== TEST_PATH) return undefined;
      if (mode === "abort") return "abort";
      if (mode === "409") { model.generation += 1; return { status: 409, json: { detail: raw } }; }
      if (mode === "403") return { status: 403, json: { detail: raw } };
      return {
        status: 200,
        json: {
          status: "failed", provider: "jev", generation: model.generation, requested_model: "jev-1.13.0",
          returned_model: null, model_prob: null, usage: { input_tokens: null, output_tokens: null },
          attempts: 2, elapsed_ms: 5000, error_code: "provider_unreachable",
        },
      };
    },
  });
  await page.goto("/settings");
  await expect(testButton(page)).toHaveText("Test saved Jev connection");
  await testButton(page).click();
  await expect(testRegion(page).getByRole("alert")).toContainText("provider_unreachable");
  await expect(testFact(page, "Model probability")).toHaveText("Not reported");
  await expect(testFact(page, "Attempts")).toHaveText("2");

  mode = "409";
  await testButton(page).click();
  await expect(testRegion(page).getByRole("alert")).toContainText("no result for the current configuration was accepted");
  await expect(testRegion(page).getByRole("listitem")).toHaveCount(0);
  await expect(panel(page)).not.toContainText(raw);

  await expect(testButton(page)).toBeDisabled();
  await reloadButton(page).click();
  mode = "403";
  await testButton(page).click();
  await expect(testRegion(page).getByRole("alert")).toContainText("refused");
  await expect(panel(page)).not.toContainText(raw);

  mode = "abort";
  await testButton(page).click();
  await expect(testRegion(page).getByRole("alert")).toContainText("outcome of the connection test is unknown");
});

test("a 404 load shows no invented defaults and can be retried", async ({ page }) => {
  let missing = true;
  await mockApi(page, {
    respond: (method, path) =>
      method === "GET" && path === MODEL_PATH && missing ? { status: 404, json: { detail: "Not Found" } } : undefined,
  });
  await page.goto("/settings");
  await expect(panel(page).getByRole("alert")).toContainText("HTTP 404");
  await expect(panel(page).getByRole("textbox")).toHaveCount(0);
  await expect(providerSelect(page)).toHaveCount(0);
  missing = false;
  await panel(page).getByRole("button", { name: "Try again" }).click();
  await expect(detWeight(page)).toHaveValue("1");
});

test("reviewers get no panel and no model settings requests", async ({ page }) => {
  const { requests } = await mockApi(page, { role: "reviewer" });
  await page.goto("/settings");
  await expect.poll(() => requests.some((r) => r.path === "/auth/me")).toBe(true);
  await page.waitForLoadState("networkidle");
  await expect(panel(page)).toHaveCount(0);
  expect(requests.filter((r) => r.path.startsWith(MODEL_PATH))).toEqual([]);
});

test("fits a 390px viewport without horizontal overflow and with blank key inputs", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page, { model: { provider: "laya", jev_api_key_configured: true, laya_api_key_configured: true } });
  await page.goto("/settings");
  const overflow = () =>
    page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  await expect(layaKey(page)).toHaveValue("");
  expect(await overflow()).toBeLessThanOrEqual(0);
  await layaKey(page).evaluate(element => element.closest("fieldset")!.scrollIntoView({ block: "start" }));
  await page.screenshot({ path: testInfo.outputPath("model-settings-laya-390.png"), fullPage: false });

  await providerSelect(page).selectOption("jev");
  await expect(panel(page).getByText(NOTICE, { exact: true })).toBeVisible();
  await expect(jevKey(page)).toHaveValue("");
  await jevKey(page).focus();
  await expect(jevKey(page)).toBeFocused();
  expect(await overflow()).toBeLessThanOrEqual(0);
  await jevKey(page).evaluate(element => element.closest("fieldset")!.scrollIntoView({ block: "start" }));
  await page.screenshot({ path: testInfo.outputPath("model-settings-jev-390.png"), fullPage: false });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await jevKey(page).evaluate(element => element.closest("fieldset")!.scrollIntoView({ block: "start" }));
  await page.screenshot({ path: testInfo.outputPath("model-settings-jev-desktop.png"), fullPage: false });
});


test("numeric-equivalent weights and a discarded Laya endpoint do not block a saved provider test", async ({ page }) => {
  const { requests } = await mockApi(page, { model: { provider: "jev", jev_api_key_configured: true, hosted_acknowledgment_valid: true } });
  await page.goto("/settings");
  await setWeights(page, "1.0", "0.00");
  await expect(testButton(page)).toBeDisabled();
  await saveButton(page).click();
  await expect(formStatus(page)).toContainText("No changes to save.");
  await expect(testButton(page)).toBeEnabled();
  expect(patches(requests)).toHaveLength(0);
  await providerSelect(page).selectOption("laya");
  await endpoint(page).fill("http://host.docker.internal:8009");
  await providerSelect(page).selectOption("jev");
  await expect(testButton(page)).toBeEnabled();
  await testButton(page).click();
  expect(tests(requests)).toHaveLength(1);
  expect(tests(requests)[0].body).toEqual({ expected_generation: 3 });
  expect(patches(requests)).toHaveLength(0);
});

test("a saved generation change labels the prior test historical and requires an explicit form reload", async ({ page }) => {
  await page.clock.install();
  const { requests, model } = await mockApi(page, { model: { provider: "laya" } });
  await page.goto("/settings");
  await testButton(page).click();
  await expect(testFact(page, "Tested generation")).toHaveText("3");
  const before = modelGets(requests);
  model.generation = 4;
  await page.clock.fastForward(31_000);
  await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
  await expect.poll(() => modelGets(requests)).toBeGreaterThan(before);
  await expect(fact(page, "Configuration generation")).toHaveText("4");
  await expect(testRegion(page)).toContainText("This result is historical");
  await expect(testFact(page, "Tested generation")).toHaveText("3");
  await expect(saveButton(page)).toBeDisabled();
  await expect(testButton(page)).toBeDisabled();
  await reloadButton(page).click();
  await expect(testRegion(page).getByRole("listitem")).toHaveCount(0);
  await expect(testButton(page)).toBeEnabled();
  await testButton(page).click();
  await expect(testFact(page, "Tested generation")).toHaveText("4");
  expect(tests(requests).map(item => item.body)).toEqual([{ expected_generation: 3 }, { expected_generation: 4 }]);
  expect(patches(requests)).toHaveLength(0);
});
