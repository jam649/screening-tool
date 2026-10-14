import { expect, test } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

test("fresh installation creates Admin, keeps secure session on reload, and supports logout and login", async ({
  page,
  request,
  context,
}) => {
  test.skip(
    !process.env.PLAYWRIGHT_BASE_URL,
    "Set PLAYWRIGHT_BASE_URL to an isolated fresh installation for the real API gate.",
  );
  const status = await request.get("/api/v1/setup");
  expect(status.ok()).toBeTruthy();
  const setup = await status.json();
  expect(
    setup.needs_setup,
    "This gate requires a fresh database with no existing users.",
  ).toBe(true);
  const email = `gate-${Date.now()}@example.com`;
  const password = "Synthetic browser gate password 2026!";
  const screenshots = join(homedir(), "Desktop/screenshots/screening-tool");
  await mkdir(screenshots, { recursive: true });
  await page.goto("/settings");
  await expect(
    page.getByRole("heading", { name: "Let’s get you set up." }),
  ).toBeVisible();
  await page.screenshot({
    path: join(screenshots, "milestone-1-setup.png"),
    fullPage: true,
  });
  await page.getByLabel("Full name").fill("Fixture Administrator");
  await page.getByLabel("Email address").fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText(setup.disclaimer, { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Create workspace" }),
  ).toBeDisabled();
  await page.getByRole("checkbox").focus();
  await page.keyboard.press("Space");
  await page.screenshot({
    path: join(screenshots, "milestone-1-disclaimer.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "Create workspace" }).click();
  await expect(
    page.getByRole("navigation", { name: "Main navigation" }),
  ).toBeVisible();
  const cookies = await context.cookies();
  const sessionCookie = cookies.find((cookie) => cookie.httpOnly);
  expect(sessionCookie).toBeDefined();
  expect(sessionCookie?.secure).toBe(true);
  expect(sessionCookie?.sameSite).toBe("Lax");
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Settings", exact: true }),
  ).toBeVisible();
  await page.getByRole("link", { name: "Overview", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "A clear place to begin." }),
  ).toBeVisible();
  await page.screenshot({
    path: join(screenshots, "milestone-1-overview.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(
    page.getByRole("heading", { name: "Welcome back." }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Welcome back." }),
  ).toBeVisible();
  await page.getByLabel("Email address").fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(
    page.getByRole("navigation", { name: "Main navigation" }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("navigation", { name: "Main navigation" }),
  ).toBeVisible();
});
