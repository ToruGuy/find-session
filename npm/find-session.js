#!/usr/bin/env node
// npx entry point of find-session. Zero dependencies, Node 18+.
//
//   install / update / uninstall   handled here (copy the skill, link it into the harnesses)
//   anything else                  passed to the Python helper (search, inspect, recent, ...)
//
// npx runs this from a temporary npm cache, so the skill is copied to a stable
// place first and the harnesses link to that copy, never to the cache.
//
// The link rules are the same as in scripts/install_skill.py (the installer of
// a clone): tests/install_cases.json is run against both.

"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

const SKILL_NAME = "find-session";
const PACKAGE_ROOT = path.resolve(__dirname, "..");
const PACKAGE_SKILL = path.join(PACKAGE_ROOT, "skills", SKILL_NAME);
const VERSION = require(path.join(PACKAGE_ROOT, "package.json")).version;
const HARNESSES = ["claude", "codex", "omp"];
const LABELS = { claude: "Claude Code", codex: "Codex", omp: "omp" };
const OWN_COMMANDS = ["install", "update", "uninstall"];
// The helper's commands (find_session/cli.py COMMANDS; a test compares the two lists).
const HELPER_COMMANDS = ["search", "inspect", "recent", "index", "sources", "build-index"];
// Older name of `recent`, still accepted by the helper.
const LEGACY_COMMANDS = ["brief-input"];
const MARKER = ".installed-by-npm.json";
const MIN_PYTHON = [3, 9];
// Exit code when no usable Python is found (the helper itself uses 1 and 2).
const EXIT_NO_PYTHON = 3;
const SKIP_COPY = new Set(["__pycache__", ".DS_Store"]);
// Files the helper writes into its cache directory, and nothing else: the index
// (find_session/cache.py `cache_path`) with the files SQLite keeps next to it
// (find_session/index.py COMPANIONS), and the caches of versions before 0.3.
// tests/test_cache.py checks this pattern against the names the helper really uses.
const CACHE_FILE =
  /^(index-[0-9a-f]{8}-py\d+\.sqlite3(-wal|-shm|-journal)?|\.?sessions-[0-9a-f]{12}(-py\d+\.bin(\.\d+\.tmp)?|\.json))$/;

const USAGE = `find-session ${VERSION}: search past Claude Code, Codex and omp conversations.

Usage: npx find-session <command> [options]

Set up:
  install     copy the skill to ${displayPath(dataDir())} and link it into
              every tool found (~/.claude, ~/.codex, ~/.omp), show what it sees
              and index the history so the first search is fast
  update      refresh the copy from this package (run as: npx find-session@latest update)
  uninstall   remove our links, the copied skill and the search index

Search (runs the Python helper, needs Python 3.9+):
  search "words"    [--source claude|codex|omp] [--since 3d] [--project NAME] [--json]
  inspect SELECTOR  [--range A:B] [--tail N] [--grep WORDS] [--tools]
  recent, index, sources, build-index, --version

  <command> --help  all options of a command
`;

const INSTALL_USAGE = (command) => `Usage: npx find-session ${command} [options]

${
  command === "update"
    ? "Replace the copied skill with the one in this package (run as: npx find-session@latest update)\nand link it like install does."
    : `Copy the skill to ${displayPath(dataDir())}, link it into every tool found\n(~/.claude, ~/.codex, ~/.omp), show how many sessions each has and index the history.`
}
Safe to run again. A find-session installed another way (a clone, skills.sh, a Claude Code plugin) is kept.

  --harness claude|codex|omp   link this tool, found or not (repeatable; default: every tool found)
  --replace                    move a different existing skill to a backup, then link
  --no-index                   do not index the history now (the first search does it then)
  --dry-run                    show what would change
`;

const UNINSTALL_USAGE = `Usage: npx find-session uninstall [--dry-run]

Remove what install created: our links in the tools' skill directories, the copied
skill and the search index in ${displayPath(cacheDir())}. Links and files that are
not ours, a git clone and your config are kept.

  --dry-run   show what would be removed
`;

function displayPath(target) {
  const home = os.homedir();
  return target === home || target.startsWith(home + path.sep) ? "~" + target.slice(home.length) : target;
}

function dataDir() {
  if (process.platform === "win32" && process.env.LOCALAPPDATA) {
    return path.join(process.env.LOCALAPPDATA, SKILL_NAME);
  }
  const base = process.env.XDG_DATA_HOME || path.join(os.homedir(), ".local", "share");
  return path.join(base, SKILL_NAME);
}

function cacheDir() {
  const base = process.env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache");
  return path.join(base, SKILL_NAME);
}

function ompAgentDir() {
  return process.env.PI_CODING_AGENT_DIR || path.join(os.homedir(), ".omp", "agent");
}

function skillRoots() {
  const home = os.homedir();
  // omp reads ~/.claude/skills only when opted in, so it gets its own link.
  return {
    claude: path.join(home, ".claude", "skills"),
    codex: path.join(home, ".codex", "skills"),
    omp: path.join(ompAgentDir(), "skills"),
  };
}

// Other places a tool reads skills from, where another installer may have put this one.
function otherRoots() {
  // skills.sh (`npx skills add`) installs for Codex into ~/.agents/skills.
  return { claude: pluginSkillRoots(), codex: [path.join(os.homedir(), ".agents", "skills")] };
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return null;
  }
}

// The skills folders of find-session installed as a Claude Code plugin
// (`/plugin install find-session@…`). Claude Code lists its plugins in
// ~/.claude/plugins/installed_plugins.json. Only a plugin that every session
// loads counts: installed for the user (not for one project) and not switched
// off in ~/.claude/settings.json.
function pluginSkillRoots() {
  const claude = path.join(os.homedir(), ".claude");
  const plugins = (readJson(path.join(claude, "plugins", "installed_plugins.json")) || {}).plugins;
  if (!plugins || typeof plugins !== "object") return [];
  const enabled = (readJson(path.join(claude, "settings.json")) || {}).enabledPlugins || {};
  const roots = [];
  for (const [name, installs] of Object.entries(plugins)) {
    if (name.split("@")[0] !== SKILL_NAME || enabled[name] === false) continue;
    for (const entry of Array.isArray(installs) ? installs : []) {
      if (entry && typeof entry.installPath === "string" && (entry.scope || "user") === "user") {
        roots.push(path.join(entry.installPath, "skills"));
      }
    }
  }
  return roots;
}

function detectedHarnesses() {
  const home = os.homedir();
  const present = {
    claude: isDir(path.join(home, ".claude")),
    codex: isDir(path.join(home, ".codex")),
    omp: isDir(ompAgentDir()) || isDir(path.join(home, ".omp")),
  };
  return HARNESSES.filter((name) => present[name]);
}

function isDir(target) {
  try {
    return fs.statSync(target).isDirectory();
  } catch {
    return false;
  }
}

function exists(target) {
  try {
    fs.lstatSync(target);
    return true;
  } catch {
    return false;
  }
}

function isSymlink(target) {
  try {
    return fs.lstatSync(target).isSymbolicLink();
  } catch {
    return false;
  }
}

function realpath(target) {
  try {
    return fs.realpathSync(target);
  } catch {
    return null;
  }
}

// Where a link points, as an absolute path, even when the target is gone.
function linkTarget(link) {
  try {
    return path.resolve(path.dirname(link), fs.readlinkSync(link));
  } catch {
    return null;
  }
}

function samePath(a, b) {
  if (!a || !b) return false;
  const left = realpath(a) || path.resolve(a);
  const right = realpath(b) || path.resolve(b);
  return left === right;
}

// The version of a find-session skill folder, or null when `skill` is not one.
function installedVersion(skill) {
  try {
    const head = fs.readFileSync(path.join(skill, "SKILL.md"), "utf8").slice(0, 400);
    if (!head.split(/\r?\n/).includes(`name: ${SKILL_NAME}`)) return null;
    if (!fs.statSync(path.join(skill, "scripts", "sessions.py")).isFile()) return null;
    return fs.readFileSync(path.join(skill, "VERSION"), "utf8").trim() || "unknown version";
  } catch {
    return null;
  }
}

// 1 when version a is newer than b, -1 when older, 0 when equal or not comparable.
function compareVersions(a, b) {
  const parts = (text) => String(text).split(/[.+-]/).map((part) => Number.parseInt(part, 10));
  const [left, right] = [parts(a), parts(b)];
  for (let i = 0; i < 3; i++) {
    if (Number.isNaN(left[i]) || Number.isNaN(right[i])) return 0;
    if (left[i] !== right[i]) return left[i] > right[i] ? 1 : -1;
  }
  return 0;
}

// ---------------------------------------------------------------- Python

function findPython() {
  const forced = process.env.FIND_SESSION_PYTHON;
  const candidates = forced
    ? [[forced]]
    : process.platform === "win32"
      ? [["python3"], ["python"], ["py", "-3"]]
      : [["python3"], ["python"]];
  const tooOld = [];
  for (const [command, ...prefix] of candidates) {
    const probe = spawnSync(
      command,
      [...prefix, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
      { encoding: "utf8", windowsHide: true },
    );
    if (probe.error || probe.status !== 0) continue;
    const version = (probe.stdout || "").trim();
    const [major, minor] = version.split(".").map(Number);
    if (major > MIN_PYTHON[0] || (major === MIN_PYTHON[0] && minor >= MIN_PYTHON[1])) {
      return { command, prefix, version };
    }
    tooOld.push(`${[command, ...prefix].join(" ")} is Python ${version}`);
  }
  const need = `Python ${MIN_PYTHON.join(".")} or newer is needed`;
  if (tooOld.length) {
    return { error: `${need}, but ${tooOld.join("; ")}. Install a newer Python and run this again.` };
  }
  const tried = forced ? `FIND_SESSION_PYTHON (${forced}) did not run` : "no python3 or python was found on PATH";
  return { error: `${need}, and ${tried}. Install it from https://www.python.org/downloads/ and run this again.` };
}

function requirePython() {
  const python = findPython();
  if (python.error) {
    console.error(`find-session: ${python.error}`);
    process.exitCode = EXIT_NO_PYTHON;
    return null;
  }
  return python;
}

function runHelper(python, skillDir, args, env = {}) {
  const helper = path.join(skillDir, "scripts", "sessions.py");
  return new Promise((resolve) => {
    const child = spawn(python.command, [...python.prefix, helper, ...args], {
      stdio: "inherit",
      // Usage and error lines of the helper name the command the user typed.
      env: { ...process.env, FIND_SESSION_PROG: "npx find-session", ...env },
    });
    // Ctrl-C reaches the helper directly; the wrapper just waits for it.
    const ignore = () => {};
    process.on("SIGINT", ignore);
    process.on("SIGTERM", ignore);
    child.on("error", (error) => {
      console.error(`find-session: could not start Python: ${error.message}`);
      resolve(EXIT_NO_PYTHON);
    });
    child.on("close", (code, signal) => {
      process.removeListener("SIGINT", ignore);
      process.removeListener("SIGTERM", ignore);
      resolve(signal ? 128 + (os.constants.signals[signal] || 1) : code);
    });
  });
}

// ---------------------------------------------------------------- options

function parseOptions(args, allowed) {
  const options = { harness: [], replace: false, dryRun: false, index: true };
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "--dry-run" && allowed.includes("dry-run")) options.dryRun = true;
    else if (arg === "--replace" && allowed.includes("replace")) options.replace = true;
    else if (arg === "--no-index" && allowed.includes("no-index")) options.index = false;
    else if ((arg === "--harness" || arg.startsWith("--harness=")) && allowed.includes("harness")) {
      const value = arg.includes("=") ? arg.slice(arg.indexOf("=") + 1) : args[++i];
      if (!HARNESSES.includes(value)) {
        throw new UsageError(`--harness must be one of ${HARNESSES.join(", ")} (got ${value ?? "nothing"})`);
      }
      if (!options.harness.includes(value)) options.harness.push(value);
    } else {
      throw new UsageError(`unknown option ${arg}`);
    }
  }
  return options;
}

class UsageError extends Error {}

// ---------------------------------------------------------------- the copy

// What lives in the data directory: our copy, a git clone, nothing, or something else.
function dataState(dir) {
  if (!exists(dir)) return "missing";
  if (exists(path.join(dir, MARKER))) return "ours";
  const cloneSkill = path.join(dir, "skills", SKILL_NAME, "SKILL.md");
  if (exists(path.join(dir, ".git"))) return exists(cloneSkill) ? "clone" : "old-clone";
  if (isDir(dir) && fs.readdirSync(dir).length === 0) return "missing";
  return "foreign";
}

function readMarker(dir) {
  try {
    return JSON.parse(fs.readFileSync(path.join(dir, MARKER), "utf8"));
  } catch {
    return {};
  }
}

function copyTree(source, destination) {
  fs.mkdirSync(destination, { recursive: true });
  for (const entry of fs.readdirSync(source, { withFileTypes: true })) {
    if (SKIP_COPY.has(entry.name) || /\.(pyc|egg-info)$/.test(entry.name)) continue;
    const from = path.join(source, entry.name);
    const to = path.join(destination, entry.name);
    if (entry.isDirectory()) copyTree(from, to);
    else if (entry.isFile()) fs.copyFileSync(from, to);
  }
}

// Replace the copied skill with the one in this package; never half-written.
function writeCopy(dir) {
  const skills = path.join(dir, "skills");
  const destination = path.join(skills, SKILL_NAME);
  const staging = path.join(skills, `.${SKILL_NAME}-${process.pid}-new`);
  fs.mkdirSync(skills, { recursive: true });
  fs.rmSync(staging, { recursive: true, force: true });
  copyTree(PACKAGE_SKILL, staging);
  fs.rmSync(destination, { recursive: true, force: true });
  fs.renameSync(staging, destination);
  const marker = { package: "find-session", version: VERSION, note: "Created by `npx find-session install`." };
  fs.writeFileSync(path.join(dir, MARKER), JSON.stringify(marker, null, 2) + "\n");
}

// ---------------------------------------------------------------- links

function backupPath(root) {
  const backups = path.join(path.dirname(root), "skill-backups");
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace("T", "-").replace(/\..*/, "");
  let candidate = path.join(backups, `${SKILL_NAME}-${stamp}`);
  for (let counter = 2; exists(candidate); counter++) {
    candidate = path.join(backups, `${SKILL_NAME}-${stamp}-${counter}`);
  }
  return candidate;
}

function makeLink(skillDir, link) {
  // A junction needs no special rights on Windows; elsewhere it is a plain symlink.
  fs.symlinkSync(skillDir, link, process.platform === "win32" ? "junction" : "dir");
}

function keptMessage(where, version) {
  return `kept, already installed from ${displayPath(where)} (${version}); --replace links this one instead`;
}

// Whether a tool already has a find-session that some other installer put there.
function elsewhere(root, others = []) {
  const link = path.join(root, SKILL_NAME);
  if (exists(link)) return installedVersion(link) !== null;
  return others.some((other) => installedVersion(path.join(other, SKILL_NAME)) !== null);
}

function linkHarness(root, skillDir, legacyRoot, others, { replace, dryRun }) {
  const link = path.join(root, SKILL_NAME);
  if (isSymlink(link) && samePath(link, skillDir)) return [true, "already linked"];
  if (isSymlink(link) && legacyRoot && samePath(link, legacyRoot)) {
    // Up to 0.2.1 a clone linked its repository root; point the link at the skill folder.
    if (dryRun) return [true, `would relink -> ${displayPath(skillDir)}`];
    fs.unlinkSync(link);
    makeLink(skillDir, link);
    return [true, `relinked -> ${displayPath(skillDir)}`];
  }
  if (exists(link)) {
    // A find-session installed another way (a clone, skills.sh) works: it is not a conflict.
    const version = installedVersion(link);
    if (version && !replace) return [true, keptMessage(realpath(link) || link, version)];
    if (!replace) return [false, `conflict: ${displayPath(link)} is a different skill, kept (use --replace)`];
    const backup = backupPath(root);
    if (dryRun) return [true, `would move ${displayPath(link)} to ${displayPath(backup)} and link`];
    fs.mkdirSync(path.dirname(backup), { recursive: true });
    // rename moves a symlink itself, never the directory it points to.
    fs.renameSync(link, backup);
    makeLink(skillDir, link);
    return [true, `linked, previous one moved to ${displayPath(backup)}`];
  }
  if (!replace) {
    for (const other of others) {
      const elsewhere = path.join(other, SKILL_NAME);
      const version = installedVersion(elsewhere);
      if (version && !samePath(elsewhere, skillDir)) return [true, keptMessage(elsewhere, version)];
    }
  }
  if (dryRun) return [true, `would link ${displayPath(link)} -> ${displayPath(skillDir)}`];
  fs.mkdirSync(root, { recursive: true });
  makeLink(skillDir, link);
  return [true, `linked ${displayPath(link)}`];
}

// ---------------------------------------------------------------- commands

async function install(args, { refresh }) {
  const options = parseOptions(args, ["harness", "replace", "dry-run", "no-index"]);
  const python = requirePython();
  if (!python) return process.exitCode;

  const harnesses = options.harness.length ? options.harness : detectedHarnesses();
  if (!harnesses.length) {
    console.error(
      "find-session: no Claude Code, Codex or omp directory found (~/.claude, ~/.codex, ~/.omp).\n" +
        "Install one of them first, or pick one explicitly: --harness claude",
    );
    return 1;
  }

  const dir = dataDir();
  const state = dataState(dir);
  const prefix = options.dryRun ? "dry-run: " : "";
  const roots = skillRoots();
  const others = otherRoots();
  let skillDir = path.join(dir, "skills", SKILL_NAME);
  // The skill whose helper shows the summary and builds the index: ours, unless nothing was copied.
  let helperDir = null;
  let legacyRoot = null;
  if (state === "old-clone") {
    // Up to 0.2.1 the README cloned the repository here, before the skill had its own folder.
    console.error(
      `find-session: ${displayPath(dir)} is a git clone without skills/find-session (an older find-session?).\n` +
        `Update it with: git -C ${displayPath(dir)} pull   then run this again. It is left alone.`,
    );
    return 1;
  }
  if (state === "foreign") {
    console.error(
      `find-session: ${displayPath(dir)} exists and was not created by this installer, so it is left alone.\n` +
        "Move it away (or delete it if you do not need it) and run this again.",
    );
    return 1;
  }
  if (state === "clone") {
    legacyRoot = dir;
    console.log(`skill: using your git clone at ${displayPath(dir)} (update it with: git -C ${displayPath(dir)} pull)`);
  } else {
    const installed = state === "ours" ? readMarker(dir).version : null;
    const intact = exists(path.join(skillDir, "SKILL.md"));
    if (state === "missing" && !options.replace && harnesses.every((name) => elsewhere(roots[name], others[name]))) {
      // Every tool already has a find-session from another installer: a copy here
      // would be a second version that nothing links to.
      console.log("skill: not copied, every tool here already has find-session (below); --replace moves them to an npx copy");
      helperDir = PACKAGE_SKILL;
    } else if (installed && intact && compareVersions(installed, VERSION) > 0) {
      // npx can run an older package from its cache: never put that over a newer install.
      console.log(
        `skill: ${displayPath(skillDir)} is ${installed}, newer than this package (${VERSION}): kept.\n` +
          `       For the newest release run: npx find-session@latest ${refresh ? "update" : "install"}` +
          `   (to go back to ${VERSION} on purpose, uninstall first)`,
      );
    } else if (installed === VERSION && intact && !refresh) {
      console.log(`skill: ${displayPath(skillDir)} is up to date (${VERSION})`);
    } else {
      const what = installed && installed !== VERSION ? `${installed} -> ${VERSION}` : VERSION;
      if (!options.dryRun) writeCopy(dir);
      console.log(`${prefix}skill: ${options.dryRun ? "would copy" : "copied"} ${what} to ${displayPath(skillDir)}`);
    }
  }

  let ok = true;
  for (const harness of HARNESSES) {
    if (!harnesses.includes(harness)) {
      console.log(
        options.harness.length
          ? `${harness}: skipped, not named with --harness`
          : `${harness}: skipped, ${LABELS[harness]} not found (add --harness ${harness} to install anyway)`,
      );
      continue;
    }
    const [success, message] = linkHarness(roots[harness], skillDir, legacyRoot, others[harness] || [], options);
    ok = ok && success;
    console.log(`${prefix}${harness}: ${message}`);
  }

  if (options.dryRun || !ok) return ok ? 0 : 1;
  // The summary first and the index last: an interrupted build leaves a finished install.
  console.log("\nWhat the skill can see:");
  const helperEnv = helperDir ? { FIND_SESSION_COMMAND: "npx find-session" } : {};
  let code = await runHelper(python, helperDir || skillDir, ["sources"], helperEnv);
  if (code === 0 && options.index) {
    console.log(
      "\nIndexing your history so that the first search is fast (one-time; --no-index skips it; " +
        "if it is interrupted, run this again: it continues):",
    );
    code = await runHelper(python, helperDir || skillDir, ["build-index"], helperEnv);
  }
  if (code === 0) {
    console.log(
      `\nDone. Start a new agent session and ask it to "find the session where ...".\n` +
        `Or search here: npx find-session search "words". Update later: npx find-session@latest update`,
    );
  }
  return code;
}

function uninstall(args) {
  const options = parseOptions(args, ["dry-run"]);
  const say = (message) => console.log(`${options.dryRun ? "dry-run: would remove" : "removed"} ${message}`);
  const dir = dataDir();
  const state = dataState(dir);
  const skillDir = path.join(dir, "skills", SKILL_NAME);
  let removed = 0;

  for (const [harness, root] of Object.entries(skillRoots())) {
    const link = path.join(root, SKILL_NAME);
    if (!exists(link)) continue;
    const target = linkTarget(link);
    // Only links that point into our folder: compare paths, even for a dangling link.
    const clone = state === "clone" || state === "old-clone";
    const ours = isSymlink(link) && (samePath(target, skillDir) || (clone && samePath(target, dir)));
    if (!ours) {
      console.log(`${harness}: ${displayPath(link)} is not our link, kept`);
      continue;
    }
    if (!options.dryRun) {
      fs.unlinkSync(link);
      // The skills directory itself, when the link was all it held.
      removeIfEmpty(root);
    }
    say(`${harness} link ${displayPath(link)}`);
    removed++;
  }

  if (state === "ours") {
    if (!options.dryRun) {
      fs.rmSync(skillDir, { recursive: true, force: true });
      fs.rmSync(path.join(dir, MARKER), { force: true });
      removeIfEmpty(path.join(dir, "skills"));
      removeIfEmpty(dir);
    }
    say(`the skill copy ${displayPath(skillDir)}`);
    removed++;
  } else if (state === "clone" || state === "old-clone") {
    console.log(`kept your git clone at ${displayPath(dir)}; delete it yourself if you no longer need it`);
  } else if (state === "foreign") {
    console.log(`kept ${displayPath(dir)}: it was not created by this installer`);
  }

  const configured = process.env.FIND_SESSION_CACHE || "";
  if (configured && !["0", "off", "no", "false", "none"].includes(configured.toLowerCase())) {
    console.log(`kept the search index in ${configured}: FIND_SESSION_CACHE points there, delete it yourself`);
  }
  const cache = cacheDir();
  if (isDir(cache)) {
    const names = fs.readdirSync(cache);
    const files = names.filter((name) => CACHE_FILE.test(name));
    const foreign = names.length - files.length;
    if (!options.dryRun) {
      for (const name of files) fs.rmSync(path.join(cache, name), { force: true });
      removeIfEmpty(cache);
    }
    if (files.length) {
      say(`the search index ${displayPath(cache)} (${files.length} file${files.length === 1 ? "" : "s"})`);
      removed++;
    }
    if (foreign) {
      console.log(
        `kept ${displayPath(cache)}: ${foreign} file${foreign === 1 ? "" : "s"} in it ` +
          `${foreign === 1 ? "is" : "are"} not ours (${names.find((name) => !CACHE_FILE.test(name))})`,
      );
    }
  }

  if (!removed) console.log("nothing to remove: find-session was not installed by npx here");
  console.log(`Your config (if any) stays at ${displayPath(configFile())}.`);
  return 0;
}

function configFile() {
  if (process.env.FIND_SESSION_CONFIG) return process.env.FIND_SESSION_CONFIG;
  const base = process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config");
  return path.join(base, SKILL_NAME, "config.json");
}

function removeIfEmpty(dir) {
  try {
    fs.rmdirSync(dir);
  } catch {
    // Not empty or already gone: leave it.
  }
}

// The first word that is not an option: the command (options of the helper may come before it).
function commandOf(argv) {
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--home" || argv[i] === "--book") i++;
    else if (!argv[i].startsWith("-")) return argv[i];
  }
  return null;
}

async function main(argv) {
  const [command, ...rest] = argv;
  const ownCommand = OWN_COMMANDS.includes(command);
  try {
    const help = (arg) => arg === "-h" || arg === "--help";
    if (!command || command === "help" || help(command)) {
      process.stdout.write(USAGE);
      return 0;
    }
    if (ownCommand && rest.some(help)) {
      process.stdout.write(command === "uninstall" ? UNINSTALL_USAGE : INSTALL_USAGE(command));
      return 0;
    }
    if (command === "install") return await install(rest, { refresh: false });
    if (command === "update") return await install(rest, { refresh: true });
    if (command === "uninstall") return uninstall(rest);
  } catch (error) {
    if (error instanceof UsageError) {
      console.error(`find-session ${command}: ${error.message}\nRun: npx find-session ${command} --help`);
      return 2;
    }
    throw error;
  }
  const named = commandOf(argv);
  if (named && ![...HELPER_COMMANDS, ...LEGACY_COMMANDS].includes(named)) {
    // The helper would list only its own commands; here there are three more.
    console.error(
      `find-session: unknown command '${named}'. Commands: ${[...OWN_COMMANDS, ...HELPER_COMMANDS].join(", ")}.\n` +
        "Run: npx find-session --help",
    );
    return 2;
  }
  const python = requirePython();
  if (!python) return process.exitCode;
  // Prefer the installed copy of this same version: the paths it prints stay valid
  // after npm clears its cache. Otherwise its hints name `npx find-session`.
  const dir = dataDir();
  const installed = path.join(dir, "skills", SKILL_NAME);
  if (dataState(dir) === "ours" && readMarker(dir).version === VERSION && exists(path.join(installed, "SKILL.md"))) {
    return runHelper(python, installed, argv);
  }
  return runHelper(python, PACKAGE_SKILL, argv, { FIND_SESSION_COMMAND: "npx find-session" });
}

// `npx find-session ... | head`: the reader went away, which is not an error.
process.stdout.on("error", (error) => {
  if (error.code === "EPIPE") process.exit(0);
  throw error;
});

main(process.argv.slice(2)).then(
  (code) => {
    process.exitCode = code ?? 0;
  },
  (error) => {
    console.error(`find-session: ${error.stack || error}`);
    process.exitCode = 1;
  },
);
