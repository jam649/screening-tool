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
const SOURCE_URL = "https://www.federalregister.gov/d/2026-01234";
const SUMMARY_KEYS = [
  "id",
  "run_id",
  "party_id",
  "csl_entry_id",
  "party_hash",
  "entry_hash",
  "party_name",
  "entry_name",
  "source_list",
  "det_score",
  "model_prob",
  "final_confidence",
  "bucket",
  "status",
  "created_at",
];

type Hit = Record<string, unknown> & { id: string };

const partySnapshot = (name: string, alt_names: string[] = []) => ({
  name,
  alt_names,
  party_type: "company",
  role: "customer",
  address_lines: ["12 Quay Street"],
  city: "Rotterdam",
  region: null,
  postal_code: "3011 AA",
  country_iso2: "NL",
});

function makeHit(
  id: string,
  party: string,
  entry: string,
  bucket: string,
  confidence: number,
  extra: Record<string, unknown> = {},
): Hit {
  return {
    id,
    run_id: "run-2026-10-12",
    party_id: `party-${id}`,
    csl_entry_id: `entry-${id}`,
    party_hash: `${id}-party-hash`,
    entry_hash: `${id}-entry-hash`,
    party_name: party,
    entry_name: entry,
    source_list: "EL",
    det_score: confidence,
    model_prob: null,
    final_confidence: confidence,
    bucket,
    status: "open",
    created_at: "2026-10-12T09:00:00Z",
    party_snapshot: partySnapshot(party),
    entry_snapshot: {
      name: entry,
      alt_names: [],
      addresses: [
        { address: "14 Quay Street", city: "Rotterdam", state: null, postal_code: "3011 AA", country: "NL" },
      ],
      countries: ["NL"],
      entity_type: "Entity",
      source_list: "EL",
    },
    explanation: {
      components: { name: confidence - 5, country: 2, city: 3, address: 0, entity_type: 0, short_name: 0 },
      matched_tokens: { party: [], entry: [] },
      best_aliases: { party, entry },
    },
    source_url: SOURCE_URL,
    dispositions: [],
    prior_disposition: null,
    suppression_disposition: null,
    ...extra,
  };
}

async function mockReview(page: Page, hits: Hit[], { conflict = false } = {}) {
  const open = [...hits];
  const posts: { id: string; body: unknown; csrf: string | undefined }[] = [];
  const requests: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/^\/api\/v1/, "");
    if (route.request().method() === "GET" && (path === "/notifications/status" || path === "/api/v1/notifications/status")) return route.fulfill({ json: { batches: [], health: [] } });
    if (route.request().method() === "GET" && (path === "/monitoring/status" || path === "/api/v1/monitoring/status")) return route.fulfill({ json: { enabled: true, schedules: [], operations: [], health: [] } });
    if (route.request().method() === "GET" && (path === "/settings/monitoring" || path === "/api/v1/settings/monitoring")) return route.fulfill({ json: { csl_fetch_time: "05:00", source_poll_minutes: 15, retention_weekday: "sun", retention_time: "03:00" } });
    requests.push(`${request.method()} ${path}`);
    if (path === "/setup") return route.fulfill({ json: setup });
    if (path === "/auth/me") return route.fulfill({ json: reviewer });
    if (path === "/queue")
      return route.fulfill({
        json: {
          items: open.map((hit) => Object.fromEntries(SUMMARY_KEYS.map((key) => [key, hit[key]]))),
          total: open.length,
        },
      });
    const decided = /^\/hits\/([^/]+)\/dispositions$/.exec(path);
    if (decided && request.method() === "POST") {
      const id = decided[1];
      const body = request.postDataJSON() as { kind: string; note: string };
      posts.push({ id, body, csrf: request.headers()["x-csrf-token"] });
      // Either way the item leaves the server queue: decided here or by someone else.
      const index = open.findIndex((hit) => hit.id === id);
      if (index >= 0) open.splice(index, 1);
      if (conflict)
        return route.fulfill({
          status: 409,
          json: { detail: "This item changed or was already reviewed; refresh the queue" },
        });
      const { id: actorId, email, display_name } = reviewer.user;
      return route.fulfill({
        status: 201,
        json: {
          disposition: {
            id: `decision-${id}`,
            hit_id: id,
            kind: body.kind,
            actor_user_id: actorId,
            actor: { id: actorId, email, display_name },
            note: body.note,
            created_at: "2026-10-13T12:00:00Z",
          },
          observations_dispositioned: 1,
          hit_id: id,
        },
      });
    }
    const detail = /^\/hits\/([^/]+)$/.exec(path);
    const found = detail && hits.find((hit) => hit.id === detail[1]);
    if (found) return route.fulfill({ json: found });
    return route.fulfill({ status: 404, json: { detail: "Not found" } });
  });
  return { posts, requests };
}

const clearButton = (page: Page) => page.getByRole("button", { name: /Clear as false positive/ });

test("a Reviewer works the filtered queue with J/K and F/M/E while notes stay ordinary text", async ({
  page,
}) => {
  const api = await mockReview(page, [
    makeHit("hit-a", "Harbor Freight Partners", "Harbour Freight Partners LLC", "likely", 96),
    makeHit("hit-b", "Larkspur Components", "Larkspur Component Works", "likely", 91),
    makeHit("hit-c", "Ostrava Metals", "Ostrava Metal Group", "review", 78),
  ]);
  await page.goto("/queue?bucket=likely");
  const list = page.getByRole("navigation", { name: "Open items" });
  const harbor = list.getByRole("button", { name: /Harbor Freight Partners/ });
  const larkspur = list.getByRole("button", { name: /Larkspur Components/ });
  await expect(list.getByRole("button")).toHaveCount(2);
  await expect(harbor).toHaveAttribute("aria-current", "true");
  await expect(clearButton(page)).toBeEnabled();
  await expect(clearButton(page)).toHaveAttribute("aria-keyshortcuts", "F");

  await page.keyboard.press("k");
  await expect(page.getByText("This is the first item in this view.")).toBeVisible();
  await page.keyboard.press("Control+j");
  await expect(harbor).toHaveAttribute("aria-current", "true");
  await page.keyboard.press("j");
  await expect(larkspur).toHaveAttribute("aria-current", "true");
  await page.keyboard.press("k");
  await expect(harbor).toHaveAttribute("aria-current", "true");

  const note = page.getByLabel("Decision note");
  await expect(page.getByRole("button", { name: /Confirmed match/ })).toBeDisabled();
  await expect(page.getByRole("button", { name: /Escalate/ })).toBeDisabled();
  await page.keyboard.press("m");
  await expect(note).toBeFocused();
  await expect(page.getByText("Add a note explaining why this is a match.")).toBeVisible();
  await note.pressSequentially("Registry number matches; j k f e v");
  await expect(note).toHaveValue("Registry number matches; j k f e v");
  await expect(harbor).toHaveAttribute("aria-current", "true");
  expect(api.posts).toHaveLength(0);

  await expect(page.getByRole("button", { name: /Confirmed match/ })).toBeEnabled();
  await page.getByRole("button", { name: /Confirmed match/ }).click();
  await expect(page.getByText(/Recorded .Confirmed match. for Harbor Freight Partners/)).toBeVisible();
  expect(api.posts).toEqual([
    {
      id: "hit-a",
      body: { kind: "confirmed_match", note: "Registry number matches; j k f e v" },
      csrf: reviewer.csrf_token,
    },
  ]);
  await expect(list.getByRole("button")).toHaveCount(1);
  await expect(larkspur).toHaveAttribute("aria-current", "true");
  await expect(list.getByText("Ostrava Metals")).toHaveCount(0);
  await expect(note).toHaveValue("");

  const heading = page.getByRole("heading", { level: 2, name: /Larkspur Components/ });
  await expect(heading).toBeVisible();
  await expect(clearButton(page)).toBeEnabled();
  await page.keyboard.press("e");
  await expect(note).toBeFocused();
  await expect(page.getByText("Add a note explaining why this needs escalation.")).toBeVisible();
  expect(api.posts).toHaveLength(1);
  await heading.click();
  await page.keyboard.press("f");
  await expect.poll(() => api.posts.length).toBe(2);
  expect(api.posts[1]).toEqual({
    id: "hit-b",
    body: { kind: "false_positive", note: "" },
    csrf: reviewer.csrf_token,
  });
  await expect(page.getByText("No likely matches are open.")).toBeVisible();
  await expect(page.getByText("1 other open item is in the queue.")).toBeVisible();
  await page.getByRole("button", { name: "Show all open items" }).click();
  await expect(page).toHaveURL("/queue");
  await expect(list.getByRole("button", { name: /Ostrava Metals/ })).toHaveAttribute(
    "aria-current",
    "true",
  );
});

test("a conflicting decision records nothing and reloads the queue", async ({ page }) => {
  const api = await mockReview(
    page,
    [makeHit("hit-a", "Harbor Freight Partners", "Harbour Freight Partners LLC", "likely", 96)],
    { conflict: true },
  );
  await page.goto("/queue");
  await expect(clearButton(page)).toBeEnabled();
  const queueReads = () => api.requests.filter((request) => request === "GET /queue").length;
  const before = queueReads();
  await page.keyboard.press("f");
  const alert = page.getByRole("alert");
  await expect(alert).toContainText("already reviewed");
  await expect(alert).toContainText("No decision was recorded");
  await expect.poll(queueReads).toBeGreaterThan(before);
  await expect(page.getByText("There are no open items in the review queue.")).toBeVisible();
  await expect(page.getByText(/Recorded “/)).toHaveCount(0);
  expect(api.posts).toHaveLength(1);
});

test("the comparison shows recorded snapshots, matched words, the real prior decision and what changed", async ({
  page,
}) => {
  const prior = {
    id: "decision-old",
    hit_id: "hit-old",
    kind: "false_positive",
    actor_user_id: "morgan-id",
    actor: { id: "morgan-id", email: "morgan@example.test", display_name: "Morgan Lee" },
    note: "Different registry number and city.",
    created_at: "2026-09-02T10:00:00Z",
    what_changed: {
      party: {
        name: { before: "Harbor Freight", after: "Harbor Freight Partners" },
        party_type: { before: "company", after: "individual" },
      },
      entry: {},
    },
  };
  const api = await mockReview(page, [
    makeHit("hit-a", "Harbor Freight Partners", "Harbour Freight Partners LLC", "likely", 88.5, {
      party_snapshot: partySnapshot("Harbor Freight Partners", ["HFP Logistics"]),
      prior_disposition: prior,
      explanation: {
        components: { name: 84, country: 2, city: 3, address: 0, entity_type: 0, short_name: 0 },
        matched_tokens: { party: ["harbor", "freight", "partners"], entry: ["harbour", "freight", "partners"] },
        best_aliases: { party: "Harbor Freight Partners", entry: "Harbour Freight Partners LLC" },
      },
    }),
  ]);
  await page.goto("/queue");
  const table = page.getByRole("table", { name: "Recorded at screening time" });
  await expect(table.locator("mark")).toHaveText([
    "Harbor",
    "Freight",
    "Partners",
    "Harbour",
    "Freight",
    "Partners",
  ]);
  await expect(table.getByRole("row", { name: /Alternate names/ })).toContainText("HFP Logistics");
  await expect(page.locator(".rv-scores")).toContainText("Model probability");
  await expect(page.locator(".rv-scores")).toContainText("Not used");
  await expect(page.getByText(/Names compare at 84 of 100/)).toBeVisible();
  const earlier = page.getByRole("region", { name: "Earlier decision on this pairing" });
  await expect(earlier).toContainText("Previously: Cleared as false positive");
  await expect(earlier).toContainText("Morgan Lee (morgan@example.test)");
  await expect(earlier).toContainText("Different registry number and city.");
  await expect(earlier).toContainText("Party type");
  await expect(earlier).toContainText("company");
  await expect(earlier).toContainText("individual");
  await expect(earlier.locator(".rv-before").first()).toHaveText("Harbor Freight");
  await expect(page.getByText("No decision has been recorded on this comparison yet.")).toBeVisible();
  expect(api.requests.some((request) => request.includes("/parties"))).toBe(false);
});

test("V opens only a safe http(s) source in a new tab", async ({ page, context }) => {
  await context.route("https://www.federalregister.gov/**", (route) =>
    route.fulfill({ contentType: "text/html", body: "<title>Notice</title>" }),
  );
  await mockReview(page, [
    makeHit("hit-a", "Harbor Freight Partners", "Harbour Freight Partners LLC", "likely", 96, {
      source_url: "javascript:alert(document.domain)",
    }),
    makeHit("hit-b", "Larkspur Components", "Larkspur Component Works", "likely", 91),
  ]);
  let popups = 0;
  page.on("popup", () => popups++);
  await page.goto("/queue");
  await expect(clearButton(page)).toBeEnabled();
  await expect(page.getByRole("link", { name: /View source/ })).toHaveCount(0);
  await page.keyboard.press("v");
  await expect(page.getByText("No source link is available for this listing.")).toBeVisible();
  expect(popups).toBe(0);
  await page.keyboard.press("j");
  const link = page.getByRole("link", { name: /View source/ });
  await expect(link).toHaveAttribute("href", SOURCE_URL);
  await expect(link).toHaveAttribute("rel", "noopener noreferrer");
  await expect(link).toHaveAttribute("aria-keyshortcuts", "V");
  const popup = page.waitForEvent("popup");
  await page.keyboard.press("v");
  await expect(await popup).toHaveURL(SOURCE_URL);
});


test("recorded ASCII/native alignment and address evidence highlight raw text conservatively", async ({ page }) => {
  await mockReview(page, [makeHit("hit-native", "Иван Петров", "Ivan Petrov", "review", 81, {
    party_snapshot: { ...partySnapshot("Иван Петров", ["重庆"]), address_lines: ["12 Harbour Road"] },
    entry_snapshot: { name: "Ivan Petrov", alt_names: [], addresses: [{ address: "80 Harbour Road" }], countries: [], entity_type: "Individual" },
    explanation: { matched_tokens: { name: ["ivan", "petrov"], address: ["harbour", "road"] },
      normalized: { party: { unicode: "иван петров", ascii: "ivan petrov" }, entry: { unicode: "ivan petrov", ascii: "ivan petrov" }, compared_party: "ivan petrov", compared_entry: "ivan petrov" } }
  })]);
  await page.goto("/queue");
  const table = page.getByRole("table", { name: "Recorded at screening time" });
  await expect(table.getByRole("row", { name: /^Name / }).locator("mark")).toHaveText(["Иван", "Петров", "Ivan", "Petrov"]);
  await expect(table.getByRole("row", { name: /^Address / }).locator("mark")).toHaveText(["Harbour", "Road", "Harbour", "Road"]);
  await expect(table.getByRole("row", { name: /Alternate names/ }).locator("mark")).toHaveCount(0);
});

test("one native word expanding to multiple ASCII tokens requires all recorded tokens", async ({ page }) => {
  const hit = makeHit("hit-expansion", "重庆", "Chong Qing", "review", 81, {
    party_snapshot: partySnapshot("重庆"),
    explanation: { matched_tokens: { name: ["chong"] }, normalized: { party: { unicode: "重庆", ascii: "chong qing" } } }
  });
  await mockReview(page, [hit]);
  await page.goto("/queue");
  const ownName = page.getByRole("row", { name: /^Name / }).locator("td").first();
  await expect(ownName).toHaveText("重庆");
  await expect(ownName.locator("mark")).toHaveCount(0);
});

// Clearly synthetic mocked raw source payloads, about 6000 characters together.
const RAW_MEMO = "Synthetic mocked ledger memo line. ".repeat(84);
const rawBefore = {
  Id: "MOCK-1001",
  DisplayName: "Harbor Freight",
  Active: true,
  Balance: 0,
  Notes: "",
  Fax: null,
  Taxable: false,
  Memo: "<script>window.__rawInjected = true</script>",
  Ledger: `${RAW_MEMO}before`,
};
const rawAfter = {
  ...rawBefore,
  DisplayName: "Harbor Freight Partners",
  Ledger: `${RAW_MEMO}after`,
  Suffix: '<img src="x" onerror="window.__rawInjected = true">',
};
const rawOnlyBefore = { Id: "MOCK-2002", Status: null, Count: 0 };
const rawOnlyAfter = { Id: "MOCK-2002", Status: "", Count: 0, Flag: false };

const priorWith = (hitId: string, what_changed: unknown) => ({
  id: `decision-old-${hitId}`,
  hit_id: `${hitId}-old`,
  kind: "false_positive",
  actor_user_id: "morgan-id",
  actor: { id: "morgan-id", email: "morgan@example.test", display_name: "Morgan Lee" },
  note: "Different registry number and city.",
  created_at: "2026-09-02T10:00:00Z",
  what_changed,
});

const rawHit = () =>
  makeHit("hit-raw", "Harbor Freight Partners", "Harbour Freight Partners LLC", "likely", 92, {
    prior_disposition: priorWith("hit-raw", {
      party: {
        name: { before: "Harbor Freight", after: "Harbor Freight Partners" },
        party_type: { before: "individual", after: "company" },
        country_iso2: { before: "BE", after: "NL" },
        address_lines: { before: ["12 Quay Street"], after: ["12 Quay Street", "Unit 2"] },
        raw: { before: rawBefore, after: rawAfter },
      },
      entry: {
        alt_names: { before: [], after: ["HFP Logistics"] },
        addresses: {
          before: [{ address: "1 Dock Road", city: "Antwerp" }],
          after: [{ address: "14 Quay Street", city: "Rotterdam" }],
        },
      },
    }),
    explanation: {
      components: { name: 88, country: 2, city: 3, address: 0, entity_type: 0, short_name: 0 },
      matched_tokens: { party: [], entry: [] },
      best_aliases: { party: "Harbor Freight Partners", entry: "Harbour Freight Partners LLC" },
      hash_rule_change: { previous: 3, current: 4, reason: "Synthetic mocked rule note." },
    },
  });
const rawOnlyHit = () =>
  makeHit("hit-raw-only", "Larkspur Components", "Larkspur Component Works", "likely", 91, {
    prior_disposition: priorWith("hit-raw-only", {
      party: { raw: { before: rawOnlyBefore, after: rawOnlyAfter } },
      entry: {},
    }),
  });

const earlierRegion = (page: Page) =>
  page.getByRole("region", { name: "Earlier decision on this pairing" });
const nextFrame = (page: Page) =>
  page.evaluate(
    () => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))),
  );
async function capture(page: Page, path: string) {
  await page.evaluate(() => window.scrollTo(0, 0));
  await nextFrame(page);
  await page.screenshot({ path, fullPage: true });
}
const fitsScreen = (page: Page) =>
  page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);

test("a long raw source diff stays in a closed disclosure while business changes and the real prior decision stay visible", async ({
  page,
}, testInfo) => {
  expect(JSON.stringify(rawBefore).length + JSON.stringify(rawAfter).length).toBeGreaterThan(6000);
  await page.setViewportSize({ width: 1440, height: 950 });
  const api = await mockReview(page, [rawHit(), rawOnlyHit()]);
  await page.goto("/queue");
  const earlier = earlierRegion(page);
  await expect(earlier).toContainText("Previously: Cleared as false positive");
  await expect(earlier).toContainText("Morgan Lee (morgan@example.test)");
  await expect(earlier).toContainText("Different registry number and city.");
  await expect(earlier.locator("time[datetime='2026-09-02T10:00:00.000Z']")).toBeVisible();
  await expect(earlier).toContainText("Matching rule changed from version 3 to 4.");

  const changes = earlier.locator(".rv-changes");
  await expect(changes.locator(":scope > div")).toHaveCount(6);
  await expect(changes).toContainText("Your record · Name");
  await expect(changes).toContainText("Your record · Party type");
  await expect(changes).toContainText("Your record · Country");
  await expect(changes).toContainText("BE");
  await expect(changes).toContainText("Your record · Address");
  await expect(changes).toContainText("12 Quay Street; Unit 2");
  await expect(changes).toContainText("Listing · Alternate names");
  await expect(changes).toContainText("Listing · Addresses");
  await expect(changes).toContainText("1 Dock Road");
  await expect(changes).not.toContainText("Raw");

  const raw = earlier.locator("details.rv-raw");
  await expect(raw).toHaveCount(1);
  await expect(raw).toHaveJSProperty("open", false);
  const summary = raw.locator(":scope > summary");
  await expect(summary).toContainText("Your record · Raw source data changed");
  await expect(summary).toContainText("3 top-level fields differ");
  const shown = await earlier.innerText();
  expect(shown).not.toContain("Synthetic mocked ledger memo");
  expect(shown.length).toBeLessThan(1500);

  await expect(clearButton(page)).toBeEnabled();
  await expect(page.getByLabel("Decision note")).toBeVisible();
  expect(api.posts).toHaveLength(0);
  await capture(page, testInfo.outputPath("mocked-review-raw-disclosure-desktop-1440x950.png"));
});

test("Enter and Space reveal exact escaped payloads without deciding; refetch keeps it open and J/K resets it", async ({
  page,
}) => {
  await page.clock.install();
  const api = await mockReview(page, [rawHit(), rawOnlyHit()]);
  await page.goto("/queue");
  const list = page.getByRole("navigation", { name: "Open items" });
  const harbor = list.getByRole("button", { name: /Harbor Freight Partners/ });
  const larkspur = list.getByRole("button", { name: /Larkspur Components/ });
  const earlier = earlierRegion(page);
  const raw = earlier.locator("details.rv-raw");
  const summary = raw.locator(":scope > summary");
  const blocks = raw.locator("pre.rv-json");
  await expect(raw).toHaveJSProperty("open", false);

  await summary.focus();
  await page.keyboard.press("Enter");
  await expect(raw).toHaveJSProperty("open", true);
  await expect(raw.getByRole("heading", { name: "Before" })).toBeVisible();
  await expect(raw.getByRole("heading", { name: "After" })).toBeVisible();
  await expect(blocks).toHaveCount(2);
  expect(await blocks.nth(0).textContent()).toBe(JSON.stringify(rawBefore, null, 2));
  expect(await blocks.nth(1).textContent()).toBe(JSON.stringify(rawAfter, null, 2));
  await expect(blocks.nth(0)).toContainText('"Fax": null');
  await expect(blocks.nth(0)).toContainText('"Notes": ""');
  await expect(blocks.nth(0)).toContainText('"Balance": 0');
  await expect(blocks.nth(0)).toContainText('"Taxable": false');
  await expect(blocks.nth(0)).toContainText("<script>window.__rawInjected = true</script>");
  await expect(blocks.nth(1)).toContainText(JSON.stringify(rawAfter.Suffix).slice(1, -1));
  await expect(raw.locator("script, img")).toHaveCount(0);
  expect(await page.evaluate(() => (window as { __rawInjected?: boolean }).__rawInjected)).toBeUndefined();

  await page.keyboard.press("Enter");
  await expect(raw).toHaveJSProperty("open", false);
  await page.keyboard.press(" ");
  await expect(raw).toHaveJSProperty("open", true);
  expect(await blocks.nth(1).textContent()).toBe(JSON.stringify(rawAfter, null, 2));
  await expect(summary).toBeFocused();
  await expect(harbor).toHaveAttribute("aria-current", "true");
  expect(api.posts).toHaveLength(0);

  // Read-only refetch of the same comparison through the existing query focus behavior.
  const hitReads = () => api.requests.filter((request) => request === "GET /hits/hit-raw").length;
  const reads = hitReads();
  await page.clock.fastForward(31_000);
  const refetched = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/v1/hits/hit-raw",
  );
  await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
  await refetched;
  expect(hitReads()).toBeGreaterThan(reads);
  await nextFrame(page);
  await expect(raw).toHaveJSProperty("open", true);
  expect(await blocks.nth(0).textContent()).toBe(JSON.stringify(rawBefore, null, 2));

  await page.keyboard.press("j");
  await expect(larkspur).toHaveAttribute("aria-current", "true");
  await expect(earlier).toContainText("Only the raw source data changed since that decision");
  await expect(earlier).not.toContainText("No field differences");
  await expect(earlier).toContainText("Morgan Lee (morgan@example.test)");
  await expect(raw).toHaveJSProperty("open", false);
  await expect(summary).toContainText("Your record · Raw source data changed");
  await expect(summary).toContainText("2 top-level fields differ");
  await summary.click();
  await expect(raw).toHaveJSProperty("open", true);
  expect(await blocks.nth(0).textContent()).toBe(JSON.stringify(rawOnlyBefore, null, 2));
  expect(await blocks.nth(1).textContent()).toBe(JSON.stringify(rawOnlyAfter, null, 2));

  // Back to a cached pair: same component position, but its disclosure starts closed.
  await page.keyboard.press("k");
  await expect(harbor).toHaveAttribute("aria-current", "true");
  await expect(earlier).toContainText("Your record · Party type");
  await expect(raw).toHaveJSProperty("open", false);
  expect(api.posts).toHaveLength(0);

  const note = page.getByLabel("Decision note");
  await note.fill("Synthetic raw change reviewed; j k f");
  await expect(harbor).toHaveAttribute("aria-current", "true");
  await page.getByRole("button", { name: /Escalate/ }).click();
  await expect.poll(() => api.posts.length).toBe(1);
  expect(api.posts[0]).toEqual({
    id: "hit-raw",
    body: { kind: "escalated", note: "Synthetic raw change reviewed; j k f" },
    csrf: reviewer.csrf_token,
  });
  await expect(page.getByText(/Recorded .Escalated. for Harbor Freight Partners/)).toBeVisible();
  await expect(larkspur).toHaveAttribute("aria-current", "true");
});

test("mocked 390px preview keeps the raw disclosure compact, bounded and on screen", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const api = await mockReview(page, [rawHit()]);
  await page.goto("/queue");
  const earlier = earlierRegion(page);
  const raw = earlier.locator("details.rv-raw");
  await expect(earlier).toContainText("Different registry number and city.");
  await expect(raw).toHaveJSProperty("open", false);
  await expect(clearButton(page)).toBeEnabled();
  expect(await fitsScreen(page)).toBe(true);
  await capture(page, testInfo.outputPath("mocked-review-raw-disclosure-mobile-390.png"));

  await raw.locator(":scope > summary").click();
  const before = raw.locator("pre.rv-json").first();
  await expect(before).toBeVisible();
  const box = await before.evaluate((element) => ({
    client: element.clientHeight,
    scroll: element.scrollHeight,
  }));
  expect(box.client).toBeLessThanOrEqual(400);
  expect(box.scroll).toBeGreaterThan(box.client);
  expect(await fitsScreen(page)).toBe(true);
  expect(api.posts).toHaveLength(0);
});
