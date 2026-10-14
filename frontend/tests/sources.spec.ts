import {
  expect,
  test,
  type Locator,
  type Page,
  type Request,
  type Route,
} from "@playwright/test";

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
  csrf_token: "sources-csrf-token",
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

const STABLE_ID_WARNING =
  "Map a stable customer or vendor ID to keep changes in the same party file. " +
  "Without an ID, changed screening fields create a new party file; previous history is retained.";
const CONFIRM = "I confirm this file is the complete source snapshot";

type Mapping = Record<string, string | string[]>;
type SourceFixture = {
  id: string;
  kind: string;
  label: string;
  record_count: number;
  last_synced_at: string | null;
  next_sync_at: string | null;
  health: "not_synced" | "healthy" | "stale" | "error";
  last_error: string | null;
  column_mapping: Mapping;
};

function sourceOf(overrides: Partial<SourceFixture> = {}): SourceFixture {
  return {
    id: "source-1",
    kind: "csv",
    label: "Customer ledger",
    record_count: 0,
    last_synced_at: null,
    next_sync_at: null,
    health: "not_synced",
    last_error: null,
    column_mapping: {},
    ...overrides,
  };
}

const savedMapping: Mapping = {
  name: "Legal Name",
  external_id: "Customer ID",
  address_lines: ["Address 2", "Address 1"],
  alt_names: ["Trade Name"],
  city: "City",
  country: "Former Country",
};
const importedSource = sourceOf({
  record_count: 3,
  last_synced_at: "2026-10-12T15:30:00Z",
  health: "healthy",
  column_mapping: savedMapping,
});

// Synthetic rows only; the second name checks that raw cells are escaped.
const csvText =
  [
    "Customer ID,Legal Name,Party Type,Address 1,Address 2,City,Country,Trade Name",
    "C-100,Northwind Test Traders,company,1 Harbor Way,Suite 4,Portland,US,Northwind",
    "C-101,<b>Avery Example</b>,individual,22 Elm Street,,Leeds,GB,",
  ].join("\r\n") + "\r\n";

function previewOf(text: string, columnMapping: Mapping = {}) {
  const [head, ...lines] = text.trim().split(/\r?\n/);
  const headers = head.split(",");
  const rows = lines.map((line) => {
    const cells = line.split(",");
    return Object.fromEntries(
      headers.map((header, index) => [header, cells[index] ?? ""]),
    );
  });
  return {
    headers,
    rows: rows.slice(0, 20),
    total_rows: rows.length,
    stable_id_warning: STABLE_ID_WARNING,
    column_mapping: columnMapping,
    source_id: "source-1",
  };
}

const csvFile = (name = "customers.csv", text = csvText) => ({
  name,
  mimeType: "text/csv",
  buffer: Buffer.from(text),
});

function uploadResult(overrides: Record<string, unknown> = {}) {
  return {
    source_id: "source-1",
    added: 0,
    changed: 0,
    unchanged: 0,
    deleted: 0,
    rejected: [],
    rejected_count: 0,
    omitted_rejection_details: 0,
    task_ids: [],
    applied: true,
    source: importedSource,
    screening_run_ids: [],
    ...overrides,
  };
}

type Part = {
  name: string;
  filename?: string;
  contentType?: string;
  value: string;
};

// Reads the browser's real multipart body so the boundary and parts are verified.
function parseMultipart(request: Request) {
  const contentType = request.headers()["content-type"] ?? "";
  const boundary = /^multipart\/form-data; boundary=(.+)$/.exec(contentType)?.[1];
  if (!boundary)
    throw new Error(`Expected a multipart body, got "${contentType}"`);
  const body = request.postDataBuffer()?.toString("utf8") ?? "";
  const parts: Part[] = body
    .split(`--${boundary}`)
    .slice(1, -1)
    .map((chunk) => {
      const content = chunk.replace(/^\r\n/, "").replace(/\r\n$/, "");
      const split = content.indexOf("\r\n\r\n");
      const head = content.slice(0, split);
      const disposition = /name="([^"]*)"(?:; filename="([^"]*)")?/.exec(head);
      return {
        name: disposition?.[1] ?? "",
        filename: disposition?.[2],
        contentType: /^content-type: *([^\r\n]+)/im.exec(head)?.[1],
        value: content.slice(split + 4),
      };
    });
  return { boundary, contentType, parts };
}

type Handlers = Record<string, (route: Route) => Promise<void> | void>;

async function mockApi(page: Page, session: typeof admin, handlers: Handlers) {
  const calls: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/^\/api\/v1/, "");
    if (route.request().method() === "GET" && (path === "/notifications/status" || path === "/api/v1/notifications/status")) return route.fulfill({ json: { batches: [], health: [] } });
    if (route.request().method() === "GET" && (path === "/monitoring/status" || path === "/api/v1/monitoring/status")) return route.fulfill({ json: { enabled: true, schedules: [], operations: [], health: [] } });
    if (route.request().method() === "GET" && (path === "/settings/monitoring" || path === "/api/v1/settings/monitoring")) return route.fulfill({ json: { csl_fetch_time: "05:00", source_poll_minutes: 15, retention_weekday: "sun", retention_time: "03:00" } });
    if (path === "/dashboard") return route.fulfill({ json: { total_parties: 0, csl_version: null } });
    if (path === "/setup") return route.fulfill({ json: setup });
    if (path === "/auth/me") return route.fulfill({ json: session });
    const key = `${request.method()} ${path}`;
    calls.push(key);
    const handler = handlers[key];
    if (handler) return handler(route);
    return route.fulfill({ status: 404, json: { detail: "Not found" } });
  });
  return calls;
}

const sourceCard = (scope: Page | Locator, label: string) =>
  scope.getByRole("article", { name: label });
const wizardFor = (page: Page, label: string) =>
  page.getByRole("region", { name: `Upload a file to ${label}` });
const fitsViewport = (page: Page) =>
  page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);

async function previewFile(page: Page, label: string, file = csvFile()) {
  await sourceCard(page, label)
    .getByRole("button", { name: "Upload CSV or XLSX" })
    .click();
  const wizard = wizardFor(page, label);
  await wizard.getByLabel("File to import").setInputFiles(file);
  await wizard.getByRole("button", { name: "Preview file" }).click();
  await expect(wizard.getByRole("table", { name: /^Preview of/ })).toBeVisible();
  return wizard;
}

async function importPreviewed(wizard: Locator) {
  await wizard.getByLabel(CONFIRM).check();
  await wizard.getByRole("button", { name: "Import file" }).click();
}

test("an Admin adds a CSV source with JSON and CSRF, then previews the file as real multipart", async ({
  page,
}) => {
  const sources: SourceFixture[] = [];
  let createRequest: Request | undefined;
  let previewRequest: Request | undefined;
  const calls = await mockApi(page, admin, {
    "GET /sources": (route) => route.fulfill({ json: sources }),
    "POST /sources": (route) => {
      createRequest = route.request();
      const created = sourceOf();
      sources.push(created);
      return route.fulfill({ status: 201, json: created });
    },
    "POST /sources/preview": (route) => {
      previewRequest = route.request();
      return route.fulfill({ json: previewOf(csvText) });
    },
  });
  await page.goto("/sources");
  await expect(
    page.getByRole("heading", { name: "Sources", level: 1 }),
  ).toBeVisible();
  await expect(page.getByText("No sources yet.")).toBeVisible();
  await page.getByLabel("Source name").fill("  Customer ledger ");
  await page.getByRole("button", { name: "Add CSV source" }).click();

  const wizard = wizardFor(page, "Customer ledger");
  await expect(wizard).toBeVisible();
  expect(createRequest?.headers()["content-type"]).toBe("application/json");
  expect(createRequest?.headers()["x-csrf-token"]).toBe(admin.csrf_token);
  expect(createRequest?.postDataJSON()).toEqual({
    kind: "csv",
    label: "Customer ledger",
  });
  const importButton = wizard.getByRole("button", { name: "Import file" });
  await expect(importButton).toBeDisabled();

  await wizard.getByLabel("File to import").setInputFiles(csvFile());
  await wizard.getByRole("button", { name: "Preview file" }).click();
  const table = wizard.getByRole("table", {
    name: "Preview of customers.csv: first 2 of 2 rows",
  });
  await expect(table.locator("tbody tr")).toHaveCount(2);
  await expect(
    table.getByText("<b>Avery Example</b>", { exact: true }),
  ).toBeVisible();

  const { boundary, contentType, parts } = parseMultipart(previewRequest!);
  expect(contentType).toBe(`multipart/form-data; boundary=${boundary}`);
  expect(previewRequest!.headers()["x-csrf-token"]).toBe(admin.csrf_token);
  expect(parts.map((part) => part.name)).toEqual(["file", "source_id"]);
  expect(parts[0]).toMatchObject({
    filename: "customers.csv",
    contentType: "text/csv",
    value: csvText,
  });
  expect(parts[1]).toMatchObject({ value: "source-1" });
  expect(parts[1].filename).toBeUndefined();

  const mapping = wizard.getByRole("group", { name: "Map columns" });
  const select = (label: string) => mapping.getByLabel(label, { exact: true });
  await expect(select("Name column")).toHaveValue("Legal Name");
  await expect(select("External ID column")).toHaveValue("Customer ID");
  await expect(select("Party type column")).toHaveValue("Party Type");
  await expect(select("Role column")).toHaveValue("");
  await expect(select("City column")).toHaveValue("City");
  await expect(select("Country column")).toHaveValue("Country");
  const addresses = mapping.getByRole("group", {
    name: "Address line columns",
  });
  await expect(
    addresses.getByRole("checkbox", { name: "Address 1", exact: true }),
  ).toBeChecked();
  await expect(
    addresses.getByRole("checkbox", { name: "Address 2", exact: true }),
  ).toBeChecked();
  await expect(
    addresses.getByRole("checkbox", { name: "City", exact: true }),
  ).not.toBeChecked();
  await expect(wizard.getByText(STABLE_ID_WARNING, { exact: true })).toBeVisible();

  await select("Name column").selectOption("");
  await wizard.getByLabel(CONFIRM).check();
  await expect(importButton).toBeDisabled();
  await expect(wizard.getByText("Choose a name column to continue.")).toBeVisible();
  await select("Name column").selectOption("Legal Name");
  await expect(importButton).toBeEnabled();
  await wizard.getByLabel(CONFIRM).uncheck();
  await expect(importButton).toBeDisabled();

  await wizard.getByRole("button", { name: "Cancel" }).click();
  await expect(sourceCard(page, "Customer ledger")).toContainText(
    "Not yet imported",
  );
  expect(calls.some((call) => call.endsWith("/upload"))).toBe(false);
});

test("a saved mapping keeps its arrays, and an applied import sends multipart mapping JSON and updates the card", async ({
  page,
}) => {
  let current = importedSource;
  let uploadRequest: Request | undefined;
  let listGate: Promise<void> | undefined;
  let releaseList = () => {};
  await mockApi(page, admin, {
    "GET /sources": async (route) => {
      if (listGate) await listGate;
      return route.fulfill({ json: [current] });
    },
    "POST /sources/preview": (route) =>
      route.fulfill({ json: previewOf(csvText, savedMapping) }),
    "POST /sources/source-1/upload": (route) => {
      uploadRequest = route.request();
      // Hold the list refetch so the card must update from the response.
      listGate = new Promise((resolve) => {
        releaseList = resolve;
      });
      current = {
        ...importedSource,
        record_count: 8,
        last_synced_at: "2026-10-13T09:00:00Z",
      };
      return route.fulfill({
        json: uploadResult({
          added: 2,
          changed: 1,
          unchanged: 5,
          deleted: 1,
          task_ids: ["task-1"],
          screening_run_ids: ["run-1"],
          source: current,
        }),
      });
    },
  });
  await page.goto("/sources");
  await expect(
    sourceCard(page, "Customer ledger").getByText("3", { exact: true }),
  ).toBeVisible();
  const wizard = await previewFile(page, "Customer ledger");
  const mapping = wizard.getByRole("group", { name: "Map columns" });
  await expect(mapping.getByLabel("Name column", { exact: true })).toHaveValue(
    "Legal Name",
  );
  // The saved country column is missing from this file, so a guess replaces it.
  await expect(
    mapping.getByLabel("Country column", { exact: true }),
  ).toHaveValue("Country");
  await expect(
    mapping.getByLabel("Party type column", { exact: true }),
  ).toHaveValue("Party Type");
  await expect(
    mapping.getByText("Combined in this order: Address 2, Address 1"),
  ).toBeVisible();
  await expect(
    mapping
      .getByRole("group", { name: "Alternate name columns" })
      .getByRole("checkbox", { name: "Trade Name", exact: true }),
  ).toBeChecked();

  await importPreviewed(wizard);
  await expect(wizard.getByText("The import was applied.")).toBeVisible();

  const { boundary, contentType, parts } = parseMultipart(uploadRequest!);
  expect(contentType).toBe(`multipart/form-data; boundary=${boundary}`);
  expect(uploadRequest!.headers()["x-csrf-token"]).toBe(admin.csrf_token);
  expect(parts.map((part) => part.name)).toEqual(["file", "mapping"]);
  expect(parts[0]).toMatchObject({
    filename: "customers.csv",
    contentType: "text/csv",
    value: csvText,
  });
  expect(parts[1].filename).toBeUndefined();
  expect(JSON.parse(parts[1].value)).toEqual({
    name: "Legal Name",
    external_id: "Customer ID",
    party_type: "Party Type",
    city: "City",
    country: "Country",
    address_lines: ["Address 2", "Address 1"],
    alt_names: ["Trade Name"],
  });

  const status = wizard.getByRole("status");
  await expect(status).toContainText("2 added");
  await expect(status).toContainText("1 changed");
  await expect(status).toContainText("5 unchanged");
  await expect(status).toContainText("1 deleted");
  await expect(status).toContainText("Screening recorded 1 run for new and changed parties.");
  await expect(page.getByText(/screening complete/i)).toHaveCount(0);
  await expect(
    sourceCard(wizard, "Customer ledger").getByText("8", { exact: true }),
  ).toBeVisible();
  await expect(wizard.getByRole("button", { name: "Import file" })).toHaveCount(
    0,
  );

  await wizard.getByRole("button", { name: "Back to sources" }).click();
  const card = sourceCard(page, "Customer ledger");
  await expect(card.getByText("8", { exact: true })).toBeVisible();
  await expect(card).toContainText("Healthy");
  releaseList();
});

test("a rejected import changes no parties, lists the returned rejections, and keeps the mapping", async ({
  page,
}) => {
  const rejectedSource: SourceFixture = {
    ...importedSource,
    health: "error",
    last_error: "Import rejected: 507 invalid rows",
  };
  let current = importedSource;
  await mockApi(page, admin, {
    "GET /sources": (route) => route.fulfill({ json: [current] }),
    "POST /sources/preview": (route) =>
      route.fulfill({ json: previewOf(csvText, savedMapping) }),
    "POST /sources/source-1/upload": (route) => {
      current = rejectedSource;
      return route.fulfill({
        json: uploadResult({
          applied: false,
          rejected: Array.from({ length: 500 }, (_, index) => ({
            row_number: index + 2,
            reason: index === 0
              ? "Party type must be company or individual"
              : index === 1
                ? "Role must be customer, vendor, or both"
                : "Country must be a recognized country name or code",
          })),
          rejected_count: 507,
          omitted_rejection_details: 7,
          source: rejectedSource,
        }),
      });
    },
  });
  await page.goto("/sources");
  const wizard = await previewFile(page, "Customer ledger");
  await importPreviewed(wizard);

  const alert = wizard.getByRole("alert");
  await expect(alert).toContainText("Nothing was imported.");
  await expect(alert).toContainText("507 rows were rejected");
  const rejections = wizard.getByRole("table", {
    name: "Rejected rows: 500 listed of 507",
  });
  await expect(rejections.locator("tbody tr")).toHaveCount(500);
  await expect(rejections).toContainText(
    "Party type must be company or individual",
  );
  await expect(
    wizard.getByText("7 more rejected rows are not listed", { exact: false }),
  ).toBeVisible();
  await expect(wizard.getByText(/import was applied/i)).toHaveCount(0);
  await expect(wizard.locator(".badge.success")).toHaveCount(0);

  const card = sourceCard(wizard, "Customer ledger");
  await expect(card).toContainText("Needs attention");
  await expect(card).toContainText("Import rejected: 507 invalid rows");
  await expect(card.getByText("3", { exact: true })).toBeVisible();
  await expect(
    wizard
      .getByRole("group", { name: "Map columns" })
      .getByLabel("Name column", { exact: true }),
  ).toHaveValue("Legal Name");
  await expect(wizard.getByLabel(CONFIRM)).toBeChecked();
  await expect(
    wizard.getByRole("button", { name: "Import file" }),
  ).toBeEnabled();
});

test("a 422 upload shows the server detail and keeps the earlier record count", async ({
  page,
}) => {
  await mockApi(page, admin, {
    "GET /sources": (route) => route.fulfill({ json: [importedSource] }),
    "POST /sources/preview": (route) =>
      route.fulfill({ json: previewOf(csvText, savedMapping) }),
    "POST /sources/source-1/upload": (route) =>
      route.fulfill({
        status: 422,
        json: { detail: "Mapped columns must exist in the upload" },
      }),
  });
  await page.goto("/sources");
  const wizard = await previewFile(page, "Customer ledger");
  await importPreviewed(wizard);
  const alert = wizard.getByRole("alert");
  await expect(alert).toContainText("The file was not imported.");
  await expect(alert).toContainText("Mapped columns must exist in the upload");
  await expect(alert).toContainText(
    "Earlier records for this source are unchanged.",
  );
  await expect(wizard.getByText(/import was applied/i)).toHaveCount(0);
  await expect(
    wizard.getByRole("button", { name: "Import file" }),
  ).toBeEnabled();
  await wizard.getByRole("button", { name: "Cancel" }).click();
  const card = sourceCard(page, "Customer ledger");
  await expect(card.getByText("3", { exact: true })).toBeVisible();
  await expect(card).toContainText("Healthy");
});

test("changing the file clears the stale preview, and a failed preview leaves none behind", async ({
  page,
}) => {
  const previews: string[] = [];
  const calls = await mockApi(page, admin, {
    "GET /sources": (route) => route.fulfill({ json: [importedSource] }),
    "POST /sources/preview": (route) => {
      const file = parseMultipart(route.request()).parts.find(
        (part) => part.name === "file",
      );
      previews.push(file?.filename ?? "");
      return file?.filename === "broken.csv"
        ? route.fulfill({
            status: 422,
            json: { detail: "Column headings must be nonempty and unique" },
          })
        : route.fulfill({ json: previewOf(csvText, savedMapping) });
    },
  });
  await page.goto("/sources");
  const wizard = await previewFile(page, "Customer ledger");
  const importButton = wizard.getByRole("button", { name: "Import file" });
  await wizard.getByLabel(CONFIRM).check();
  await expect(importButton).toBeEnabled();

  await wizard
    .getByLabel("File to import")
    .setInputFiles(csvFile("broken.csv", "Name,Name\nA,B\n"));
  await expect(wizard.getByRole("table")).toHaveCount(0);
  await expect(wizard.getByRole("group", { name: "Map columns" })).toHaveCount(
    0,
  );
  await expect(wizard.getByLabel(CONFIRM)).toHaveCount(0);
  await expect(importButton).toBeDisabled();
  expect(previews).toEqual(["customers.csv"]);

  await wizard.getByRole("button", { name: "Preview file" }).click();
  await expect(wizard.getByRole("alert")).toContainText(
    "Column headings must be nonempty and unique",
  );
  await expect(wizard.getByRole("table")).toHaveCount(0);
  await expect(importButton).toBeDisabled();
  expect(previews).toEqual(["customers.csv", "broken.csv"]);
  expect(calls.some((call) => call.endsWith("/upload"))).toBe(false);
});

test("unsupported, empty, and oversized files are blocked before any request", async ({
  page,
}) => {
  const calls = await mockApi(page, admin, {
    "GET /sources": (route) => route.fulfill({ json: [importedSource] }),
  });
  await page.goto("/sources");
  await sourceCard(page, "Customer ledger")
    .getByRole("button", { name: "Upload CSV or XLSX" })
    .click();
  const wizard = wizardFor(page, "Customer ledger");
  const input = wizard.getByLabel("File to import");
  const previewButton = wizard.getByRole("button", { name: "Preview file" });
  await expect(wizard.getByText("CSV / XLSX, up to 10 MiB")).toBeVisible();
  await expect(input).toHaveAttribute("accept", ".csv,.xlsx");

  await input.setInputFiles({
    name: "notes.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("Name\nA\n"),
  });
  await expect(wizard.getByRole("alert")).toHaveText(
    "Choose a .csv or .xlsx file.",
  );
  await expect(previewButton).toBeDisabled();

  await input.setInputFiles({
    name: "empty.csv",
    mimeType: "text/csv",
    buffer: Buffer.alloc(0),
  });
  await expect(wizard.getByRole("alert")).toContainText("This file is empty.");
  await expect(previewButton).toBeDisabled();

  await input.setInputFiles({
    name: "large.xlsx",
    mimeType:
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    buffer: Buffer.alloc(10 * 1024 * 1024 + 1),
  });
  await expect(wizard.getByRole("alert")).toContainText("larger than 10 MiB");
  await expect(previewButton).toBeDisabled();

  await input.setInputFiles(csvFile());
  await expect(wizard.getByRole("alert")).toHaveCount(0);
  await expect(previewButton).toBeEnabled();
  expect(calls.filter((call) => call.startsWith("POST"))).toEqual([]);
});

test("the synthetic sample download is a same-origin GET link for Admins", async ({
  page,
}) => {
  const calls = await mockApi(page, admin, {
    "GET /sources": (route) => route.fulfill({ json: [] }),
    "GET /sources/demo.csv": (route) =>
      route.fulfill({
        headers: {
          "content-type": "text/csv",
          "content-disposition":
            'attachment; filename="screening-tool-demo-parties.csv"',
        },
        body: "name\nSynthetic Example Co\n",
      }),
  });
  await page.goto("/sources");
  const link = page.getByRole("link", { name: "Download synthetic sample CSV" });
  await expect(link).toHaveAttribute("href", "/api/v1/sources/demo.csv");
  await expect(link).toHaveAttribute("download", "");
  await expect(
    page.getByText(/invented parties for testing, not customer data/),
  ).toBeVisible();
  const response = await link.evaluate(async (anchor) => {
    const result = await fetch((anchor as HTMLAnchorElement).href, {
      credentials: "same-origin",
    });
    return {
      status: result.status,
      disposition: result.headers.get("content-disposition"),
    };
  });
  expect(response).toEqual({
    status: 200,
    disposition: 'attachment; filename="screening-tool-demo-parties.csv"',
  });
  expect(calls).toContain("GET /sources/demo.csv");
});

test("a Reviewer reads source health from Overview without upload, add, or sample controls", async ({
  page,
}) => {
  const failing = sourceOf({
    id: "source-2",
    label: "Vendor master",
    health: "error",
    last_error: "Import rejected: 2 invalid rows",
    record_count: 14,
    last_synced_at: "2026-10-10T08:00:00Z",
  });
  const calls = await mockApi(page, reviewer, {
    "GET /sources": (route) =>
      route.fulfill({ json: [importedSource, failing] }),
  });
  await page.goto("/");
  await page
    .getByRole("main")
    .getByRole("link", { name: "Sources", exact: true })
    .click();
  await expect(page).toHaveURL("/sources");
  await expect(
    page.getByRole("heading", { name: "Sources", level: 1 }),
  ).toBeVisible();
  const ledger = sourceCard(page, "Customer ledger");
  await expect(ledger).toContainText("Healthy");
  await expect(ledger.getByText("3", { exact: true })).toBeVisible();
  const vendors = sourceCard(page, "Vendor master");
  await expect(vendors).toContainText("Needs attention");
  await expect(vendors).toContainText("Import rejected: 2 invalid rows");
  await expect(
    page.getByRole("button", { name: /Upload|Add CSV source|Preview/ }),
  ).toHaveCount(0);
  await expect(page.getByLabel("Source name")).toHaveCount(0);
  await expect(page.getByRole("link", { name: /sample/i })).toHaveCount(0);
  expect([...new Set(calls)]).toEqual(["GET /sources"]);
});

test("source cards and a wide preview stay within a small screen", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const headers = Array.from(
    { length: 12 },
    (_, index) => `Exported column heading number ${index + 1}`,
  );
  const rows = Array.from({ length: 20 }, (_, row) =>
    Object.fromEntries(
      headers.map((header, column) => [
        header,
        `Synthetic value ${row}-${column} ${"x".repeat(60)}`,
      ]),
    ),
  );
  const label = "Northwind regional customer ledger exported by the finance team";
  await mockApi(page, admin, {
    "GET /sources": (route) =>
      route.fulfill({
        json: [
          sourceOf({
            label,
            health: "error",
            last_error: `Import rejected: ${"a very long reason ".repeat(8)}`,
          }),
        ],
      }),
    "POST /sources/preview": (route) =>
      route.fulfill({
        json: {
          headers,
          rows,
          total_rows: 2060,
          stable_id_warning: STABLE_ID_WARNING,
          column_mapping: {},
          source_id: "source-1",
        },
      }),
  });
  await page.goto("/sources");
  await expect(sourceCard(page, label)).toBeVisible();
  expect(await fitsViewport(page)).toBe(true);
  const wizard = await previewFile(page, label);
  const region = wizard.getByRole("region", { name: /first 20 of 2,060 rows/ });
  await expect(region).toHaveAttribute("tabindex", "0");
  await expect(region.locator("tbody tr")).toHaveCount(20);
  expect(await fitsViewport(page)).toBe(true);
});

const cslVersion = {
  id: "version-1",
  fetched_at: "2026-10-01T14:05:00Z",
  file_sha256: "a".repeat(64),
  semantic_sha256: "b".repeat(64),
  entry_count: 12408,
  added: 12408,
  changed: 0,
  removed: 0,
  source_list_stamps: {},
};
const emptyStatus = {
  current_version: null,
  last_fetch: null,
  pending_screenings: 0,
};
const readyStatus = {
  current_version: cslVersion,
  last_fetch: {
    id: "fetch-1",
    fetched_at: cslVersion.fetched_at,
    completed_at: cslVersion.fetched_at,
    status: "success",
    version_id: cslVersion.id,
    file_sha256: cslVersion.file_sha256,
    error: null,
    source_list_stamps: {},
    warnings: [],
  },
  pending_screenings: 0,
};

test("Overview keeps the list download available while sources fail, then counts only a real import", async ({
  page,
}) => {
  let sourcesHealthy = false;
  await mockApi(page, admin, {
    "GET /csl/status": (route) => route.fulfill({ json: emptyStatus }),
    "GET /sources": (route) =>
      sourcesHealthy
        ? route.fulfill({ json: [importedSource] })
        : route.fulfill({
            status: 500,
            json: { detail: "Sources could not be read." },
          }),
  });
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText(
    "Sources could not be read.",
  );
  await expect(
    page.getByText(
      "Progress is unavailable until the list and source status load.",
    ),
  ).toBeVisible();
  await expect(page.getByText(/of 3 steps complete/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Fetch CSL now" })).toBeEnabled();
  await expect(
    page.getByRole("link", { name: /Add source|View sources/ }),
  ).toHaveCount(0);

  sourcesHealthy = true;
  await page.getByRole("button", { name: "Check again" }).click();
  await expect(page.getByText("2 of 3 steps complete")).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: "Your customer and vendor records are imported",
    }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "View sources" })).toHaveAttribute(
    "href",
    "/sources",
  );
  await expect(page.getByText("Imported", { exact: true })).toBeVisible();
  await expect(page.getByText("Setup in progress")).toBeVisible();
  await expect(page.getByText("Data is ready")).toHaveCount(0);
});

test("Overview says data is ready only after the list and a source import both exist", async ({
  page,
}) => {
  let sources = [sourceOf()];
  await mockApi(page, admin, {
    "GET /csl/status": (route) => route.fulfill({ json: readyStatus }),
    "GET /sources": (route) => route.fulfill({ json: sources }),
  });
  await page.goto("/");
  await expect(page.getByText("2 of 3 steps complete")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Add your customers and vendors" }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "Add source" })).toHaveAttribute(
    "href",
    "/sources",
  );
  await expect(page.getByText("Setup in progress")).toBeVisible();
  await expect(
    page.getByRole("button", { name: /source|customer|vendor/i }),
  ).toHaveCount(0);

  sources = [importedSource];
  await page.reload();
  await expect(page.getByText("3 of 3 steps complete")).toBeVisible();
  await expect(page.getByText("Data is ready")).toBeVisible();
  await expect(page.getByText(/screened|screening complete/i)).toHaveCount(0);
});


test("a header-only full snapshot marks existing source parties inactive without losing history", async ({ page }) => {
  const headerOnly = "Customer ID,Legal Name\r\n";
  let current = importedSource;
  let uploadRequest: Request | undefined;
  await mockApi(page, admin, {
    "GET /sources": (route) => route.fulfill({ json: [current] }),
    "POST /sources/preview": (route) => route.fulfill({ json: previewOf(headerOnly) }),
    "POST /sources/source-1/upload": (route) => {
      uploadRequest = route.request();
      current = { ...importedSource, record_count: 0, last_synced_at: "2026-10-13T10:00:00Z" };
      return route.fulfill({ json: uploadResult({ source: current, deleted: 3 }) });
    },
  });
  await page.goto("/sources");
  const wizard = await previewFile(page, "Customer ledger", csvFile("empty-snapshot.csv", headerOnly));
  const preview = wizard.getByRole("table", { name: "Preview of empty-snapshot.csv: first 0 of 0 rows" });
  await expect(preview.locator("tbody tr")).toHaveCount(0);
  await expect(wizard.getByText(/This snapshot contains no party rows/)).toContainText("their history and audit records are kept");
  await expect(wizard.getByRole("button", { name: "Import file" })).toBeDisabled();
  await wizard.getByLabel(CONFIRM).check();
  await expect(wizard.getByRole("button", { name: "Import file" })).toBeEnabled();
  await wizard.getByRole("button", { name: "Import file" }).click();
  await expect(wizard.getByText("The import was applied.")).toBeVisible();
  await expect(wizard.getByRole("status")).toContainText("3 deleted (marked inactive)");
  await expect(sourceCard(wizard, "Customer ledger").getByText("0", { exact: true })).toBeVisible();
  const multipart = parseMultipart(uploadRequest!);
  expect(multipart.parts[0].value).toBe(headerOnly);
  expect(JSON.parse(multipart.parts[1].value)).toEqual({ name: "Legal Name", external_id: "Customer ID" });
});

test("an uncertain server failure asks for a status check without claiming the import was refused", async ({ page }) => {
  await mockApi(page, admin, {
    "GET /sources": (route) => route.fulfill({ json: [importedSource] }),
    "POST /sources/preview": (route) => route.fulfill({ json: previewOf(csvText, savedMapping) }),
    "POST /sources/source-1/upload": (route) => route.fulfill({ status: 500, json: { detail: "The server could not finish the response" } }),
  });
  await page.goto("/sources");
  const wizard = await previewFile(page, "Customer ledger");
  await importPreviewed(wizard);
  const alert = wizard.getByRole("alert");
  await expect(alert).toContainText("The import outcome could not be confirmed.");
  await expect(alert).toContainText("Check this source’s status before trying again.");
  await expect(alert).not.toContainText("The file was not imported.");
  await expect(alert).not.toContainText("Earlier records for this source are unchanged.");
});


test("native export headers map street addresses into the actual upload", async ({ page }) => {
  const text = "external_id,name,party_type,role,address_lines,city,region,postal_code,country\r\nfixture-1,Synthetic Company,company,vendor,42 Example Road,Portland,OR,97201,US\r\n";
  let request: Request | undefined;
  await mockApi(page, admin, {
    "GET /sources": (route) => route.fulfill({ json: [sourceOf()] }),
    "POST /sources/preview": (route) => route.fulfill({ json: previewOf(text) }),
    "POST /sources/source-1/upload": (route) => {
      request = route.request();
      return route.fulfill({ json: uploadResult({ added: 1 }) });
    },
  });
  await page.goto("/sources");
  const wizard = await previewFile(page, "Customer ledger", csvFile("native-export.csv", text));
  await expect(wizard.getByRole("group", { name: "Address line columns" }).getByRole("checkbox", { name: "address_lines", exact: true })).toBeChecked();
  await importPreviewed(wizard);
  await expect(wizard.getByText("The import was applied.")).toBeVisible();
  const mapping = JSON.parse(parseMultipart(request!).parts.find(part => part.name === "mapping")!.value);
  expect(mapping).toEqual({ name: "name", external_id: "external_id", party_type: "party_type", role: "role", city: "city", region: "region", postal_code: "postal_code", country: "country", address_lines: ["address_lines"] });
});


test("an applied import with a failed screening task preserves success and shows the pending warning", async ({ page }) => {
  await mockApi(page, admin, {
    "GET /sources": route => route.fulfill({ json: [importedSource] }),
    "POST /sources/preview": route => route.fulfill({ json: previewOf(csvText, savedMapping) }),
    "POST /sources/source-1/upload": route => route.fulfill({ json: uploadResult({
      added: 1, task_ids: ["successful-task", "failed-task"], screening_run_ids: ["completed-run"],
      screening_pending: true,
      screening_errors: [{ task_id: "failed-task", attempts: 2, error: "Screening failed; no comparison from this task was committed" }],
    }) }),
  });
  await page.goto("/sources");
  const wizard = await previewFile(page, "Customer ledger");
  await importPreviewed(wizard);
  await expect(wizard.getByText("The import was applied.")).toBeVisible();
  await expect(wizard.getByRole("alert")).toContainText("The records were imported, but screening is pending.");
  await expect(wizard.getByRole("alert")).toContainText("Screening failed; no comparison from this task was committed (attempt 2)");
  await expect(wizard.getByRole("status")).toContainText("Screening recorded 1 run for new and changed parties.");
  await expect(wizard.getByText("Nothing was imported.")).toHaveCount(0);
});
