import { expect, test, type Page } from "@playwright/test";

const CSRF = "csrf-synthetic-token";
const PARTY = "party-1";
type Role = "admin" | "reviewer";
type Reply = { status?: number; json: unknown };
type Handler = Reply | ((body: Record<string, unknown>) => Reply);
type Call = { key: string; csrf: string | undefined; body: unknown };

const user = (role: Role) => ({
  id: `user-${role}`,
  email: `${role}@example.test`,
  display_name: `Synthetic ${role}`,
  role,
  active: true,
});
const settings = (retention_years: number | null) => ({
  display_name: "Synthetic Workspace",
  timezone: "UTC",
  retention_years,
  auto_clear_enabled: false,
  auto_clear_threshold: 85,
  auto_clear_acknowledgment_audit_id: null,
  pending_screening_task_id: null,
  candidate_min: 60,
  review_threshold: 75,
  likely_threshold: 90,
  score_weights: {
    token_set: 0.4, token_sort: 0.3, partial: 0.3, country_match: 5, country_mismatch: -5,
    city_match: 3, address_max: 5, entity_mismatch: -10, short_name: -5,
  },
  legal_suffixes: ["inc"],
  candidate_stopwords: ["the"],
  enabled_source_lists: null,
  disclaimer: "Synthetic disclaimer.",
  disclaimer_url: "https://example.test/disclaimer",
});
const status = (overrides: Record<string, unknown> = {}) => ({
  enabled: true,
  years: 7,
  minimum_years: 5,
  inactive_parties: 2,
  active_holds: [],
  certificates: [],
  permanently_retained: ["audit_log", "parties"],
  ...overrides,
});
const protectedParty = {
  party_id: PARTY,
  eligible: false,
  reasons: ["eligibility_not_attested", "within_retention_period"],
  years: 7,
  basis_at: "2025-02-03T10:00:00Z",
  eligible_on: null,
  eligible_at: null,
  attestation: null,
  holds: [],
};

async function mockApi(page: Page, role: Role, handlers: Record<string, Handler>) {
  const calls: Call[] = [];
  const routes: Record<string, Handler> = {
    "GET /setup": {
      json: {
        needs_setup: false,
        product_name: "Synthetic Screening",
        disclaimer: "Synthetic disclaimer.",
        disclaimer_url: "https://example.test/disclaimer",
      },
    },
    "GET /auth/me": { json: { user: user(role), csrf_token: CSRF } },
    "GET /settings": { json: settings(7) },
    "GET /users": { json: { items: [user(role)], invitations: [] } },
    [`GET /parties/${PARTY}`]: { json: { party: { id: PARTY, name: "Synthetic Trading Co", status: "deleted", role: "vendor", party_type: "entity", alt_names: [], address_lines: [], suppressed_pairings: 0, stale: false }, source: null, events: [], audit_log: [] } },
    ...handlers,
  };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const key = `${request.method()} ${new URL(request.url()).pathname.replace(/^\/api\/v1/, "")}`;
    const body = request.postData() ? request.postDataJSON() : undefined;
    calls.push({ key, csrf: request.headers()["x-csrf-token"], body });
    const handler = routes[key];
    // Endpoints this suite does not describe stay unknown rather than defaulted.
    if (!handler) return route.fulfill({ status: 404, json: { detail: "Not available in this test" } });
    const reply = typeof handler === "function" ? handler(body) : handler;
    await route.fulfill({ status: reply.status ?? 200, json: reply.json });
  });
  return calls;
}

test("Admin settings places a workspace hold and needs a reference to release a government hold", async ({ page }) => {
  const holds: Record<string, unknown>[] = [
    { id: "hold-gov", party_id: null, kind: "government", reason: "Synthetic agency request", actor: null, created_at: "2026-01-02T03:04:05Z" },
  ];
  const calls = await mockApi(page, "admin", {
    "GET /retention/status": () => ({ json: status({ active_holds: [...holds] }) }),
    "POST /retention/holds": (body) => {
      const hold = { ...body, id: "hold-new", actor: "Synthetic admin", created_at: "2026-10-01T00:00:00Z" };
      holds.push(hold);
      return { status: 201, json: hold };
    },
    "POST /retention/holds/hold-gov/release": (body) => {
      holds.splice(0, 1);
      return { status: 201, json: { id: "rel-1", hold_id: "hold-gov", ...body, actor: "Synthetic admin", created_at: "2026-10-01T00:00:00Z" } };
    },
  });
  await page.goto("/settings");
  const retention = page.getByRole("region", { name: "Evidence retention" });
  await expect(retention.getByText(/Retention is on/)).toBeVisible();
  await expect(retention.getByText(/It is not full\s+erasure/)).toBeVisible();

  const government = retention.getByRole("listitem").filter({ hasText: "Synthetic agency request" });
  await expect(government.getByText(/Placed by identity not recorded/)).toBeVisible();
  await government.getByLabel("Release reason").fill("Agency closed the request");
  await government.getByRole("button", { name: "Release hold" }).click();
  await expect(government.getByText("A government hold needs the written authorization reference.")).toBeVisible();
  expect(calls.some((call) => call.key.endsWith("/release"))).toBe(false);

  await government.getByLabel("Written authorization reference").fill("Letter SYN-001");
  await government.getByRole("button", { name: "Release hold" }).click();
  await expect(retention.getByText("Synthetic agency request")).toHaveCount(0);
  expect(calls.find((call) => call.key === "POST /retention/holds/hold-gov/release")).toMatchObject({
    csrf: CSRF,
    body: { reason: "Agency closed the request", authorization_reference: "Letter SYN-001" },
  });

  const form = retention.getByRole("form", { name: "Workspace hold (all parties)" });
  await form.getByLabel("Reason for hold").fill("Synthetic litigation review");
  await form.getByRole("button", { name: "Place workspace hold" }).click();
  await expect(retention.getByRole("listitem").filter({ hasText: "Synthetic litigation review" })).toBeVisible();
  expect(calls.find((call) => call.key === "POST /retention/holds")).toMatchObject({
    csrf: CSRF,
    body: { party_id: null, kind: "other", reason: "Synthetic litigation review" },
  });
});

test("Party file attestation keeps edits on 422 and shows server reasons after saving", async ({ page }) => {
  let current: Record<string, unknown> = protectedParty;
  let attempts = 0;
  const calls = await mockApi(page, "admin", {
    [`GET /retention/parties/${PARTY}/eligibility`]: () => ({ json: current }),
    [`PATCH /retention/parties/${PARTY}/eligibility`]: (body) => {
      attempts += 1;
      if (attempts === 1) return { status: 422, json: { detail: "Synthetic validation failure" } };
      current = {
        ...protectedParty,
        reasons: ["external_event_date_unproven", "within_retention_period"],
        attestation: { id: "att-1", ...body, actor: null, created_at: "2026-10-01T00:00:00Z" },
      };
      return { json: current };
    },
    "POST /retention/holds": (body) => ({ status: 201, json: { ...body, id: "hold-p", actor: null, created_at: "2026-10-01T00:00:00Z" } }),
  });
  await page.goto(`/parties/${PARTY}`);
  const panel = page.getByRole("region", { name: "Evidence retention" });
  await expect(panel.getByText("No eligibility attestation has been recorded for this party.")).toBeVisible();

  const date = panel.getByLabel("Latest applicable external event date", { exact: true });
  const external = panel.getByLabel(/external event dates for this party were considered/);
  const government = panel.getByLabel(/known government holds were checked/);
  await expect(external).not.toBeChecked();
  await expect(government).not.toBeChecked();
  await date.fill("2024-05-06");
  await government.check();
  await panel.getByRole("button", { name: "Save attestation" }).click();
  await expect(panel.getByText("Synthetic validation failure")).toBeVisible();
  await expect(date).toHaveValue("2024-05-06");
  await expect(government).toBeChecked();

  await panel.getByRole("button", { name: "Save attestation" }).click();
  await expect(panel.getByText("The latest external event dates have not been attested as considered.")).toBeVisible();
  await expect(panel.getByText(/This party's evidence is protected/)).toBeVisible();
  const patches = calls.filter((call) => call.key === `PATCH /retention/parties/${PARTY}/eligibility`);
  expect(patches[1]).toEqual({
    key: `PATCH /retention/parties/${PARTY}/eligibility`,
    csrf: CSRF,
    body: { event_date: "2024-05-06", external_dates_considered: false, government_holds_excluded: true },
  });

  const hold = panel.getByRole("form", { name: "Hold for this party" });
  await hold.getByLabel("Hold type").selectOption("government");
  await hold.getByLabel("Reason for hold").fill("Synthetic agency notice");
  await hold.getByRole("button", { name: "Place party hold" }).click();
  await expect
    .poll(() => calls.find((call) => call.key === "POST /retention/holds"))
    .toMatchObject({ csrf: CSRF, body: { party_id: PARTY, kind: "government", reason: "Synthetic agency notice" } });
});

test("Apply runs directly, treats a failure as unknown and preview reports zero honestly", async ({ page }) => {
  let certificates: unknown[] = [];
  let applies = 0;
  const plan = { enabled: true, years: 7, as_of: "2026-10-01T12:00:00Z" };
  const certificate = { id: "cert-1", party_id: "party-9", sha256: "ab".repeat(32) };
  const calls = await mockApi(page, "admin", {
    "GET /retention/status": () => ({ json: status({ certificates }) }),
    "POST /retention/apply": () => {
      applies += 1;
      if (applies === 1) return { status: 503, json: { detail: "Synthetic upstream failure" } };
      certificates = [{ ...certificate, created_at: "2026-10-01T12:00:01Z" }];
      const bundle = { party_id: "party-9", attestation_id: "att-9", basis_at: "2019-01-01T00:00:00Z", eligible_on: "2026-01-01", eligible_at: "2026-01-01T00:00:00Z", hit_ids: ["hit-1", "hit-2"], disposition_ids: ["disp-1"] };
      return { json: { ...plan, bundles: [bundle], blocked: [], hits: 2, dispositions: 1, applied: true, certificates: [certificate] } };
    },
    "POST /retention/preview": { json: { ...plan, bundles: [], blocked: [{ party_id: "party-3", reasons: ["active_hold"] }], hits: 0, dispositions: 0 } },
  });
  await page.goto("/settings");
  const purge = page.getByRole("region", { name: "Purge evidence" });
  const apply = purge.getByRole("button", { name: "Apply retention now" });
  await apply.click();
  await expect(purge.getByRole("alert")).toContainText("The outcome is unknown");
  await expect(apply).toBeDisabled();
  await purge.getByRole("button", { name: "Refresh status" }).click();
  await expect(apply).toBeEnabled();

  await apply.click();
  await expect(purge.getByRole("status")).toContainText("Purged: 2 hit records, 1 disposition records, 1 parties.");
  const stored = page.getByRole("region", { name: "Purge certificates" });
  await expect(stored.getByRole("link", { name: "party-9" })).toBeVisible();
  expect(calls.filter((call) => call.key === "POST /retention/apply").every((call) => call.csrf === CSRF)).toBe(true);

  await purge.getByRole("button", { name: "Preview retention" }).click();
  await expect(purge.getByRole("status")).toContainText("Eligible now: 0 hit records, 0 disposition records, 0 parties.");
  await expect(purge.getByText("No parties are eligible right now.")).toBeVisible();
  await expect(purge.getByText("An active hold protects this party.")).toBeVisible();
});

test("Retention off keeps apply disabled and says so plainly", async ({ page }) => {
  await mockApi(page, "admin", {
    "GET /settings": { json: settings(null) },
    "GET /retention/status": { json: status({ enabled: false, years: null }) },
  });
  await page.goto("/settings");
  const retention = page.getByRole("region", { name: "Evidence retention" });
  await expect(retention.getByText(/Retention is off\. Nothing is purged\./)).toBeVisible();
  await expect(retention.getByRole("button", { name: "Apply retention now" })).toBeDisabled();
  await expect(retention.getByText("No purge certificates yet.")).toBeVisible();
});

test("Reviewer sessions never request retention endpoints", async ({ page }) => {
  const calls = await mockApi(page, "reviewer", {});
  await page.goto("/settings");
  await page.waitForLoadState("networkidle");
  await page.goto(`/parties/${PARTY}`);
  await page.waitForLoadState("networkidle");
  await expect(page.getByRole("region", { name: "Evidence retention" })).toHaveCount(0);
  expect(calls.some((call) => call.key === "GET /auth/me")).toBe(true);
  expect(calls.filter((call) => call.key.includes("/retention"))).toEqual([]);
});


test("new activity requires a fresh attestation and exposes the retained maximum date", async ({ page }) => {
  await mockApi(page, "admin", {
    [`GET /retention/parties/${PARTY}/eligibility`]: { json: {
      ...protectedParty,
      reasons: ["attestation_predates_activity"],
      observed_activity_at: "2026-10-12T12:00:00Z",
      latest_attested_event_date: "2024-05-06",
    } },
  });
  await page.goto(`/parties/${PARTY}`);
  const panel = page.getByRole("region", { name: "Evidence retention" });
  await expect(panel.getByText("A new attestation is required after the latest party activity.")).toBeVisible();
  await expect(panel.locator("dd").filter({ hasText: "2024-05-06" })).toBeVisible();
  await expect(panel.getByText(/An earlier date cannot lower a previously attested event date/)).toBeVisible();
  await expect(panel.getByText(/This party's evidence is protected/)).toBeVisible();
});


test("saving the General retention policy refreshes eligibility controls without reloading", async ({ page }) => {
  const general = settings(null);
  const calls = await mockApi(page, "admin", {
    "GET /settings": () => ({ json: { ...general } }),
    "PATCH /settings": body => { Object.assign(general, body); return { json: { ...general } }; },
    "GET /retention/status": () => ({ json: status({ enabled: general.retention_years !== null, years: general.retention_years }) }),
  });
  await page.goto("/settings");
  const retention = page.getByRole("region", { name: "Evidence retention", exact: true });
  const apply = retention.getByRole("button", { name: "Apply retention now" });
  await expect(retention.getByText(/Retention is off\. Nothing is purged\./)).toBeVisible();
  await expect(apply).toBeDisabled();
  await page.getByLabel("Enable retention", { exact: true }).check();
  await page.getByRole("button", { name: "Save general settings" }).click();
  await expect(retention.getByText(/Retention is on: evidence for inactive parties becomes eligible 5 years/)).toBeVisible();
  await expect(apply).toBeEnabled();
  expect(calls.filter(call => call.key === "PATCH /settings")).toEqual([{ key: "PATCH /settings", csrf: CSRF, body: { retention_years: 5 } }]);
  expect(calls.filter(call => call.key === "GET /retention/status").length).toBeGreaterThanOrEqual(2);
  await page.getByLabel("Enable retention", { exact: true }).uncheck();
  await page.getByRole("button", { name: "Save general settings" }).click();
  await expect(retention.getByText(/Retention is off\. Nothing is purged\./)).toBeVisible();
  await expect(apply).toBeDisabled();
});
