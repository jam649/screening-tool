import { readFileSync, readdirSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const frontend = dirname(dirname(fileURLToPath(import.meta.url)));
const listing = spawnSync("npm", ["ls", "--omit=dev", "--all", "--parseable"], {
  cwd: frontend,
  encoding: "utf8",
  stdio: ["ignore", "pipe", "pipe"],
});
if (listing.status !== 0) throw new Error("Unable to enumerate installed frontend runtime dependencies");
const packages = [];
for (const directory of new Set(listing.stdout.trim().split("\n").slice(1))) {
  const metadata = JSON.parse(readFileSync(join(directory, "package.json"), "utf8"));
  const filenames = readdirSync(directory).filter(name => /^(?:licen[sc]e|copying|notice)(?:[._-]|$)/i.test(name)).sort();
  if (!filenames.length) throw new Error(`Missing original runtime license notice: ${metadata.name}`);
  packages.push({
    name: metadata.name,
    version: metadata.version,
    license: typeof metadata.license === "string" ? metadata.license : JSON.stringify(metadata.license),
    notices: filenames.map(name => ({ name, text: readFileSync(join(directory, name), "utf8") })),
  });
}
packages.sort((left, right) => `${left.name}@${left.version}`.localeCompare(`${right.name}@${right.version}`, "en"));
const sections = packages.map(pkg => [
  `Package: ${pkg.name}@${pkg.version}`,
  `License metadata: ${pkg.license}`,
  ...pkg.notices.map(notice => `Original ${notice.name}:\n${notice.text}`),
].join("\n"));
const text = "screening-tool frontend runtime dependency notices\n" +
  "Generated from the pinned installed packages; original notices are retained verbatim.\n" +
  "Application license: MIT. Dependency licenses remain separate.\n\n" +
  sections.join("\n\n" + "=".repeat(72) + "\n\n") + "\n";
const destinations = [
  join(frontend, "public", "third-party-notices.txt"),
  join(frontend, "..", "docs", "licenses", "frontend-runtime-LICENSES.txt"),
];
for (const destination of destinations) {
  if (process.argv.includes("--check")) {
    if (readFileSync(destination, "utf8") !== text) throw new Error("Frontend notices differ from pinned packages; regenerate them");
  } else {
    mkdirSync(dirname(destination), { recursive: true });
    writeFileSync(destination, text);
  }
}
console.info(`Verified original notices for ${packages.length} frontend runtime packages.`);
