import { expect, test, type Page, type Route } from "@playwright/test";

test.use({ locale: "en-US", timezoneId: "UTC" });

const setup = {
  needs_setup: false,
  product_name: "screening-tool",
  disclaimer:
    "The CSL is one tool offered to assist users in identifying potentially sanctioned companies, entities, or persons and is not a substitute for undertaking appropriate due diligence.",
  disclaimer_url: "https://developer.trade.gov/terms-of-service",
};
const admin = {
  user: {
    id: "admin-id",
    email: "admin@example.test",
    display_name: "Alex Admin",
    role: "admin",
    active: true,
  },
  csrf_token: "csl-csrf-token",
};
const reviewer = {
  user: {
    ...admin.user,
    id: "reviewer-id",
    email: "reviewer@example.test",
    display_name: "Riley Reviewer",
    role: "reviewer",
  },
  csrf_token: "reviewer-csrf-token",
};

function listStamps(imported: string) {
  return {
    SDN: {
      source: "Specially Designated Nationals (SDN) - Treasury Department",
      last_imported: imported,
      source_last_updated: "2026-09-30T16:12:00+00:00",
      import_rate: "Daily",
    },
    EL: {
      source: "Entity List (EL) - Bureau of Industry and Security",
      last_imported: imported,
      source_last_updated: "2026-09-28T13:00:00+00:00",
      import_rate: "Daily",
    },
    sources_api: {
      SDN: {
        source: "Specially Designated Nationals (SDN) - Treasury Department",
        last_imported: imported,
        source_last_updated: "2026-09-30T16:12:00+00:00",
        import_rate: "Hourly",
      },
    },
  };
}

const firstVersion = {
  id: "version-1",
  fetched_at: "2026-10-01T14:05:00Z",
  file_sha256: "a".repeat(64),
  semantic_sha256: "b".repeat(64),
  entry_count: 12408,
  added: 12408,
  changed: 0,
  removed: 0,
  source_list_stamps: listStamps("2026-10-01T09:00:00+00:00"),
};
const secondVersion = {
  id: "version-2",
  fetched_at: "2026-10-12T15:30:00Z",
  file_sha256: "c".repeat(64),
  semantic_sha256: "d".repeat(64),
  entry_count: 12415,
  added: 9,
  changed: 4,
  removed: 2,
  source_list_stamps: listStamps("2026-10-12T09:00:00+00:00"),
};

function fetchOf(
  version: typeof firstVersion,
  id: string,
  overrides: Record<string, unknown> = {},
) {
  return {
    id,
    fetched_at: version.fetched_at,
    completed_at: version.fetched_at,
    status: "success",
    version_id: version.id,
    file_sha256: version.file_sha256,
    error: null,
    source_list_stamps: version.source_list_stamps,
    warnings: [],
    ...overrides,
  };
}

type StatusFixture = {
  current_version: unknown;
  last_fetch: unknown;
  pending_screenings: number;
};
type CslHandler = (route: Route, path: string) => Promise<void>;

async function mockApi(page: Page, session: typeof admin, csl: CslHandler) {
  const cslRequests: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/^\/api\/v1/, "");
    if (path === "/setup") return route.fulfill({ json: setup });
    if (path === "/auth/me") return route.fulfill({ json: session });
    if (path.startsWith("/csl")) {
      cslRequests.push(`${request.method()} ${path}`);
      return csl(route, path);
    }
    return route.fulfill({ status: 404, json: { detail: "Not found" } });
  });
  return cslRequests;
}

const notFound = (route: Route) =>
  route.fulfill({ status: 404, json: { detail: "Not found" } });

const cslPanel = (page: Page) =>
  page.getByRole("region", {
    name: "Consolidated Screening List",
    exact: true,
  });

const fitsViewport = (page: Page) =>
  page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);

test("Admin fetches the first list version from Overview with the CSRF token and no body", async ({
  page,
}) => {
  let status: StatusFixture = {
    current_version: null,
    last_fetch: null,
    pending_screenings: 0,
  };
  let releaseStatus = () => {};
  const statusGate = new Promise<void>((resolve) => {
    releaseStatus = resolve;
  });
  let refreshToken: string | undefined;
  let refreshBody: string | null | undefined;
  await mockApi(page, admin, async (route, path) => {
    if (path === "/csl/status") {
      await statusGate;
      return route.fulfill({ json: status });
    }
    if (path === "/csl/refresh") {
      refreshToken = route.request().headers()["x-csrf-token"];
      refreshBody = route.request().postData();
      status = {
        current_version: firstVersion,
        last_fetch: fetchOf(firstVersion, "fetch-1"),
        pending_screenings: 0,
      };
      return route.fulfill({
        json: {
          ...status,
          fetch_id: "fetch-1",
          changed: true,
          pending_task_id: null,
          closed_hits: 0,
        },
      });
    }
    return notFound(route);
  });
  await page.goto("/");
  await expect(page.getByText("Checking setup progress…")).toBeVisible();
  await expect(page.getByText(/of 3 steps complete/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Fetch CSL now" })).toHaveCount(
    0,
  );
  releaseStatus();
  await expect(page.getByText("1 of 3 steps complete")).toBeVisible();
  await page.getByRole("button", { name: "Fetch CSL now" }).click();
  await expect(page.getByText("A new list version was saved.")).toBeVisible();
  await expect(page.getByText("2 of 3 steps complete")).toBeVisible();
  expect(refreshToken).toBe(admin.csrf_token);
  expect(refreshBody).toBeNull();
  await expect(
    page.getByText("Coming next", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /source|customer|vendor/i }),
  ).toHaveCount(0);
  await expect(page.getByText(/screened|screening complete/i)).toHaveCount(0);
});

test("an unchanged refresh says no version was saved and shows fresh stamps apart from the version stamps", async ({
  page,
}) => {
  let status: StatusFixture = {
    current_version: firstVersion,
    last_fetch: fetchOf(firstVersion, "fetch-1"),
    pending_screenings: 0,
  };
  const requests = await mockApi(page, admin, async (route, path) => {
    if (path === "/csl/status") return route.fulfill({ json: status });
    if (path === "/csl/versions")
      return route.fulfill({ json: [firstVersion] });
    if (path === "/csl/refresh") {
      status = {
        ...status,
        last_fetch: fetchOf(firstVersion, "fetch-2", {
          fetched_at: "2026-10-13T10:00:00Z",
          completed_at: "2026-10-13T10:01:00Z",
          source_list_stamps: listStamps("2026-10-13T09:00:00+00:00"),
          warnings: ["The EL source did not report an update time."],
        }),
      };
      return route.fulfill({
        json: {
          ...status,
          fetch_id: "fetch-2",
          changed: false,
          pending_task_id: null,
          closed_hits: 0,
        },
      });
    }
    return notFound(route);
  });
  await page.goto("/settings");
  const panel = cslPanel(page);
  const history = panel.getByRole("table", { name: /Saved versions/ });
  await expect(history.locator("tbody tr")).toHaveCount(1);
  await panel.getByRole("button", { name: "Refresh CSL" }).click();
  await expect(panel.getByText("The list is unchanged.")).toBeVisible();
  const latestStamps = panel.getByRole("table", {
    name: "Source list stamps reported by the latest fetch",
  });
  await expect(latestStamps).toContainText("2026-10-13T09:00:00+00:00");
  await expect(latestStamps).not.toContainText("2026-10-01T09:00:00+00:00");
  await expect(
    panel.getByRole("table", {
      name: "Search API source stamps reported by the latest fetch",
    }),
  ).toContainText("Hourly");
  await expect(
    panel.getByRole("region", { name: "Latest fetch attempt", exact: true }),
  ).toContainText("The EL source did not report an update time.");
  await panel.getByText("Show stamps recorded with the current version").click();
  await expect(
    panel.getByRole("table", {
      name: "Source list stamps recorded with the current version",
    }),
  ).toContainText("2026-10-01T09:00:00+00:00");
  await expect(history.locator("tbody tr")).toHaveCount(1);
  await expect
    .poll(() => requests.filter((r) => r === "GET /csl/versions").length)
    .toBeGreaterThanOrEqual(2);
});

test("a failed refresh keeps the prior version and shows the recorded failure", async ({
  page,
}) => {
  let status: StatusFixture = {
    current_version: firstVersion,
    last_fetch: fetchOf(firstVersion, "fetch-1"),
    pending_screenings: 0,
  };
  const requests = await mockApi(page, admin, async (route, path) => {
    if (path === "/csl/status") return route.fulfill({ json: status });
    if (path === "/csl/versions")
      return route.fulfill({ json: [firstVersion] });
    if (path === "/csl/refresh") {
      status = {
        ...status,
        last_fetch: {
          id: "fetch-2",
          fetched_at: "2026-10-13T10:00:00Z",
          completed_at: "2026-10-13T10:00:30Z",
          status: "failed",
          version_id: null,
          file_sha256: null,
          error: "Upstream responded with HTTP 503 Service Unavailable.",
          source_list_stamps: {},
          warnings: [],
        },
      };
      return route.fulfill({
        status: 502,
        json: { detail: "The screening list source did not respond." },
      });
    }
    return notFound(route);
  });
  await page.goto("/settings");
  const panel = cslPanel(page);
  await panel.getByRole("button", { name: "Refresh CSL" }).click();
  await expect(panel.getByRole("alert")).toContainText(
    "The screening list source did not respond.",
  );
  const latest = panel.getByRole("region", {
    name: "Latest fetch attempt",
    exact: true,
  });
  await expect(latest).toContainText("Failed");
  await expect(latest).toContainText(
    "Upstream responded with HTTP 503 Service Unavailable.",
  );
  const current = panel.getByRole("region", {
    name: "Current version",
    exact: true,
  });
  await expect(current).toContainText("12,408");
  await expect(current.getByText(firstVersion.file_sha256)).toBeVisible();
  await expect(panel.getByRole("button", { name: "Refresh CSL" })).toBeEnabled();
  expect(
    requests.filter((r) => r === "GET /csl/status").length,
  ).toBeGreaterThanOrEqual(2);
});

test("an unreadable status hides fetching until Try again loads the real state", async ({
  page,
}) => {
  let healthy = false;
  const requests = await mockApi(page, admin, async (route, path) => {
    if (path === "/csl/status")
      return healthy
        ? route.fulfill({
            json: {
              current_version: firstVersion,
              last_fetch: fetchOf(firstVersion, "fetch-1"),
              pending_screenings: 0,
            },
          })
        : route.fulfill({
            status: 500,
            json: { detail: "The status could not be read." },
          });
    return notFound(route);
  });
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText(
    "The status could not be read.",
  );
  await expect(page.getByRole("button", { name: "Fetch CSL now" })).toHaveCount(
    0,
  );
  await expect(page.getByText(/of 3 steps complete/)).toHaveCount(0);
  healthy = true;
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByText("2 of 3 steps complete")).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(requests.some((r) => r.startsWith("POST"))).toBe(false);
});

test("a Reviewer is kept out of list administration and never requests CSL data", async ({
  page,
}) => {
  const requests = await mockApi(page, reviewer, (route) =>
    route.fulfill({ status: 403, json: { detail: "Admin access required" } }),
  );
  await page.goto("/settings");
  await expect(page).toHaveURL("/");
  await expect(page.getByText("Your reviewer account is ready")).toBeVisible();
  await expect(
    page.getByText(/Contact your workspace administrator/),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Fetch CSL now|Refresh CSL/ }),
  ).toHaveCount(0);
  await expect(page.getByText(/of 3 steps complete/)).toHaveCount(0);
  expect(requests).toEqual([]);
});

test("a download started elsewhere disables refreshing and polls until it finishes", async ({
  page,
}) => {
  let finished = false;
  await mockApi(page, admin, async (route, path) => {
    if (path === "/csl/status")
      return route.fulfill({
        json: {
          current_version: firstVersion,
          last_fetch: finished
            ? fetchOf(firstVersion, "fetch-2")
            : fetchOf(firstVersion, "fetch-2", {
                status: "fetching",
                completed_at: null,
                version_id: null,
                file_sha256: null,
                source_list_stamps: {},
              }),
          pending_screenings: 0,
        },
      });
    if (path === "/csl/versions")
      return route.fulfill({ json: [firstVersion] });
    return notFound(route);
  });
  await page.goto("/settings");
  const panel = cslPanel(page);
  await expect(
    panel.getByRole("button", { name: "Fetching the list…" }),
  ).toBeDisabled();
  await expect(panel.getByText(/A list download is underway/)).toBeVisible();
  finished = true;
  await expect(panel.getByRole("button", { name: "Refresh CSL" })).toBeEnabled(
    { timeout: 10_000 },
  );
});

test("list details and the overview stay within a small screen", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page, admin, async (route, path) => {
    if (path === "/csl/status")
      return route.fulfill({
        json: {
          current_version: secondVersion,
          last_fetch: fetchOf(secondVersion, "fetch-3"),
          pending_screenings: 2,
        },
      });
    if (path === "/csl/versions")
      return route.fulfill({ json: [secondVersion, firstVersion] });
    return notFound(route);
  });
  await page.goto("/settings");
  const panel = cslPanel(page);
  await expect(
    panel.getByRole("table", { name: /Saved versions/ }).locator("tbody tr"),
  ).toHaveCount(2);
  await expect(
    panel
      .getByRole("region", { name: "Current version", exact: true })
      .getByText(secondVersion.semantic_sha256),
  ).toBeVisible();
  expect(await fitsViewport(page)).toBe(true);
  await page.goto("/");
  await expect(page.getByText("2 of 3 steps complete")).toBeVisible();
  await expect(
    page.getByText(/2 screenings are queued on the server/),
  ).toBeVisible();
  expect(await fitsViewport(page)).toBe(true);
});
