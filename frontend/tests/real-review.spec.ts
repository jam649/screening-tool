import { expect, test } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

test.use({ trace: "off" });

function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [], cell = "", quoted = false;
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (char === '"') {
      if (quoted && text[i + 1] === '"') { cell += '"'; i++; }
      else quoted = !quoted;
    } else if (char === "," && !quoted) { row.push(cell); cell = ""; }
    else if ((char === "\r" || char === "\n") && !quoted) {
      if (char === "\r" && text[i + 1] === "\n") i++;
      row.push(cell); rows.push(row); row = []; cell = "";
    } else cell += char;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  return rows;
}
function csvText(rows: string[][]) {
  return rows.map(row => row.map(cell => /[",\r\n]/.test(cell) ? `"${cell.replaceAll('"', '""')}"` : cell).join(",")).join("\r\n") + "\r\n";
}

// Run alone after real-csv.spec.ts on the same isolated Docker data volume,
// rebuilt with the operational M4 frontend. No session files or auth bypasses.
test("real Reviewer gate accepts an invitation, works F M E J K V, and retains historical party evidence", async ({ browser, baseURL }) => {
  test.skip(!process.env.PLAYWRIGHT_BASE_URL || process.env.SCREENING_TOOL_REVIEW_GATE !== "1", "Requires the populated isolated CSV gate installation and explicit Reviewer gate flag.");
  test.setTimeout(600_000);
  const adminContext = await browser.newContext({ baseURL });
  const adminPage = await adminContext.newPage();
  await adminPage.goto("/");
  await adminPage.getByLabel("Email address").fill("docker-gate-admin@example.test");
  await adminPage.getByLabel("Password", { exact: true }).fill("Synthetic gate password 2026!");
  await adminPage.getByRole("button", { name: "Sign in" }).click();
  await expect(adminPage.getByRole("navigation", { name: "Main navigation" })).toBeVisible();
  const adminSession = await (await adminContext.request.get("/api/v1/auth/me")).json();
  const adminHeaders = { "X-CSRF-Token": adminSession.csrf_token, Origin: new URL(baseURL!).origin };
  const inviteResponse = await adminContext.request.post("/api/v1/users/invitations", {
    data: { email: `reviewer-gate-${Date.now()}@example.test`, role: "reviewer" }, headers: adminHeaders,
  });
  expect(inviteResponse.status()).toBe(201);
  const invite = await inviteResponse.json();
  const token = new URL(invite.invite_url, baseURL).hash.slice("#token=".length);
  const reviewerContext = await browser.newContext({ baseURL });
  const page = await reviewerContext.newPage();
  const urls: string[] = [];
  const browserErrors: string[] = [];
  page.on("pageerror", error => browserErrors.push(error.name));
  let decisionsRequested = 0;
  page.on("request", request => {
    urls.push(request.url());
    if (request.method() === "POST" && request.url().endsWith("/dispositions")) decisionsRequested++;
  });
  try {
    await page.goto(invite.invite_url);
  } catch {
    throw new Error("The invitation page could not be opened; token details are omitted.");
  }
  await expect.poll(() => new URL(page.url()).hash === "").toBe(true);
  await page.getByRole("textbox", { name: /name/i }).fill("Synthetic Gate Reviewer");
  await page.getByLabel("Password", { exact: true }).fill("Synthetic Reviewer gate password 2026!");
  await page.getByRole("button", { name: /Accept invitation|Create account|Join workspace|Create your account/i }).click();
  await expect(page.getByRole("navigation", { name: "Main navigation" })).toBeVisible();
  expect(urls.every(url => !url.includes(token))).toBe(true);
  await expect(page.getByRole("link", { name: "Settings", exact: true })).toHaveCount(0);
  const reviewer = await (await reviewerContext.request.get("/api/v1/auth/me")).json();
  expect(reviewer.user.role).toBe("reviewer");
  const queue = await (await reviewerContext.request.get("/api/v1/queue")).json();
  expect(queue.total).toBe(77);
  console.info("Real Reviewer gate: invitation accepted; 77 synthetic queue items loaded.");
  const original = queue.items[0];
  const firstRead = page.waitForResponse(response => response.url().endsWith(`/hits/${original.id}`));
  await page.goto("/queue");
  const originalDetail = await (await firstRead).json();
  const note = page.getByRole("textbox", { name: /note/i });
  await expect(note).toBeVisible();
  const queueScreenshots = join(homedir(), "Desktop/screenshots/screening-tool");
  await mkdir(queueScreenshots, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 950 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await page.screenshot({ path: join(queueScreenshots, "milestone-4-real-reviewer-queue.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: join(queueScreenshots, "milestone-4-real-reviewer-queue-mobile.png") });
  await page.setViewportSize({ width: 1440, height: 950 });

  const nextRead = page.waitForResponse(response => response.url().endsWith(`/hits/${queue.items[1].id}`));
  await page.locator("h1").click();
  await page.keyboard.press("j");
  expect((await (await nextRead).json()).id).toBe(queue.items[1].id);
  await page.keyboard.press("k");
  await expect(note).toHaveValue("");
  expect(originalDetail.source_url, "Use a real queue comparison with an official source URL for the V shortcut gate.").toBeTruthy();
  const popupPromise = page.waitForEvent("popup");
  await page.keyboard.press("v");
  const popup = await popupPromise;
  await expect.poll(() => popup.url()).toMatch(/^https?:\/\//);
  expect(new URL(popup.url()).protocol).toMatch(/^https?:$/);
  await popup.close();
  await note.fill("Synthetic false-positive evidence ");
  await page.keyboard.press("m");
  await expect(note).toHaveValue("Synthetic false-positive evidence m");
  expect(decisionsRequested).toBe(0);
  await page.locator("h1").click();
  const clearedResponse = page.waitForResponse(response => response.request().method() === "POST" && response.url().endsWith("/dispositions"));
  await page.keyboard.press("f");
  const cleared = await clearedResponse;
  expect(cleared.status()).toBe(201);
  const falsePositive = await cleared.json();
  expect(falsePositive.hit_id).toBe(original.id); // proves K returned from J.
  expect(falsePositive.disposition.kind).toBe("false_positive");
  expect(falsePositive.disposition.actor.id).toBe(reviewer.user.id);
  expect(falsePositive.disposition.note).toBe("Synthetic false-positive evidence m");

  await expect(note).toHaveValue("");
  await expect(page.getByRole("button", { name: /Confirm(ed)? match/i })).toBeDisabled();
  await page.locator("h1").click();
  await page.keyboard.press("m");
  await expect(note).toBeFocused();
  expect(decisionsRequested).toBe(1);
  await note.fill("Synthetic confirmed-match review note");
  await page.locator("h1").click();
  const matchResponse = page.waitForResponse(response => response.request().method() === "POST" && response.url().endsWith("/dispositions"));
  await page.keyboard.press("m");
  const match = await matchResponse;
  expect(match.status()).toBe(201);
  const confirmed = await match.json();
  expect(confirmed.disposition.kind).toBe("confirmed_match");
  expect(confirmed.disposition.actor.id).toBe(reviewer.user.id);
  expect(confirmed.disposition.note).toBe("Synthetic confirmed-match review note");

  await expect(note).toHaveValue("");
  await expect(page.getByRole("button", { name: /Escalate/i })).toBeDisabled();
  await page.locator("h1").click();
  await page.keyboard.press("e");
  await expect(note).toBeFocused();
  expect(decisionsRequested).toBe(2);
  await note.fill("Synthetic escalation pending additional diligence");
  await page.locator("h1").click();
  const escalationResponse = page.waitForResponse(response => response.request().method() === "POST" && response.url().endsWith("/dispositions"));
  await page.keyboard.press("e");
  const escalation = await escalationResponse;
  expect(escalation.status()).toBe(201);
  const escalated = await escalation.json();
  expect(escalated.disposition.kind).toBe("escalated");
  expect(escalated.disposition.actor.id).toBe(reviewer.user.id);
  expect(new Set([falsePositive.hit_id, confirmed.hit_id, escalated.hit_id]).size).toBe(3);

  const actions = [falsePositive, confirmed, escalated];
  // This is the explicitly synthetic 2060-party gate data, never customer decisions.
  const fixtureSources = await (await adminContext.request.get("/api/v1/sources")).json();
  expect(fixtureSources.every((item: { label: string }) => item.label.includes("Synthetic"))).toBe(true);
  let remaining = await (await reviewerContext.request.get("/api/v1/queue")).json();
  while (remaining.total > 0) {
    await expect(page.getByRole("button", { name: /Clear.*false positive/i })).toBeEnabled();
    await note.fill("Synthetic full-queue gate: reviewed fixture comparison context");
    await page.locator("h1").click();
    const responsePromise = page.waitForResponse(response => response.request().method() === "POST" && response.url().endsWith("/dispositions"));
    await page.keyboard.press("f");
    const response = await responsePromise;
    expect(response.status()).toBe(201);
    const action = await response.json();
    expect(action.disposition.actor.id).toBe(reviewer.user.id);
    expect(action.disposition.kind).toBe("false_positive");
    expect(action.observations_dispositioned).toBeGreaterThan(0);
    actions.push(action);
    const next = await (await reviewerContext.request.get("/api/v1/queue")).json();
    expect(next.total).toBe(remaining.total - 1);
    remaining = next;
  }
  expect(actions.length).toBe(queue.total);
  console.info("Real Reviewer gate: all 77 keyboard decisions committed with Reviewer attribution.");
  expect(new Set(actions.map(action => action.hit_id)).size).toBe(queue.total);
  // Every keyboard-selected action must have real actor attribution, an audit
  // row, and a matching immutable observation in its party file.
  const auditResponse = await reviewerContext.request.get("/api/v1/audit-log?action=review.disposition&target_table=dispositions&limit=1000");
  expect(auditResponse.ok()).toBeTruthy();
  const allAudits = await auditResponse.json();
  const files = new Map<string, { events: Array<{ hits: Array<{ id: string; dispositions: Array<{ id: string; actor: { id: string } }> }> }> }>();
  for (const action of actions) {
    const selected = queue.items.find((item: { id: string }) => item.id === action.hit_id);
    expect(selected).toBeDefined();
    expect(action.disposition.actor.id).toBe(reviewer.user.id);
    expect(allAudits.items.some((event: { target_id: string; after?: { actor?: { id: string }; queue_hit_id?: string } }) => event.target_id === action.disposition.id && event.after?.actor?.id === reviewer.user.id && event.after?.queue_hit_id === action.hit_id)).toBe(true);
    if (!files.has(selected.party_id)) {
      const response = await reviewerContext.request.get(`/api/v1/parties/${selected.party_id}`);
      expect(response.ok()).toBeTruthy();
      files.set(selected.party_id, await response.json());
    }
    const observation = files.get(selected.party_id)!.events.flatMap(event => event.hits).find(hit => hit.id === action.hit_id);
    expect(observation).toBeDefined();
    expect(observation!.dispositions.some(decision => decision.id === action.disposition.id && decision.actor.id === reviewer.user.id)).toBe(true);
  }
  console.info("Real Reviewer gate: every selected decision verified in audit and immutable party files.");
  const repeatScreen = await adminContext.request.post("/api/v1/screen", { data: { all: true }, headers: adminHeaders, timeout: 60_000 });
  expect(repeatScreen.ok()).toBeTruthy();
  const repeatedAssessment = await repeatScreen.json();
  expect(repeatedAssessment.hits_new).toBe(0);
  expect(repeatedAssessment.parties_screened).toBe(2060);
  expect((await (await reviewerContext.request.get("/api/v1/queue")).json()).total).toBe(0);
  const confirmedHit = await (await reviewerContext.request.get(`/api/v1/hits/${confirmed.hit_id}`)).json();
  const escalatedHit = await (await reviewerContext.request.get(`/api/v1/hits/${escalated.hit_id}`)).json();
  for (const [retained, kind] of [[confirmedHit, "confirmed_match"], [escalatedHit, "escalated"]] as const) {
    const file = await (await reviewerContext.request.get(`/api/v1/parties/${retained.party_id}`)).json();
    const observation = file.events[0].hits.find((hit: { csl_entry_id: string }) => hit.csl_entry_id === retained.csl_entry_id);
    expect(observation.completed_disposition.kind).toBe(kind);
    expect(observation.completed_disposition.actor.id).toBe(reviewer.user.id);
    expect(observation.dispositions).toEqual([]);
  }

  console.info("Real Reviewer gate: unchanged full screening left the queue empty and retained original decisions.");
  const historical = await (await reviewerContext.request.get(`/api/v1/parties/${original.party_id}`)).json();
  const oldName = historical.party.name;
  const oldType = historical.party.party_type;
  const source = historical.source;
  const csvResponse = await adminContext.request.get("/api/v1/sources/demo.csv");
  const rows = parseCsv(await csvResponse.text());
  const headers = rows[0];
  const row = rows.find((values, index) => index > 0 && values[headers.indexOf("external_id")] === historical.party.external_id);
  expect(row).toBeDefined();
  const updatedName = `${oldName} Synthetic revision`;
  row![headers.indexOf("name")] = updatedName;
  row![headers.indexOf("party_type")] = oldType === "company" ? "individual" : "company";
  const updateResponse = await adminContext.request.post(`/api/v1/sources/${source.id}/upload`, {
    headers: adminHeaders, multipart: { file: { name: "synthetic-revised-snapshot.csv", mimeType: "text/csv", buffer: Buffer.from(csvText(rows)) } }, timeout: 60_000,
  });
  expect(updateResponse.ok()).toBeTruthy();
  const update = await updateResponse.json();
  expect(update).toMatchObject({ applied: true, added: 0, changed: 1, unchanged: 2059, deleted: 0 });
  const file = await (await reviewerContext.request.get(`/api/v1/parties/${original.party_id}`)).json();
  expect(file.party.name).toBe(updatedName);
  const oldHit = file.events.flatMap((event: { hits: Array<{ id: string; party_snapshot: { name: string; party_type: string } }> }) => event.hits).find((hit: { id: string }) => hit.id === original.id);
  expect(oldHit.party_snapshot.name).toBe(oldName);
  expect(oldHit.party_snapshot.party_type).toBe(oldType);
  const newQueue = await (await reviewerContext.request.get("/api/v1/queue")).json();
  const reopened = newQueue.items.find((hit: { party_id: string; csl_entry_id: string }) => hit.party_id === original.party_id && hit.csl_entry_id === original.csl_entry_id);
  expect(reopened).toBeDefined();
  const newHit = await (await reviewerContext.request.get(`/api/v1/hits/${reopened.id}`)).json();
  expect(newHit.prior_disposition.actor.id).toBe(reviewer.user.id);
  expect(newHit.prior_disposition.note).toBe("Synthetic false-positive evidence m");
  expect(newHit.prior_disposition.what_changed.party.party_type).toMatchObject({ before: oldType, after: row![headers.indexOf("party_type")] });
  await page.goto(`/parties/${original.party_id}`);
  await expect(page.getByText(updatedName, { exact: true }).first()).toBeVisible();
  const reopenedRecord = page.locator("details.rv-hit").filter({ hasText: reopened.id });
  await expect(reopenedRecord).toHaveCount(1);
  await reopenedRecord.locator(":scope > summary").click();
  await expect(reopenedRecord.getByRole("region", { name: "Earlier decision on this pairing" })).toBeVisible();
  await expect(reopenedRecord.getByRole("region", { name: "Earlier decision on this pairing" })).toContainText("Synthetic false-positive evidence m");
  await expect(reopenedRecord.getByRole("region", { name: "Earlier decision on this pairing" })).toContainText("Synthetic Gate Reviewer");
  const audits = await (await reviewerContext.request.get(`/api/v1/audit-log?action=review.disposition&target_table=dispositions&target_id=${confirmed.disposition.id}`)).json();
  expect(audits.items.some((event: { after: { actor?: { id: string }; kind?: string } }) => event.after?.actor?.id === reviewer.user.id && event.after?.kind === "confirmed_match")).toBe(true);
  expect(browserErrors).toEqual([]);
  const screenshots = join(homedir(), "Desktop/screenshots/screening-tool");
  const artifacts = join(homedir(), "Desktop/artifacts/screening-tool");
  await mkdir(screenshots, { recursive: true }); await mkdir(artifacts, { recursive: true });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await page.screenshot({ path: join(screenshots, "milestone-4-real-reviewer-party-history.png"), fullPage: true });
  await writeFile(join(artifacts, "milestone-4-real-reviewer-gate.json"), JSON.stringify({ role: reviewer.user.role, shortcuts: ["J", "K", "V", "F", "M", "E"], human_actions: actions.length, all_selected_actions_audited_and_in_party_files: true, unchanged_rescreen_queue_empty: true, attribution_verified: true, immutable_party_snapshot_verified: true, type_change_reopened: true, invitation_token_absent_from_network_urls: urls.every(url => !url.includes(token)) }, null, 2) + "\n");
  await reviewerContext.close(); await adminContext.close();
});
