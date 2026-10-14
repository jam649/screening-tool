import { expect, test, type Page } from "@playwright/test";

// Synthetic, 43-character URL-safe token; never a real secret.
const TOKEN = "Synthetic-invite-token_0123456789abcdefghij";
const SETUP = {
  needs_setup: false,
  product_name: "Screening Tool",
  disclaimer: "Synthetic disclaimer text for tests.",
  disclaimer_url: "https://www.trade.gov/consolidated-screening-list",
};
type TestUser = {
  id: string;
  email: string;
  display_name: string;
  role: "admin" | "reviewer";
  active: boolean;
};
const ADMIN: TestUser = { id: "u-avery", email: "avery@example.test", display_name: "Avery Admin", role: "admin", active: true };
const OTHER_ADMIN: TestUser = { id: "u-sam", email: "sam@example.test", display_name: "Sam Admin", role: "admin", active: true };
const REVIEWER: TestUser = { id: "u-riley", email: "riley@example.test", display_name: "Riley Reviewer", role: "reviewer", active: true };
const NEW_USER: TestUser = { id: "u-morgan", email: "new@example.test", display_name: "Morgan Reviewer", role: "reviewer", active: true };
const sessionFor = (user: TestUser) => ({ user, csrf_token: `csrf-${user.id}` });
const WEIGHTS = {
  token_set: 0.5,
  token_sort: 0.25,
  partial: 0.25,
  country_match: 5,
  country_mismatch: -15,
  city_match: 5,
  address_max: 5,
  entity_mismatch: -10,
  short_name: -10,
};
const SETTINGS = {
  display_name: "Screening Tool",
  timezone: "UTC",
  retention_years: null as number | null,
  candidate_min: 50,
  review_threshold: 65,
  likely_threshold: 90,
  score_weights: WEIGHTS,
  legal_suffixes: ["inc", "llc", "ltd"],
  candidate_stopwords: ["the", "of", "and"],
  enabled_source_lists: null as string[] | null,
  disclaimer: SETUP.disclaimer,
  disclaimer_url: SETUP.disclaimer_url,
};
const INVITATION = {
  id: "inv-1",
  email: "new@example.test",
  role: "reviewer",
  expires_at: "2026-10-16T12:00:00Z",
  created_at: "2026-10-13T12:00:00Z",
  used_at: null,
  revoked_at: null,
};

type Call = { method: string; path: string; search: string; body: unknown; headers: Record<string, string> };
type Reply = { status?: number; body?: unknown } | undefined;
type MockOptions = { me: () => unknown; setup?: () => unknown; handle?: (call: Call) => Reply | void };

async function mockApi(page: Page, { me, setup = () => SETUP, handle }: MockOptions) {
  const calls: Call[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    let body: unknown = null;
    try {
      body = request.postDataJSON();
    } catch {
      body = request.postData();
    }
    const call: Call = { method: request.method(), path: url.pathname.replace(/^\/api\/v1/, ""), search: url.search, body, headers: request.headers() };
    calls.push(call);
    const user = me();
    const reply: NonNullable<Reply> =
      (handle?.(call) as Reply) ??
      (call.path === "/setup"
        ? { body: setup() }
        : call.path === "/auth/me"
          ? user
            ? { body: user }
            : { status: 401, body: { detail: "Not authenticated" } }
          : { status: 404, body: { detail: "Not available in this test" } });
    await route.fulfill({ status: reply.status ?? 200, contentType: "application/json", body: JSON.stringify(reply.body ?? null) });
  });
  return calls;
}

// Records the page URL at every fetch and stubs the clipboard for human-triggered copies.
async function watchBrowser(page: Page) {
  await page.addInitScript(() => {
    const scope = window as unknown as Record<string, unknown>;
    const log: { page: string; url: string }[] = [];
    scope.__fetchLog = log;
    const original = window.fetch.bind(window);
    window.fetch = (input, init) => {
      log.push({ page: window.location.href, url: input instanceof Request ? input.url : String(input) });
      return original(input, init);
    };
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (text: string) => void (scope.__copied = text) },
    });
  });
}
const browserValue = (page: Page, key: string) =>
  page.evaluate((name) => (window as unknown as Record<string, unknown>)[name], key);
const storedText = (page: Page) =>
  page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }));

test("Admin saves only changed general settings with CSRF and the brand refreshes", async ({ page }) => {
  const settings = structuredClone(SETTINGS);
  const calls = await mockApi(page, {
    me: () => sessionFor(ADMIN),
    setup: () => ({ ...SETUP, product_name: settings.display_name }),
    handle: ({ method, path, body }) => {
      if (path !== "/settings") return;
      if (method === "PATCH") Object.assign(settings, body);
      return { body: settings };
    },
  });
  await page.goto("/settings");
  const name = page.getByLabel("Workspace name");
  await expect(name).toHaveValue("Screening Tool");
  await name.fill("Northwind Screening");
  await page.getByRole("button", { name: "Save general settings" }).click();
  await expect(page.getByText("General settings saved.")).toBeVisible();

  const patches = calls.filter((call) => call.method === "PATCH");
  expect(patches).toHaveLength(1);
  expect(patches[0].body).toEqual({ display_name: "Northwind Screening" });
  expect(patches[0].headers["x-csrf-token"]).toBe("csrf-u-avery");
  await expect(page.locator(".sidebar .brand")).toContainText("Northwind Screening");
  await expect(page).toHaveTitle("Northwind Screening");
});

test("settings validate retention, thresholds and weights, and keep edits after a 422", async ({ page }) => {
  const settings = structuredClone(SETTINGS);
  const calls = await mockApi(page, {
    me: () => sessionFor(ADMIN),
    handle: ({ method, path, body }) => {
      if (path !== "/settings") return;
      if (method === "PATCH" && "score_weights" in (body as object))
        return { status: 422, body: { detail: "Synthetic rejection from the server" } };
      if (method === "PATCH") Object.assign(settings, body);
      return { body: settings };
    },
  });
  const patches = () => calls.filter((call) => call.method === "PATCH");
  await page.goto("/settings");

  await expect(page.getByLabel("Retention period (years)")).toBeDisabled();
  await page.getByLabel("Enable retention").check();
  await page.getByLabel("Retention period (years)").fill("3");
  await page.getByRole("button", { name: "Save general settings" }).click();
  await expect(page.getByText("Enter a whole number of years, 5 or more.")).toBeVisible();
  expect(patches()).toHaveLength(0);
  await page.getByLabel("Retention period (years)").fill("7");
  await page.getByRole("button", { name: "Save general settings" }).click();
  await expect(page.getByText("General settings saved.")).toBeVisible();
  expect(patches()[0].body).toEqual({ retention_years: 7 });

  const save = page.getByRole("button", { name: "Save screening settings" });
  await page.getByLabel("Review threshold").fill("95");
  await save.click();
  await expect(page.getByText(/higher than the likely match threshold/)).toBeVisible();
  await page.getByLabel("Review threshold").fill("70");
  await page.getByText("Advanced scoring").click();
  await page.getByLabel("Token set weight").fill("0.6");
  await save.click();
  await expect(page.getByText(/^The three name weights must add up to 1/)).toBeVisible();
  await page.getByLabel("Token set weight").fill("0.5");
  await page.getByLabel("Country mismatch", { exact: true }).fill("");
  await save.click();
  await expect(page.getByText("Enter a number for country mismatch.")).toBeVisible();
  expect(patches()).toHaveLength(1);

  await page.getByLabel("Country mismatch", { exact: true }).fill("-20");
  await save.click();
  await expect(page.getByRole("alert").filter({ hasText: "Synthetic rejection" })).toBeVisible();
  expect(patches()[1].body).toEqual({ review_threshold: 70, score_weights: { ...WEIGHTS, country_mismatch: -20 } });
  await expect(page.getByLabel("Review threshold")).toHaveValue("70");
  await expect(page.getByLabel("Country mismatch", { exact: true })).toHaveValue("-20");
});

test("users guard the last active Admin and surface a server 409", async ({ page }) => {
  const people = { items: [{ ...ADMIN }, { ...REVIEWER }], invitations: [] };
  const calls = await mockApi(page, {
    me: () => sessionFor(ADMIN),
    handle: ({ method, path, body }) => {
      if (path === "/users") return { body: people };
      if (method === "PATCH" && path === "/users/u-riley") {
        if ((body as { active?: boolean }).active === false)
          return { status: 409, body: { detail: "Keep at least one active Admin" } };
        Object.assign(people.items[1], body);
        return { body: people.items[1] };
      }
    },
  });
  await page.goto("/settings");
  await expect(page.getByRole("combobox", { name: "Role for avery@example.test" })).toBeDisabled();
  await expect(page.getByRole("checkbox", { name: "Active: avery@example.test" })).toBeDisabled();
  await expect(page.getByText(/Keep at least one active Admin\. Make someone else/)).toBeVisible();

  await page.getByRole("combobox", { name: "Role for riley@example.test" }).selectOption("admin");
  await page.getByRole("button", { name: "Save changes for riley@example.test" }).click();
  await expect(page.getByRole("combobox", { name: "Role for avery@example.test" })).toBeEnabled();
  expect(calls.find((call) => call.method === "PATCH")?.body).toEqual({ role: "admin" });

  const active = page.getByRole("checkbox", { name: "Active: riley@example.test" });
  await active.uncheck();
  await expect(page.getByText(/signs this person out of every session/)).toBeVisible();
  await page.getByRole("button", { name: "Save changes for riley@example.test" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Keep at least one active Admin" })).toBeVisible();
  await expect(active).not.toBeChecked();
  expect(calls.some((call) => call.method === "DELETE")).toBe(false);
});

test("changing your own role refreshes the session and removes Admin access", async ({ page }) => {
  let me = sessionFor(ADMIN);
  const people = { items: [{ ...ADMIN }, { ...OTHER_ADMIN }], invitations: [] };
  const calls = await mockApi(page, {
    me: () => me,
    handle: ({ method, path, body }) => {
      if (path === "/users") return { body: people };
      if (method === "PATCH" && path === "/users/u-avery") {
        Object.assign(people.items[0], body);
        me = sessionFor(people.items[0]);
        return { body: people.items[0] };
      }
    },
  });
  await page.goto("/settings");
  await page.getByRole("combobox", { name: "Role for avery@example.test" }).selectOption("reviewer");
  await expect(page.getByText(/You’re changing your own access/)).toBeVisible();
  await page.getByRole("button", { name: "Save changes for avery@example.test" }).click();
  await expect(page.getByRole("link", { name: "Settings" })).toHaveCount(0);
  await expect(page).toHaveURL(/\/$/);
  expect(calls.filter((call) => call.path === "/auth/me").length).toBeGreaterThan(1);
});

test("Reviewers get no Admin navigation and no Admin API calls", async ({ page }) => {
  const calls = await mockApi(page, { me: () => sessionFor(REVIEWER) });
  await page.goto("/settings");
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("link", { name: "Settings" })).toHaveCount(0);
  await page.waitForLoadState("networkidle");
  await expect(page.getByLabel("Workspace name")).toHaveCount(0);
  expect(calls.filter((call) => ["/settings", "/users"].includes(call.path))).toEqual([]);
});

test("manual invitation shows an in-memory link, copies on request and can be revoked", async ({ page }) => {
  await watchBrowser(page);
  const people = { items: [{ ...ADMIN }], invitations: [] as (typeof INVITATION)[] };
  const calls = await mockApi(page, {
    me: () => sessionFor(ADMIN),
    handle: ({ method, path, body }) => {
      if (path === "/users") return { body: people };
      if (method === "POST" && path === "/users/invitations") {
        people.invitations = [{ ...INVITATION, ...(body as object) }];
        return {
          status: 201,
          body: {
            ...people.invitations[0],
            invite_url: `/accept-invite#token=${TOKEN}`,
            delivery: "manual",
            delivery_pending: true,
            delivery_message: "Copy this invitation link for the recipient. SMTP is not configured.",
          },
        };
      }
      if (method === "POST" && path === "/users/invitations/inv-1/revoke") {
        const revoked = { ...people.invitations[0], revoked_at: "2026-10-13T13:00:00Z" };
        people.invitations = [];
        return { body: revoked };
      }
    },
  });
  await page.goto("/settings");
  await page.getByLabel("Email address").fill("new@example.test");
  await page.getByLabel("Role", { exact: true }).selectOption("reviewer");
  await page.getByRole("button", { name: "Create invitation link" }).click();

  const link = `${new URL(page.url()).origin}/accept-invite#token=${TOKEN}`;
  await expect(page.getByRole("textbox", { name: "Invitation link", exact: true })).toHaveValue(link);
  await expect(page.getByText("SMTP is not configured.")).toBeVisible();
  await expect(page.getByText(/email (was )?sent/i)).toHaveCount(0);
  const post = calls.find((call) => call.path === "/users/invitations");
  expect(post?.body).toEqual({ email: "new@example.test", role: "reviewer" });
  expect(post?.headers["x-csrf-token"]).toBe("csrf-u-avery");

  expect(await browserValue(page, "__copied")).toBeUndefined();
  await page.getByRole("button", { name: "Copy link" }).click();
  await expect(page.getByText("Link copied to the clipboard.")).toBeVisible();
  expect(await browserValue(page, "__copied")).toBe(link);
  expect(await storedText(page)).not.toContain(TOKEN);

  await expect(page.getByRole("cell", { name: "new@example.test", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Done" }).click();
  await expect(page.getByRole("textbox", { name: "Invitation link", exact: true })).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText(TOKEN);

  await page.getByRole("button", { name: "Revoke invitation for new@example.test" }).click();
  await expect(page.getByText("No pending invitations.")).toBeVisible();
  expect(calls.some((call) => call.path === "/users/invitations/inv-1/revoke" && call.method === "POST")).toBe(true);
});

test("anonymous acceptance strips the fragment before any request and opens the workspace", async ({ page }) => {
  await watchBrowser(page);
  let me: unknown = null;
  const calls = await mockApi(page, {
    me: () => me,
    handle: ({ method, path }) => {
      if (method === "POST" && path === "/auth/invite/validate")
        return { body: { email: NEW_USER.email, role: NEW_USER.role, expires_at: INVITATION.expires_at } };
      if (method === "POST" && path === "/auth/invite/accept") {
        me = sessionFor(NEW_USER);
        return { status: 201, body: me };
      }
    },
  });
  await page.goto(`/accept-invite#token=${TOKEN}`);
  await expect(page.getByText("new@example.test")).toBeVisible();
  await expect(page).toHaveURL(/\/accept-invite$/);

  const log = (await browserValue(page, "__fetchLog")) as { page: string; url: string }[];
  expect(log.length).toBeGreaterThan(0);
  for (const entry of log) {
    expect(entry.page).not.toContain(TOKEN);
    expect(entry.url).not.toContain(TOKEN);
  }
  const validations = calls.filter((call) => call.path === "/auth/invite/validate");
  expect(validations).toHaveLength(1);
  expect(validations[0].body).toEqual({ token: TOKEN });

  await page.getByLabel("Your name").fill("Morgan Reviewer");
  await page.getByLabel("Password", { exact: true }).fill("synthetic-passphrase-42");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.locator(".user-menu")).toContainText("Morgan Reviewer");

  const accepts = calls.filter((call) => call.path === "/auth/invite/accept");
  expect(accepts).toHaveLength(1);
  expect(accepts[0].body).toEqual({ token: TOKEN, display_name: "Morgan Reviewer", password: "synthetic-passphrase-42" });
  expect(calls.filter((call) => (call.path + call.search).includes(TOKEN))).toEqual([]);
  expect(await storedText(page)).not.toContain(TOKEN);
});

test("malformed, expired and already-used invitations show errors without retries", async ({ page }) => {
  let mode: "expired" | "used" = "expired";
  const detail = "This invitation is invalid or expired; request another invitation";
  const calls = await mockApi(page, {
    me: () => null,
    handle: ({ method, path }) => {
      if (method === "POST" && path === "/auth/invite/validate")
        return mode === "expired"
          ? { status: 400, body: { detail } }
          : { body: { email: NEW_USER.email, role: NEW_USER.role, expires_at: INVITATION.expires_at } };
      if (method === "POST" && path === "/auth/invite/accept") return { status: 400, body: { detail } };
    },
  });
  const count = (path: string) => calls.filter((call) => call.path === path).length;

  await page.goto("/accept-invite#token=not-a-real-token");
  await expect(page.getByRole("heading", { name: /link isn.t valid/ })).toBeVisible();
  await expect(page).toHaveURL(/\/accept-invite$/);
  expect(count("/auth/invite/validate")).toBe(0);

  await page.goto("about:blank");
  await page.goto(`/accept-invite#token=${TOKEN}`);
  await expect(page.getByRole("alert").filter({ hasText: "invalid or expired" })).toBeVisible();
  await expect(page.getByLabel("Password", { exact: true })).toHaveCount(0);
  expect(count("/auth/invite/validate")).toBe(1);

  mode = "used";
  await page.goto("about:blank");
  await page.goto(`/accept-invite#token=${TOKEN}`);
  await page.getByLabel("Your name").fill("Morgan Reviewer");
  await page.getByLabel("Password", { exact: true }).fill("synthetic-passphrase-42");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "invalid or expired" })).toBeVisible();
  await expect(page.getByLabel("Password", { exact: true })).toHaveCount(0);
  expect(count("/auth/invite/accept")).toBe(1);
  expect(count("/auth/invite/validate")).toBe(2);
});
