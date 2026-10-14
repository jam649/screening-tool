import { expect, test, type Page } from "@playwright/test";

test.use({ locale: "en-US", timezoneId: "UTC" });

const setup = {
  needs_setup: false,
  product_name: "screening-tool",
  disclaimer: "The CSL is one tool offered to assist users.",
  disclaimer_url: "https://developer.trade.gov/terms-of-service",
};
const reviewer = {
  user: {
    id: "reviewer-id",
    email: "riley@example.test",
    display_name: "Riley Reviewer",
    role: "reviewer",
    active: true,
  },
  csrf_token: "reviewer-csrf-token",
};
const source = {
  id: "source-erp",
  kind: "csv",
  label: "ERP customers",
  record_count: 150,
  last_synced_at: "2026-10-13T05:00:00Z",
  next_sync_at: null,
  health: "healthy",
  last_error: null,
  column_mapping: {},
};
const sourceSummary = {
  id: source.id,
  kind: "csv",
  label: source.label,
  last_synced_at: source.last_synced_at,
  record_count: 150,
  last_error: null,
};
const morgan = { id: "morgan-id", email: "morgan@example.test", display_name: "Morgan Lee" };
const CURRENT_HASH = "c".repeat(64);
const OLD_HASH = "a".repeat(64);

function partyRow(index: number, extra: Record<string, unknown> = {}) {
  const n = String(index + 1).padStart(3, "0");
  return {
    id: `party-${n}`,
    source_id: source.id,
    external_id: `C-${n}`,
    name: `Synthetic Party ${n}`,
    alt_names: [],
    party_type: "company",
    role: "customer",
    address_lines: [`${index + 1} Example Road`],
    city: "Leeds",
    region: null,
    postal_code: null,
    country_iso2: "GB",
    record_hash: n.padEnd(64, "0"),
    deleted_at: null,
    source: sourceSummary,
    status: index === 0 ? "unscreened" : "clear",
    screened_current: index !== 0,
    last_screened_at: index === 0 ? null : "2026-10-12T06:00:00Z",
    suppressed_pairings: 0,
    stale: true,
    auto_cleared: false,
    ...extra,
  };
}

const run = (id: string, version: string, started: string, finished: string) => ({
  id,
  trigger: "scheduled",
  csl_version_id: version,
  started_at: started,
  finished_at: finished,
  parties_screened: 150,
  hits_new: 0,
  hits_suppressed: 0,
  auto_cleared: 0,
  model_name: null,
  settings_snapshot: { review_threshold: 70 },
  model_fallbacks: [],
});

const suppressedHit = {
  id: "hit-sept",
  run_id: "run-sept",
  party_id: "party-nw",
  csl_entry_id: "entry-nw",
  party_hash: OLD_HASH,
  entry_hash: "e".repeat(64),
  party_name: "Northwind Trading",
  entry_name: "Northwind Trade Group",
  source_list: "SDN",
  det_score: 81,
  model_prob: null,
  final_confidence: 81,
  bucket: "review",
  status: "suppressed",
  created_at: "2026-09-20T06:00:00Z",
  party_snapshot: {
    name: "Northwind Trading",
    alt_names: [],
    party_type: "company",
    address_lines: ["5 Harbour Lane"],
    city: "Leeds",
    region: null,
    postal_code: null,
    country_iso2: "GB",
  },
  entry_snapshot: {
    name: "Northwind Trade Group",
    alt_names: [],
    addresses: [{ address: "Unit 9", city: "Riga", state: null, postal_code: null, country: "LV" }],
    countries: ["LV"],
    entity_type: "Entity",
    source_list: "SDN",
  },
  explanation: {
    components: { name: 86, country: -5, city: 0, address: 0, entity_type: 0, short_name: 0 },
    matched_tokens: { party: ["northwind"], entry: ["northwind"] },
    best_aliases: { party: "Northwind Trading", entry: "Northwind Trade Group" },
  },
  source_url: null,
  dispositions: [],
  prior_disposition: null,
  suppression_disposition: {
    id: "decision-fp",
    hit_id: "hit-aug",
    kind: "false_positive",
    actor_user_id: morgan.id,
    actor: morgan,
    note: "Registered in a different country; not the listed entity.",
    created_at: "2026-09-15T10:00:00Z",
  },
};

const partyFile = {
  party: partyRow(0, {
    id: "party-nw",
    name: "Northwind Trading Co",
    record_hash: CURRENT_HASH,
    status: "clear",
    screened_current: true,
    last_screened_at: "2026-10-13T06:01:30Z",
    stale: false,
  }),
  source: sourceSummary,
  events: [
    {
      run: run("run-zero", "version-2", "2026-10-13T06:00:00Z", "2026-10-13T06:01:30Z"),
      hits: [],
      previously_cleared: { count: 0, hits: [] },
      zero_hits: true,
      party_hash: CURRENT_HASH,
    },
    {
      run: run("run-sept", "version-1", "2026-09-20T06:00:00Z", "2026-09-20T06:01:00Z"),
      hits: [],
      previously_cleared: { count: 1, hits: [suppressedHit] },
      zero_hits: false,
      party_hash: OLD_HASH,
    },
  ],
  audit_log: [
    {
      id: "audit-1",
      created_at: "2026-09-15T10:00:00Z",
      action: "review.disposition",
      actor_user_id: morgan.id,
      actor: morgan,
      target_table: "dispositions",
      target_id: "decision-fp",
      before: null,
      after: { kind: "false_positive", note: suppressedHit.suppression_disposition.note, actor: morgan },
    },
  ],
};

async function mockApi(page: Page, handle: (path: string, url: URL) => unknown) {
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^\/api\/v1/, "");
    if (route.request().method() === "GET" && (path === "/notifications/status" || path === "/api/v1/notifications/status")) return route.fulfill({ json: { batches: [], health: [] } });
    if (route.request().method() === "GET" && (path === "/monitoring/status" || path === "/api/v1/monitoring/status")) return route.fulfill({ json: { enabled: true, schedules: [], operations: [], health: [] } });
    if (route.request().method() === "GET" && (path === "/settings/monitoring" || path === "/api/v1/settings/monitoring")) return route.fulfill({ json: { csl_fetch_time: "05:00", source_poll_minutes: 15, retention_weekday: "sun", retention_time: "03:00" } });
    if (path === "/setup") return route.fulfill({ json: setup });
    if (path === "/auth/me") return route.fulfill({ json: reviewer });
    if (path === "/sources") return route.fulfill({ json: [source] });
    const json = handle(path, url);
    return json === undefined
      ? route.fulfill({ status: 404, json: { detail: "Not found" } })
      : route.fulfill({ json });
  });
}

const fitsViewport = (page: Page) =>
  page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);

test("party filters and pages are requested from the server with exact ranges", async ({ page }) => {
  const requests: Record<string, string>[] = [];
  await mockApi(page, (path, url) => {
    if (path !== "/parties") return undefined;
    requests.push(Object.fromEntries(url.searchParams));
    const offset = Number(url.searchParams.get("offset"));
    const count = Math.min(100, 150 - offset);
    return { items: Array.from({ length: count }, (_, i) => partyRow(offset + i)), total: 150 };
  });
  await page.goto("/parties?stale=true");
  await expect(page.getByText("Showing 1–100 of 150 parties")).toBeVisible();
  expect(requests[0]).toEqual({ stale: "true", offset: "0", limit: "100" });
  await expect(page.getByLabel("Only stale parties")).toBeChecked();
  await expect(page.getByRole("row", { name: /Synthetic Party 001/ })).toContainText("Not screened yet");
  await expect(page.getByRole("row", { name: /Synthetic Party 002/ })).toContainText("No open item");
  await expect(page.getByRole("link", { name: "Synthetic Party 002" })).toHaveAttribute(
    "href",
    "/parties/party-002",
  );

  await page.getByRole("button", { name: "Next" }).click();
  await expect(page.getByText("Showing 101–150 of 150 parties")).toBeVisible();
  expect(requests.at(-1)).toEqual({ stale: "true", offset: "100", limit: "100" });
  await expect(page.getByRole("button", { name: "Next" })).toBeDisabled();

  await page.getByLabel("Search").fill("harbor");
  await page.getByLabel("Status").selectOption("unscreened");
  await page.getByLabel("Country").fill("nl");
  await page.getByRole("button", { name: "Apply filters" }).click();
  await expect
    .poll(() => requests.at(-1))
    .toEqual({ q: "harbor", country: "NL", status: "unscreened", stale: "true", offset: "0", limit: "100" });
  await expect(page.getByText("Showing 1–100 of 150 parties")).toBeVisible();
});

test("a zero-hit run shows its exact run, list version and party hash without inventing a decision", async ({
  page,
}) => {
  await mockApi(page, (path) => (path === "/parties/party-nw" ? partyFile : undefined));
  await page.goto("/parties/party-nw");
  await expect(page.getByRole("heading", { level: 1, name: "Northwind Trading Co" })).toBeVisible();
  const events = page.locator("li.rv-event");
  await expect(events.first()).toContainText("run-zero");
  const zero = events.filter({ hasText: "run-zero" });
  await expect(zero).toContainText("No candidate hits recorded in this run.");
  await expect(zero).toContainText("version-2");
  await expect(zero).toContainText(CURRENT_HASH);
  await expect(zero).not.toContainText(/false positive|cleared|confirmed|No open item|Previously/i);
});

test("previously cleared observations keep the historic name and the original reviewer's decision", async ({
  page,
}) => {
  await mockApi(page, (path) => (path === "/parties/party-nw" ? partyFile : undefined));
  await page.goto("/parties/party-nw");
  const sept = page.locator("li.rv-event").filter({ hasText: "run-sept" });
  await expect(sept).toContainText(OLD_HASH);
  const group = sept.getByText("Previously cleared (1)");
  await expect(group).toBeVisible();
  await expect(sept.getByText("Northwind Trading", { exact: true }).first()).toBeHidden();
  await group.click();
  const observation = sept.locator("details.rv-hit");
  const summary = observation.locator(":scope > summary");
  await expect(summary).toContainText("Northwind Trading compared with Northwind Trade Group");
  await expect(summary).not.toContainText("Northwind Trading Co");
  await expect(summary).toContainText("Remembered false positive · Morgan Lee");
  await summary.click();
  const remembered = observation.getByRole("region", { name: "Remembered false-positive decision" });
  await expect(remembered).toContainText("Morgan Lee (morgan@example.test)");
  await expect(remembered).toContainText("Registered in a different country; not the listed entity.");
  await expect(observation).toContainText("No separate decision was made on this observation");
  const table = observation.getByRole("table", { name: "Recorded at screening time" });
  await expect(table).toContainText("Northwind Trading");
  await expect(table).not.toContainText("Northwind Trading Co");
  await expect(page.getByRole("region", { name: "Audit log" })).toContainText("review.disposition");
});

test("the queue comparison and parties table fit a 390px screen", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const openHit = {
    ...suppressedHit,
    id: "hit-open",
    status: "open",
    suppression_disposition: null,
    source_url: "https://www.federalregister.gov/d/2026-04321",
  };
  const { party_snapshot, entry_snapshot, explanation, source_url, dispositions, prior_disposition, suppression_disposition, ...summary } =
    openHit;
  await mockApi(page, (path) => {
    if (path === "/queue") return { items: [summary], total: 1 };
    if (path === "/hits/hit-open") return openHit;
    if (path === "/parties")
      return {
        items: [
          partyRow(0),
          partyRow(1, { name: "An unusually long synthetic customer name with several words Limited" }),
        ],
        total: 2,
      };
    return undefined;
  });
  void [party_snapshot, entry_snapshot, explanation, source_url, dispositions, prior_disposition, suppression_disposition];
  await page.goto("/queue");
  await expect(page.getByRole("table", { name: "Recorded at screening time" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Clear as false positive/ })).toBeEnabled();
  expect(await fitsViewport(page)).toBe(true);
  await page.goto("/parties");
  await expect(page.getByRole("link", { name: "Synthetic Party 001" })).toBeVisible();
  expect(await fitsViewport(page)).toBe(true);
});

test("Home links real counts, states partial coverage, and shows setup only without data", async ({
  page,
}) => {
  let dashboard: Record<string, unknown> = {
    open_items: 4,
    likely_matches: 2,
    auto_cleared_since_report: 1,
    last_report_at: null,
    stale_parties: 3,
    total_parties: 10,
    parties_screened: 7,
    coverage_latest_version: 7,
    csl_version: {
      id: "version-2",
      fetched_at: "2026-10-12T15:30:00Z",
      entry_count: 12415,
      added: 9,
      changed: 4,
      removed: 2,
    },
    last_run: { id: "run-zero", finished_at: "2026-10-13T06:01:30Z" },
    sync_health: [
      { id: source.id, label: "ERP customers", last_synced_at: "2026-10-11T05:00:00Z", stale: true, last_error: null },
    ],
  };
  await mockApi(page, (path) => (path === "/dashboard" ? dashboard : undefined));
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Overview" })).toBeVisible();
  await expect(page.getByText(/7 of 10 parties screened against list version/)).toBeVisible();
  await expect(
    page.getByText(/3 parties have not yet been screened against the latest list version/),
  ).toBeVisible();
  const links: [RegExp, string][] = [
    [/Open items/, "/queue"],
    [/Likely matches/, "/queue?bucket=likely"],
    [/Auto-cleared/, "/parties?auto_cleared=true"],
    [/Stale parties/, "/parties?stale=true"],
  ];
  for (const [name, href] of links)
    await expect(page.getByRole("link", { name })).toHaveAttribute("href", href);
  await expect(page.getByRole("link", { name: /Open items/ })).toContainText("4");
  await expect(page.getByText(/ERP customers: last synced/)).toContainText("more than 24 hours ago");
  await expect(page.getByRole("link", { name: "Review sources" })).toHaveAttribute("href", "/sources");
  await expect(page.getByText("A clear place to begin.")).toHaveCount(0);

  dashboard = { ...dashboard, csl_version: null };
  await page.reload();
  await expect(page.getByRole("heading", { level: 1, name: "A clear place to begin." })).toBeVisible();
});


test("an identical repeat retains its original human assessment without inventing another decision", async ({ page }) => {
  const retained = { ...suppressedHit, status: "confirmed_match", suppression_disposition: null,
    completed_disposition: { ...suppressedHit.suppression_disposition!, kind: "confirmed_match", note: "Synthetic original confirmed assessment" } };
  const repeated = { ...partyFile, events: [{ ...partyFile.events[0], zero_hits: false, hits: [retained], previously_cleared: { count: 0, hits: [] } }] };
  await mockApi(page, path => path === "/parties/party-nw" ? repeated : undefined);
  await page.goto("/parties/party-nw");
  const observation = page.locator("details.rv-hit");
  await expect(observation.locator(":scope > summary")).toContainText("Previous human decision retained");
  await observation.locator(":scope > summary").click();
  const decision = observation.getByRole("region", { name: "Previous human decision retained" });
  await expect(decision).toContainText("Morgan Lee (morgan@example.test)");
  await expect(decision).toContainText("Synthetic original confirmed assessment");
  await expect(observation).toContainText("No separate decision was made on this observation");
});

test("Home uses the latest fetch timestamp and never calls an unfinished run complete", async ({ page }) => {
  await mockApi(page, path => path === "/dashboard" ? {
    open_items: 0, likely_matches: 0, auto_cleared_since_report: 0, last_report_at: null,
    stale_parties: 0, total_parties: 1, parties_screened: 1, coverage_latest_version: 1,
    csl_version: { id: "version-2", fetched_at: "2026-10-12T15:30:00Z", last_fetched_at: "2026-10-13T06:00:00Z", entry_count: 300, added: 0, changed: 0, removed: 0 },
    last_run: { id: "pending-run", finished_at: null }, sync_health: []
  } : undefined);
  await page.goto("/");
  await expect(page.locator("time[datetime='2026-10-13T06:00:00.000Z']")).toBeVisible();
  await expect(page.getByText("The latest screening run has not finished yet.")).toBeVisible();
  await expect(page.getByText(/The last completed screening run finished/)).toHaveCount(0);
});


test("Audit renders recorded run context and sends exact server-side action filters", async ({ page }) => {
  const requests: Record<string, string>[] = [];
  await mockApi(page, (path, url) => {
    if (path === "/runs") return { items: [{ ...run("run-audit", "version-2", "2026-10-13T06:00:00Z", "2026-10-13T06:01:30Z"), settings_snapshot: { review_threshold: 70, scope: { party_count: 150, entry_count: 300 } } }], total: 1 };
    if (path === "/audit-log") { requests.push(Object.fromEntries(url.searchParams)); return { items: partyFile.audit_log, total: 1 }; }
    return undefined;
  });
  await page.goto("/audit");
  const runRecord = page.locator("details.rv-run");
  await runRecord.locator(":scope > summary").click();
  await expect(runRecord).toContainText("run-audit");
  await expect(runRecord).toContainText("version-2");
  await expect(runRecord).toContainText("Not used");
  await runRecord.getByText("Settings used for this run", { exact: true }).click();
  await expect(runRecord.locator("pre")).toContainText('"party_count": 150');
  const filter = page.getByRole("form", { name: "Filter audit log" });
  await filter.getByLabel("Action", { exact: true }).fill("review.disposition");
  await filter.getByLabel("Record type").fill("dispositions");
  await filter.getByLabel("Record ID").fill("decision-fp");
  await filter.getByRole("button", { name: "Apply filters" }).click();
  await expect.poll(() => requests.at(-1)).toEqual({ action: "review.disposition", target_table: "dispositions", target_id: "decision-fp", offset: "0", limit: "100" });
  await expect(page.locator(".rv-audit")).toContainText("Morgan Lee (morgan@example.test)");
});


test("zero-hit participation and purged comparisons display actual permanent snapshots", async ({ page }) => {
  const recorded = structuredClone(partyFile);
  recorded.events = [
    { ...recorded.events[0], zero_hits: true, hits: [], previously_cleared: { count: 0, hits: [] },
      party_evidence: { party_snapshot: { name: "Historic zero-hit name" }, source_snapshot: { label: "Historic source" }, candidates_considered: 0, hits_recorded: 0, result_counts: {} } },
    { ...recorded.events[1], zero_hits: false, hits: [], previously_cleared: { count: 0, hits: [] },
      purged_comparisons: [{ certificate_id: "certificate-synthetic", certificate_sha256: "e".repeat(64),
        hit: { party_snapshot: { name: "Historic purged name" }, entry_snapshot: { name: "Historic listing" }, final_confidence: 87 },
        dispositions: [{ kind: "confirmed_match", note: "Synthetic original assessment", actor: { display_name: "Original Reviewer" } }] }] },
  ] as typeof recorded.events;
  await mockApi(page, path => path === "/parties/party-nw" ? recorded : undefined);
  await page.goto("/parties/party-nw");
  const zero = page.locator("li.rv-event").first();
  await zero.getByText("Party record preserved for this run").click();
  await expect(zero).toContainText("Historic zero-hit name");
  await expect(zero).toContainText("Historic source");
  const retained = page.locator("li.rv-event").nth(1);
  await expect(retained).not.toContainText("No candidate hits recorded");
  await retained.getByText("Comparison preserved in purge certificate").click();
  await expect(retained).toContainText("Historic purged name");
  await expect(retained).toContainText("Original Reviewer");
  await expect(retained).toContainText("Synthetic original assessment");
  await expect(retained).toContainText("certificate-synthetic");
});
