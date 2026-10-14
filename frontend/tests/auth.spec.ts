import { expect, test } from "@playwright/test";

const notice =
  "The CSL is one tool offered to assist users in identifying potentially sanctioned companies, entities, or persons and is not a substitute for undertaking appropriate due diligence.";
const setup = {
  needs_setup: true,
  product_name: "screening-tool",
  disclaimer: notice,
  disclaimer_url: "https://developer.trade.gov/terms-of-service",
};
const session = {
  user: {
    id: "admin-id",
    email: "admin@example.test",
    display_name: "Alex Reviewer",
    role: "admin",
    active: true,
  },
  csrf_token: "test-csrf-token",
};

test("first run gates protected pages and requires the supplied disclaimer before creating an Admin", async ({
  page,
}) => {
  let created = false;
  let loggedIn = false;
  let createBody: unknown;
  let logoutToken: string | undefined;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/setup") && request.method() === "GET")
      return route.fulfill({ json: { ...setup, needs_setup: !created } });
    if (path.endsWith("/setup") && request.method() === "POST") {
      createBody = request.postDataJSON();
      created = true;
      loggedIn = true;
      return route.fulfill({ status: 201, json: session });
    }
    if (path.endsWith("/auth/me"))
      return route.fulfill(
        loggedIn
          ? { json: session }
          : { status: 401, json: { detail: "Authentication required" } },
      );
    if (path.endsWith("/auth/logout")) {
      logoutToken = request.headers()["x-csrf-token"];
      loggedIn = false;
      return route.fulfill({ status: 204 });
    }
    return route.fulfill({ status: 404 });
  });
  await page.goto("/settings");
  await expect(
    page.getByRole("heading", { name: "Let’s get you set up." }),
  ).toBeVisible();
  await expect(
    page.getByRole("navigation", { name: "Main navigation" }),
  ).toHaveCount(0);
  await page.getByLabel("Full name").fill("Alex Reviewer");
  await page.getByLabel("Email address").fill("admin@example.test");
  await page
    .getByLabel("Password", { exact: true })
    .fill("a sufficiently long password");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText(notice, { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Create workspace" }),
  ).toBeDisabled();
  await page.getByRole("checkbox").focus();
  await page.keyboard.press("Space");
  await page.getByRole("button", { name: "Create workspace" }).click();
  await expect(
    page.getByRole("navigation", { name: "Main navigation" }),
  ).toBeVisible();
  expect(createBody).toEqual({
    email: "admin@example.test",
    display_name: "Alex Reviewer",
    password: "a sufficiently long password",
    disclaimer_acknowledged: true,
  });
  expect(await page.evaluate(() => Object.keys(localStorage))).toEqual([]);
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(
    page.getByRole("heading", { name: "Welcome back." }),
  ).toBeVisible();
  expect(logoutToken).toBe(session.csrf_token);
});

test("Reviewer does not receive Settings navigation and rejected sign-in displays a useful error", async ({
  page,
}) => {
  let loggedIn = false;
  let loginCount = 0;
  const reviewer = { ...session, user: { ...session.user, role: "reviewer" } };
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/setup"))
      return route.fulfill({ json: { ...setup, needs_setup: false } });
    if (path.endsWith("/auth/me"))
      return route.fulfill(
        loggedIn
          ? { json: reviewer }
          : { status: 401, json: { detail: "Authentication required" } },
      );
    if (path.endsWith("/auth/login")) {
      loginCount += 1;
      if (loginCount === 1)
        return route.fulfill({
          status: 401,
          json: { detail: "Email or password was not recognized" },
        });
      loggedIn = true;
      return route.fulfill({ json: reviewer });
    }
    return route.fulfill({ status: 404 });
  });
  await page.goto("/settings");
  await page.getByLabel("Email address").fill("reviewer@example.test");
  await page.getByLabel("Password", { exact: true }).fill("invalid password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toHaveText(
    "Email or password was not recognized",
  );
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(
    page.getByRole("navigation", { name: "Main navigation" }),
  ).toBeVisible();
  await expect(page).toHaveURL("/");
  await expect(
    page.getByRole("link", { name: "Settings", exact: true }),
  ).toHaveCount(0);
});

test("setup remains readable without horizontal scrolling on a small screen", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/api/v1/setup", (route) =>
    route.fulfill({ json: setup }),
  );
  await page.goto("/");
  await expect(page.getByLabel("Email address")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});
