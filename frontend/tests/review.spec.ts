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
