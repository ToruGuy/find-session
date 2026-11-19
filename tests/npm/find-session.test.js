// Tests of the npx wrapper (npm/find-session.js). No network, no real home:
// every run gets a temporary HOME and an environment without XDG/find-session
// variables. Run: node --test tests/npm/find-session.test.js
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const REPO = path.resolve(__dirname, "..", "..");
const BIN = path.join(REPO, "npm", "find-session.js");
const VERSION = fs.readFileSync(path.join(REPO, "VERSION"), "utf8").trim();
const POSIX = process.platform !== "win32";

function tempHome(t, dirs = [".claude", ".codex"]) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "find-session-npm-"));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  for (const dir of dirs) fs.mkdirSync(path.join(home, dir), { recursive: true });
  return home;
}

// Only what the wrapper needs: nothing from the developer's environment that
// could point at the real ~/.claude, ~/.codex, ~/.omp or ~/.cache.
function cleanEnv(home, extra = {}) {
  const env = { PATH: process.env.PATH, HOME: home, USERPROFILE: home, LANG: "C.UTF-8" };
  if (process.env.SYSTEMROOT) env.SYSTEMROOT = process.env.SYSTEMROOT;
  return { ...env, ...extra };
}

function run(home, args, extra = {}) {
  const result = spawnSync(process.execPath, [BIN, ...args], { env: cleanEnv(home, extra), encoding: "utf8" });
  return { code: result.status, out: result.stdout, err: result.stderr };
}

function fakePython(t, home, body) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "find-session-py-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = path.join(dir, "python3");
  fs.writeFileSync(file, `#!/bin/sh\n${body}\n`, { mode: 0o755 });
  return { dir, file };
}

function realPython() {
  for (const command of ["python3", "python"]) {
    const probe = spawnSync(command, ["-c", "import sys; print(sys.version_info >= (3, 9))"], { encoding: "utf8" });
    if (probe.status === 0 && probe.stdout.trim() === "True") return command;
  }
  return null;
}

const copyDir = (home) => path.join(home, ".local", "share", "find-session");
const skillCopy = (home) => path.join(copyDir(home), "skills", "find-session");
const link = (home, harness) =>
  path.join(home, { claude: ".claude/skills", codex: ".codex/skills", omp: ".omp/agent/skills" }[harness], "find-session");

const PYTHON = realPython();
const needsPython = { skip: PYTHON ? false : "no Python 3.9+ on PATH" };

test("install copies the skill to a stable place and links the tools that exist", needsPython, (t) => {
  const home = tempHome(t);
  const { code, out } = run(home, ["install"]);
  assert.equal(code, 0, out);
  assert.ok(fs.existsSync(path.join(skillCopy(home), "SKILL.md")));
  assert.equal(fs.readFileSync(path.join(skillCopy(home), "VERSION"), "utf8").trim(), VERSION);
  assert.ok(fs.existsSync(path.join(copyDir(home), ".installed-by-npm.json")));
  for (const harness of ["claude", "codex"]) {
    assert.ok(fs.lstatSync(link(home, harness)).isSymbolicLink());
    assert.equal(fs.realpathSync(link(home, harness)), fs.realpathSync(skillCopy(home)));
  }
  // omp is not on this machine: skipped, nothing created.
  assert.match(out, /omp: skipped/);
  assert.equal(fs.existsSync(path.join(home, ".omp")), false);
  // Then it shows what it sees, through the copied helper.
  assert.match(out, /Claude: 0 sessions/);
});

test("install is idempotent", needsPython, (t) => {
  const home = tempHome(t);
  assert.equal(run(home, ["install"]).code, 0);
  const second = run(home, ["install"]);
  assert.equal(second.code, 0, second.out);
  assert.match(second.out, /up to date/);
  assert.match(second.out, /claude: already linked/);
  assert.match(second.out, /codex: already linked/);
  assert.equal(fs.existsSync(path.join(home, ".claude", "skill-backups")), false);
  assert.deepEqual(fs.readdirSync(path.join(home, ".claude", "skills")), ["find-session"]);
});

test("update and a newer package refresh the copy", needsPython, (t) => {
  const home = tempHome(t);
  assert.equal(run(home, ["install"]).code, 0);
  const skillFile = path.join(skillCopy(home), "SKILL.md");
  fs.writeFileSync(skillFile, "edited");
  assert.equal(run(home, ["update"]).code, 0);
  assert.notEqual(fs.readFileSync(skillFile, "utf8"), "edited");

  const marker = path.join(copyDir(home), ".installed-by-npm.json");
  fs.writeFileSync(marker, JSON.stringify({ package: "find-session", version: "0.0.1" }));
  const { code, out } = run(home, ["install"]);
  assert.equal(code, 0);
  assert.match(out, new RegExp(`copied 0\\.0\\.1 -> ${VERSION.replace(/\./g, "\\.")}`));
});

test("--harness installs exactly the named tool", needsPython, (t) => {
  const home = tempHome(t, []);
  const { code, out } = run(home, ["install", "--harness", "omp"]);
  assert.equal(code, 0, out);
  assert.equal(fs.realpathSync(link(home, "omp")), fs.realpathSync(skillCopy(home)));
  assert.equal(fs.existsSync(path.join(home, ".claude")), false);
});

test("PI_CODING_AGENT_DIR moves the omp link like omp itself does", needsPython, (t) => {
  const home = tempHome(t, []);
  const agent = path.join(home, "custom-agent");
  fs.mkdirSync(agent);
  const { code } = run(home, ["install"], { PI_CODING_AGENT_DIR: agent });
  assert.equal(code, 0);
  assert.ok(fs.lstatSync(path.join(agent, "skills", "find-session")).isSymbolicLink());
});

test("without any tool, install changes nothing and says why", needsPython, (t) => {
  const home = tempHome(t, []);
  const { code, err } = run(home, ["install"]);
  assert.equal(code, 1);
  assert.match(err, /no Claude Code, Codex or omp directory found/);
  assert.deepEqual(fs.readdirSync(home), []);
});

test("--dry-run changes nothing", needsPython, (t) => {
  const home = tempHome(t);
  const { code, out } = run(home, ["install", "--dry-run"]);
  assert.equal(code, 0, out);
  assert.match(out, /would copy/);
  assert.match(out, /would link/);
  assert.equal(fs.existsSync(path.join(home, ".local")), false);
  assert.equal(fs.existsSync(path.join(home, ".claude", "skills")), false);
});

test("a different skill with the same name is kept unless --replace, which backs it up", needsPython, (t) => {
  const home = tempHome(t);
  const existing = link(home, "claude");
  fs.mkdirSync(existing, { recursive: true });
  fs.writeFileSync(path.join(existing, "SKILL.md"), "someone else's skill");

  const first = run(home, ["install"]);
  assert.equal(first.code, 1);
  assert.match(first.out, /conflict/);
  assert.equal(fs.lstatSync(existing).isSymbolicLink(), false);
  assert.ok(fs.lstatSync(link(home, "codex")).isSymbolicLink());

  const second = run(home, ["install", "--replace"]);
  assert.equal(second.code, 0, second.out);
  assert.ok(fs.lstatSync(existing).isSymbolicLink());
  const backups = fs.readdirSync(path.join(home, ".claude", "skill-backups"));
  assert.equal(backups.length, 1);
  assert.equal(
    fs.readFileSync(path.join(home, ".claude", "skill-backups", backups[0], "SKILL.md"), "utf8"),
    "someone else's skill",
  );
});

test("a folder at the copy location that is not ours is never overwritten or deleted", needsPython, (t) => {
  const home = tempHome(t);
  const mine = path.join(copyDir(home), "notes.txt");
  fs.mkdirSync(copyDir(home), { recursive: true });
  fs.writeFileSync(mine, "keep me");
  const install = run(home, ["install"]);
  assert.equal(install.code, 1);
  assert.match(install.err, /not created by this installer/);
  assert.equal(fs.existsSync(link(home, "claude")), false);
  const uninstall = run(home, ["uninstall"]);
  assert.equal(uninstall.code, 0);
  assert.equal(fs.readFileSync(mine, "utf8"), "keep me");
});

test("a git clone at the copy location is used, not copied over, and kept on uninstall", { skip: !PYTHON || !POSIX }, (t) => {
  const home = tempHome(t);
  const clone = copyDir(home);
  fs.mkdirSync(path.join(clone, ".git"), { recursive: true });
  assert.equal(spawnSync("cp", ["-R", path.join(REPO, "skills"), path.join(clone, "skills")]).status, 0);
  // A link from the old layout (repository root) is moved to the skill folder.
  fs.mkdirSync(path.dirname(link(home, "codex")), { recursive: true });
  fs.symlinkSync(clone, link(home, "codex"));

  const install = run(home, ["install"]);
  assert.equal(install.code, 0, install.out);
  assert.match(install.out, /using your git clone/);
  assert.match(install.out, /codex: relinked/);
  assert.equal(fs.existsSync(path.join(clone, ".installed-by-npm.json")), false);
  assert.equal(fs.realpathSync(link(home, "claude")), fs.realpathSync(path.join(clone, "skills", "find-session")));

  const uninstall = run(home, ["uninstall"]);
  assert.equal(uninstall.code, 0);
  assert.equal(fs.existsSync(link(home, "claude")), false);
  assert.match(uninstall.out, /kept your git clone/);
  assert.ok(fs.existsSync(path.join(clone, "skills", "find-session", "SKILL.md")));
});

test("a clone of the old layout (no skills/ folder) gets update advice and is left alone", needsPython, (t) => {
  const home = tempHome(t);
  const clone = copyDir(home);
  fs.mkdirSync(path.join(clone, ".git"), { recursive: true });
  fs.writeFileSync(path.join(clone, "SKILL.md"), "old layout");
  fs.mkdirSync(path.dirname(link(home, "claude")), { recursive: true });
  fs.symlinkSync(clone, link(home, "claude"));

  const install = run(home, ["install"]);
  assert.equal(install.code, 1);
  assert.match(install.err, /git -C ~\/\.local\/share\/find-session pull/);
  assert.equal(fs.readlinkSync(link(home, "claude")), clone);

  const uninstall = run(home, ["uninstall"]);
  assert.equal(uninstall.code, 0);
  assert.equal(fs.lstatSync(link(home, "claude"), { throwIfNoEntry: false }), undefined);
  assert.equal(fs.readFileSync(path.join(clone, "SKILL.md"), "utf8"), "old layout");
});

// A session file with one message, so that the helper has something to index.
function writeSession(home, name, text) {
  const file = path.join(home, ".claude", "projects", "-Users-alex-shop", `${name}.jsonl`);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const rows = [
    { timestamp: "2026-09-22T10:00:00Z", cwd: "/Users/alex/shop", message: { role: "user", content: text } },
    { timestamp: "2026-09-22T10:00:05Z", message: { role: "assistant", content: [{ type: "text", text: "Done." }] } },
  ];
  fs.writeFileSync(file, rows.map((row) => JSON.stringify(row) + "\n").join(""));
}

test("uninstall removes the index the helper really wrote, and the folder with it", needsPython, (t) => {
  const home = tempHome(t);
  writeSession(home, "indexed", "UNINSTALLTOPIC in a session");
  const install = run(home, ["install"]);
  assert.equal(install.code, 0, install.out + install.err);
  // The names are the helper's own: nothing here spells them out.
  const cache = path.join(home, ".cache", "find-session");
  const written = fs.readdirSync(cache);
  assert.ok(written.length >= 1, "install builds the index");
  assert.match(run(home, ["search", "UNINSTALLTOPIC"]).out, /Claude 1/);

  const preview = run(home, ["uninstall", "--dry-run"]);
  assert.equal(preview.code, 0);
  assert.match(preview.out, /dry-run: would remove the search index ~\/\.cache\/find-session \(\d+ files?\)/);
  assert.deepEqual(fs.readdirSync(cache), written);

  const { code, out } = run(home, ["uninstall"]);
  assert.equal(code, 0, out);
  assert.match(out, /removed the search index ~\/\.cache\/find-session/);
  assert.doesNotMatch(out, /not ours/);
  assert.equal(fs.existsSync(cache), false, "no copy of the conversations is left behind");
  // The skills directories install created are gone too, the tools' own folders stay.
  assert.equal(fs.existsSync(path.join(home, ".claude", "skills")), false);
  assert.ok(fs.existsSync(path.join(home, ".claude", "projects")));

  const again = run(home, ["uninstall"]);
  assert.equal(again.code, 0);
  assert.match(again.out, /nothing to remove/);
  assert.doesNotMatch(again.out, /not ours/);
});

test("uninstall removes only our links, our copy and our cache files", needsPython, (t) => {
  const home = tempHome(t, [".claude", ".codex", ".omp"]);
  assert.equal(run(home, ["install", "--no-index"]).code, 0);
  // codex: replaced by the user with a link to some other skill; omp: a real folder.
  const other = path.join(home, "other-skill");
  fs.mkdirSync(other);
  fs.unlinkSync(link(home, "codex"));
  fs.symlinkSync(other, link(home, "codex"));
  fs.unlinkSync(link(home, "omp"));
  fs.mkdirSync(link(home, "omp"));
  // Every file the helper may leave in its cache folder (the index with the files SQLite keeps
  // next to it, and the caches of older versions), plus files that are not ours.
  const cache = path.join(home, ".cache", "find-session");
  fs.mkdirSync(cache, { recursive: true });
  const ours = [
    "index-0123abcd-py312.sqlite3",
    "index-0123abcd-py312.sqlite3-wal",
    "index-0123abcd-py312.sqlite3-shm",
    "index-0123abcd-py312.sqlite3-journal",
    "index-89abcdef-py39.sqlite3",
    "sessions-0123456789ab-py312.bin",
    "sessions-0123456789ab.json",
  ];
  const theirs = ["index.html", "index-notes.sqlite3", "unrelated.txt"];
  for (const name of [...ours, ...theirs]) fs.writeFileSync(path.join(cache, name), "keep");
  const config = path.join(home, ".config", "find-session", "config.json");
  fs.mkdirSync(path.dirname(config), { recursive: true });
  fs.writeFileSync(config, "{}");

  const preview = run(home, ["uninstall", "--dry-run"]);
  assert.equal(preview.code, 0);
  assert.ok(fs.existsSync(link(home, "claude")));
  assert.equal(fs.readdirSync(cache).length, ours.length + theirs.length);

  const { code, out } = run(home, ["uninstall"]);
  assert.equal(code, 0, out);
  assert.equal(fs.existsSync(link(home, "claude")), false);
  assert.equal(fs.readlinkSync(link(home, "codex")), other);
  assert.ok(fs.statSync(link(home, "omp")).isDirectory());
  assert.match(out, /codex: .* is not our link, kept/);
  assert.equal(fs.existsSync(copyDir(home)), false);
  assert.deepEqual(fs.readdirSync(cache).sort(), [...theirs].sort());
  assert.match(out, /removed the search index ~\/\.cache\/find-session \(7 files\)/);
  assert.match(out, /kept ~\/\.cache\/find-session: 3 files in it are not ours/);
  assert.ok(fs.existsSync(config));
  assert.ok(fs.existsSync(other));

  // Twice is fine, and a dangling link of ours is still recognised as ours.
  assert.equal(run(home, ["uninstall"]).code, 0);
});

test("uninstall removes our link even after the copy is gone", needsPython, (t) => {
  const home = tempHome(t);
  assert.equal(run(home, ["install"]).code, 0);
  fs.rmSync(skillCopy(home), { recursive: true });
  const { code } = run(home, ["uninstall"]);
  assert.equal(code, 0);
  assert.equal(fs.lstatSync(link(home, "claude"), { throwIfNoEntry: false }), undefined);
});

test("a cache directory chosen with FIND_SESSION_CACHE is left alone", needsPython, (t) => {
  const home = tempHome(t);
  const custom = path.join(home, "my-cache");
  fs.mkdirSync(custom);
  fs.writeFileSync(path.join(custom, "index-0123abcd-py312.sqlite3"), "");
  const { code, out } = run(home, ["uninstall"], { FIND_SESSION_CACHE: custom });
  assert.equal(code, 0);
  assert.match(out, /FIND_SESSION_CACHE/);
  assert.deepEqual(fs.readdirSync(custom), ["index-0123abcd-py312.sqlite3"]);
});

// ---------------------------------------------------------------- the rules shared with install_skill.py

const CASES = JSON.parse(fs.readFileSync(path.join(REPO, "tests", "install_cases.json"), "utf8")).cases;

function otherInstall(folder) {
  fs.mkdirSync(path.join(folder, "scripts"), { recursive: true });
  fs.writeFileSync(path.join(folder, "SKILL.md"), "---\nname: find-session\ndescription: synthetic\n---\n");
  fs.writeFileSync(path.join(folder, "scripts", "sessions.py"), "");
  fs.writeFileSync(path.join(folder, "VERSION"), "9.9.9\n");
}

// find-session as Claude Code records a plugin install.
function pluginInstall(home, plugin) {
  otherInstall(path.join(home, plugin.path, "skills", "find-session"));
  const name = "find-session@find-session";
  const entry = { scope: plugin.scope, installPath: path.join(home, plugin.path), version: "9.9.9" };
  const listing = { version: 2, plugins: { [name]: [entry], "other@market": [{ scope: "user", installPath: "/nowhere" }] } };
  fs.writeFileSync(path.join(home, ".claude", "plugins", "installed_plugins.json"), JSON.stringify(listing));
  if (plugin.disabled) {
    fs.writeFileSync(path.join(home, ".claude", "settings.json"), JSON.stringify({ enabledPlugins: { [name]: false } }));
  }
}

for (const item of CASES) {
  test(`install rule: ${item.name}`, { skip: !PYTHON || !POSIX }, (t) => {
    const home = tempHome(t, item.dirs);
    for (const folder of item.foreign || []) {
      fs.mkdirSync(path.join(home, folder), { recursive: true });
      fs.writeFileSync(path.join(home, folder, "SKILL.md"), "---\nname: something-else\n---\n");
    }
    if (item.other_install) otherInstall(path.join(home, item.other_install));
    if (item.plugin) pluginInstall(home, item.plugin);
    for (const [from, to] of Object.entries(item.links || {})) {
      fs.mkdirSync(path.dirname(path.join(home, from)), { recursive: true });
      fs.symlinkSync(path.join(home, to), path.join(home, from));
    }

    const { code, out, err } = run(home, ["install", "--no-index", ...item.args]);

    assert.equal(code, item.exit, out + err);
    // The lines per tool come first; the summary printed after them has lines that look alike.
    const lines = out
      .split("\n\n")[0]
      .split("\n")
      .filter((line) => /^(dry-run: )?(claude|codex|omp): /.test(line));
    const expected = item.out.map((line) => line.replace("{skill}", "~/.local/share/find-session/skills/find-session"));
    assert.equal(lines.length, expected.length, out);
    lines.forEach((line, index) => {
      if (expected[index].endsWith("*")) assert.ok(line.startsWith(expected[index].slice(0, -1)), line);
      else assert.equal(line, expected[index]);
    });
    if (item.stderr) assert.ok(err.includes(item.stderr), err);
    for (const harness of ["claude", "codex", "omp"]) {
      const target = fs.existsSync(link(home, harness)) ? fs.realpathSync(link(home, harness)) : null;
      const ours = target !== null && fs.existsSync(skillCopy(home)) && target === fs.realpathSync(skillCopy(home));
      assert.equal(ours, item.linked.includes(harness), `${harness} link`);
    }
    for (const gone of item.absent || []) assert.equal(fs.existsSync(path.join(home, gone)), false, gone);
  });
}

test("install shows what it sees, then builds the index last; --no-index skips it", needsPython, (t) => {
  const home = tempHome(t);
  writeSession(home, "first", "ORDERTOPIC");
  const { code, out, err } = run(home, ["install"]);
  assert.equal(code, 0, out + err);
  const order = ["claude: linked", "What the skill can see:", "Claude: 1 session,", "Indexing your history", "Index: 1 session files", "Done."];
  const places = order.map((text) => out.indexOf(text));
  assert.ok(places.every((place) => place >= 0), out);
  assert.deepEqual([...places].sort((a, b) => a - b), places, out);
  assert.ok(fs.readdirSync(path.join(home, ".cache", "find-session")).length >= 1);

  const other = tempHome(t);
  writeSession(other, "first", "ORDERTOPIC");
  const skipped = run(other, ["install", "--no-index"]);
  assert.equal(skipped.code, 0);
  assert.doesNotMatch(skipped.out, /Indexing your history/);
  assert.equal(fs.existsSync(path.join(other, ".cache")), false);
});

test("a package older than what is installed keeps the newer copy and says so", needsPython, (t) => {
  const home = tempHome(t);
  assert.equal(run(home, ["install", "--no-index"]).code, 0);
  const marker = path.join(copyDir(home), ".installed-by-npm.json");
  const skillFile = path.join(skillCopy(home), "SKILL.md");
  const newer = "---\nname: find-session\n---\nfrom the future\n";
  fs.writeFileSync(marker, JSON.stringify({ package: "find-session", version: "99.0.0" }));
  fs.writeFileSync(skillFile, newer);
  for (const command of ["install", "update"]) {
    const { code, out } = run(home, [command, "--no-index"]);
    assert.equal(code, 0, out);
    assert.match(out, /is 99\.0\.0, newer than this package/);
    assert.match(out, new RegExp(`npx find-session@latest ${command}`));
    assert.doesNotMatch(out, /copied/);
    assert.equal(fs.readFileSync(skillFile, "utf8"), newer);
    assert.equal(JSON.parse(fs.readFileSync(marker, "utf8")).version, "99.0.0");
  }
  // Commands still run, from the package's own copy.
  assert.equal(run(home, ["--version"]).out.trim(), `find-session ${VERSION}`);
});

test("the wrapper and the helper agree on the helper's commands", needsPython, (t) => {
  const source = fs.readFileSync(BIN, "utf8");
  const listed = JSON.parse(/const HELPER_COMMANDS = (\[[^\]]*\]);/.exec(source)[1]);
  const probe = spawnSync(
    PYTHON,
    ["-c", "import json, sys; sys.path.insert(0, sys.argv[1]); from find_session import cli; print(json.dumps(list(cli.COMMANDS)))", path.join(REPO, "skills", "find-session", "scripts")],
    { encoding: "utf8" },
  );
  assert.equal(probe.status, 0, probe.stderr);
  assert.deepEqual(listed, JSON.parse(probe.stdout));
});

test("an unknown command lists all commands, the wrapper's too", (t) => {
  const home = tempHome(t);
  const { code, err } = run(home, ["serach", "x"]);
  assert.equal(code, 2);
  assert.match(err, /unknown command 'serach'\. Commands: install, update, uninstall, search, inspect, recent, index, sources, build-index\./);
});

test("usage errors of the helper name the command that was typed", needsPython, (t) => {
  const home = tempHome(t);
  const { code, err } = run(home, ["search"]);
  assert.equal(code, 2);
  assert.match(err, /usage: npx find-session search /);
  assert.match(err, /npx find-session search: error: /);
});

test("a reader that is gone ends the wrapper quietly", { skip: !POSIX }, async (t) => {
  const home = tempHome(t);
  const { spawn } = require("node:child_process");
  const child = spawn(process.execPath, [BIN, "--help"], { env: cleanEnv(home), stdio: ["ignore", "pipe", "pipe"] });
  // Closed before the wrapper has written anything: its first write finds no reader.
  child.stdout.destroy();
  let err = "";
  child.stderr.on("data", (chunk) => (err += chunk));
  const code = await new Promise((resolve) => child.on("close", resolve));
  assert.equal(code, 0, err);
  assert.equal(err, "");
});

test("when every tool already has find-session from another installer, no second copy is made", needsPython, (t) => {
  const home = tempHome(t);
  const clone = path.join(home, "src", "find-session", "skills", "find-session");
  otherInstall(clone);
  for (const harness of ["claude", "codex"]) {
    fs.mkdirSync(path.dirname(link(home, harness)), { recursive: true });
    fs.symlinkSync(clone, link(home, harness));
  }
  const { code, out } = run(home, ["install", "--no-index"]);
  assert.equal(code, 0, out);
  assert.match(out, /skill: not copied, every tool here already has find-session/);
  assert.match(out, /claude: kept, already installed from ~\/src\/find-session\/skills\/find-session \(9\.9\.9\)/);
  assert.match(out, /Claude: 0 sessions/);
  assert.equal(fs.existsSync(copyDir(home)), false);
  // --replace is the way to move them to the npx copy.
  const replaced = run(home, ["install", "--no-index", "--replace"]);
  assert.equal(replaced.code, 0, replaced.out);
  assert.equal(fs.realpathSync(link(home, "claude")), fs.realpathSync(skillCopy(home)));
});

test("other commands go to the Python helper with arguments untouched", { skip: !POSIX }, (t) => {
  const home = tempHome(t);
  const { file } = fakePython(
    t,
    home,
    `if [ "$1" = "-c" ]; then echo 3.12.1; exit 0; fi
printf '%s\\n' "command=$FIND_SESSION_COMMAND"
for arg in "$@"; do printf '%s\\n' "$arg"; done
exit 7`,
  );
  const { code, out } = run(home, ["search", "two words", "--json", "--since", "3d"], { FIND_SESSION_PYTHON: file });
  assert.equal(code, 7);
  const [command, helper, ...args] = out.trim().split("\n");
  // Not installed: the package's own copy, and hints that name the npx command.
  assert.equal(helper, path.join(REPO, "skills", "find-session", "scripts", "sessions.py"));
  assert.equal(command, "command=npx find-session");
  assert.deepEqual(args, ["search", "two words", "--json", "--since", "3d"]);
});

test("once installed, commands run the stable copy", needsPython, (t) => {
  const home = tempHome(t);
  assert.equal(run(home, ["install"]).code, 0);
  const { file } = fakePython(
    t,
    home,
    `if [ "$1" = "-c" ]; then echo 3.12.1; exit 0; fi
printf '%s\\n' "command=$FIND_SESSION_COMMAND" "$1"`,
  );
  const { code, out } = run(home, ["recent"], { FIND_SESSION_PYTHON: file });
  assert.equal(code, 0);
  assert.deepEqual(out.trim().split("\n"), ["command=", path.join(skillCopy(home), "scripts", "sessions.py")]);
});

test("--version reaches the real helper", needsPython, (t) => {
  const home = tempHome(t);
  const { code, out } = run(home, ["--version"]);
  assert.equal(code, 0);
  assert.equal(out.trim(), `find-session ${VERSION}`);
});

test("help is printed by the wrapper", (t) => {
  const home = tempHome(t);
  for (const args of [[], ["--help"], ["help"]]) {
    const { code, out } = run(home, args);
    assert.equal(code, 0);
    assert.match(out, /Usage: npx find-session <command>/);
    assert.match(out, /uninstall\s+remove our links/);
    for (const command of ["search", "inspect", "recent", "index", "sources", "build-index"]) {
      assert.match(out, new RegExp(`\\b${command}\\b`));
    }
  }
  // Each of the wrapper's own commands explains its own options.
  for (const command of ["install", "update"]) {
    const { code, out } = run(home, [command, "--help"]);
    assert.equal(code, 0);
    assert.match(out, new RegExp(`Usage: npx find-session ${command} \\[options\\]`));
    for (const option of ["--harness", "--replace", "--no-index", "--dry-run"]) assert.ok(out.includes(option), option);
  }
  const uninstall = run(home, ["uninstall", "-h"]);
  assert.match(uninstall.out, /Usage: npx find-session uninstall \[--dry-run\]/);
  assert.match(uninstall.out, /the search index in ~\/\.cache\/find-session/);
  const bad = run(home, ["install", "--bogus"]);
  assert.equal(bad.code, 2);
  assert.match(bad.err, /unknown option --bogus/);
  assert.match(bad.err, /npx find-session install --help/);
});

test("missing Python: clear message, exit code 3, nothing installed", { skip: !POSIX }, (t) => {
  const home = tempHome(t);
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), "find-session-nopath-"));
  t.after(() => fs.rmSync(empty, { recursive: true, force: true }));
  for (const args of [["search", "x"], ["install"]]) {
    const { code, err } = run(home, args, { PATH: empty });
    assert.equal(code, 3);
    assert.match(err, /Python 3\.9 or newer is needed/);
    assert.match(err, /no python3 or python was found/);
  }
  assert.equal(fs.existsSync(path.join(home, ".local")), false);
  assert.equal(fs.existsSync(path.join(home, ".claude", "skills")), false);
});

test("Python older than 3.9: names the version found, exit code 3", { skip: !POSIX }, (t) => {
  const home = tempHome(t);
  const { file } = fakePython(t, home, "echo 3.8.10");
  const { code, err } = run(home, ["search", "x"], { FIND_SESSION_PYTHON: file });
  assert.equal(code, 3);
  assert.match(err, /Python 3\.8\.10/);
});

test("the npm package ships the skill, without caches or tests, at the repository version", (t) => {
  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  // Leftovers of the Python tests and of `uv build` must not end up in the tarball.
  if (PYTHON) spawnSync(PYTHON, ["-m", "compileall", "-q", path.join(REPO, "skills")]);
  const eggInfo = path.join(REPO, "skills", "find-session", "scripts", "test_leftover.egg-info");
  if (!fs.existsSync(eggInfo)) {
    fs.mkdirSync(eggInfo);
    fs.writeFileSync(path.join(eggInfo, "PKG-INFO"), "");
    t.after(() => fs.rmSync(eggInfo, { recursive: true, force: true }));
  }
  const packed = spawnSync(npm, ["pack", "--dry-run", "--json", "--ignore-scripts"], {
    cwd: REPO,
    encoding: "utf8",
    env: { ...process.env, npm_config_loglevel: "silent" },
  });
  if (packed.error) return t.skip("npm not available");
  assert.equal(packed.status, 0, packed.stderr);
  const [info] = JSON.parse(packed.stdout);
  const files = info.files.map((file) => file.path);
  for (const expected of [
    "npm/find-session.js",
    "package.json",
    "README.md",
    "LICENSE",
    "skills/find-session/SKILL.md",
    "skills/find-session/VERSION",
    "skills/find-session/scripts/sessions.py",
    "skills/find-session/scripts/find_session/cli.py",
    "skills/find-session/scripts/find_session/parsers/omp.py",
  ]) {
    assert.ok(files.includes(expected), `${expected} missing from the package`);
  }
  assert.deepEqual(
    files.filter((file) => /__pycache__|\.pyc$|egg-info|^tests\/|^scripts\/|^\.github\//.test(file)),
    [],
  );
  assert.equal(info.version, VERSION);
  const bin = info.files.find((file) => file.path === "npm/find-session.js");
  assert.ok(bin.mode & 0o111, "npm/find-session.js must be executable");
});
