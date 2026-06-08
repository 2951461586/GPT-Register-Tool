#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

const ROOT = path.resolve(__dirname, "../..");
const HOME = process.env.HOME || process.env.USERPROFILE || "";
const DEFAULT_CPA_ROOT = process.env.CPA_ROOT || path.resolve(ROOT, "../CLIProxyAPI.git");
const DEFAULT_RUNTIME_DIR = process.env.KYL_PROTOCOL_RUNTIME || path.join(__dirname, "runtime");

function argValue(name, fallback = "") {
  const prefix = `--${name}=`;
  const hit = process.argv.slice(2).find((arg) => arg.startsWith(prefix));
  if (hit) return hit.slice(prefix.length);
  const i = process.argv.indexOf(`--${name}`);
  if (i >= 0 && process.argv[i + 1]) return process.argv[i + 1];
  return fallback;
}

function intValue(name, fallback) {
  const raw = argValue(name, process.env[name.toUpperCase().replaceAll("-", "_")] || String(fallback));
  const n = Number(raw);
  return Number.isFinite(n) ? n : fallback;
}

function boolValue(name, fallback = false) {
  const raw = argValue(name, process.env[name.toUpperCase().replaceAll("-", "_")] || "");
  if (!raw) return fallback;
  return /^(1|true|yes|on)$/i.test(raw);
}

function usage() {
  console.log(`Usage:
  node tools/kyl-protocol/run_kyl_protocol_batch.js --state <state.json> [options]

Options:
  --state <file>             Account state JSON. Defaults to STATE_PATH.
  --auth-dir <dir>           Auth output dir. Defaults to CPA_ROOT/auths.
  --cookies <file>           CDP exported cookies JSON. Defaults to CDP_COOKIE_PATH or ./runtime/cookies.json.
  --fingerprint <value>      KYL fingerprint. Defaults to state.fingerprint or KYL_FINGERPRINT.
  --start <n>                Start index. Defaults to START_INDEX or 0.
  --limit <n>                Max pending accounts. 0 means no limit.
  --workers <n>              Parallel protocol workers. Defaults to PROTOCOL_WORKERS or 2.
  --status <file>            JSONL status log path.
  --replay-script <file>     Python replay script. Defaults to kyl_protocol_replay.py.
  --python <bin>             Python executable. Defaults to python3.
  --include-existing=1       Process accounts even if auth file already exists.

Example:
  STATE_PATH=/path/to/state.json \\
  CDP_COOKIE_PATH=/path/to/cookies.json \\
  KYL_FINGERPRINT=kyl-fp-... \\
  node run_kyl_protocol_batch.js --workers 2
`);
}

if (process.argv.includes("--help") || process.argv.includes("-h")) {
  usage();
  process.exit(0);
}

function expandHome(p) {
  if (!p) return p;
  if (p === "~") return HOME;
  if (p.startsWith("~/")) return path.join(HOME, p.slice(2));
  return p;
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

function listExistingEmails(authDir) {
  const out = new Set();
  if (!fs.existsSync(authDir)) return out;
  for (const name of fs.readdirSync(authDir)) {
    if (!name.endsWith(".json")) continue;
    const file = path.join(authDir, name);
    try {
      const data = readJson(file);
      if (data.email) out.add(String(data.email));
    } catch {
      // Ignore partial or unrelated files.
    }
  }
  return out;
}

function log(rec) {
  const row = JSON.stringify({ ts: new Date().toISOString(), ...rec });
  if (STATUS_PATH) {
    fs.mkdirSync(path.dirname(STATUS_PATH), { recursive: true });
    fs.appendFileSync(STATUS_PATH, `${row}\n`);
  }
  console.log(row);
}

const stateArg = expandHome(argValue("state", process.env.STATE_PATH || ""));
if (!stateArg) {
  console.error("Missing --state/STATE_PATH");
  usage();
  process.exit(2);
}
const STATE_PATH = path.resolve(stateArg);
if (!fs.existsSync(STATE_PATH)) {
  console.error(`State file does not exist: ${STATE_PATH}`);
  process.exit(2);
}

const AUTH_DIR = path.resolve(expandHome(argValue("auth-dir", process.env.AUTH_DIR || path.join(DEFAULT_CPA_ROOT, "auths"))));
const CDP_COOKIE_PATH = path.resolve(expandHome(argValue("cookies", process.env.CDP_COOKIE_PATH || path.join(DEFAULT_RUNTIME_DIR, "cookies.json"))));
const REPLAY_SCRIPT = path.resolve(expandHome(argValue("replay-script", process.env.PROTOCOL_REPLAY_SCRIPT || path.join(__dirname, "kyl_protocol_replay.py"))));
const PYTHON = argValue("python", process.env.PYTHON || "python3");
const START_INDEX = intValue("start", Number(process.env.START_INDEX || 0));
const LIMIT = intValue("limit", Number(process.env.LIMIT || 0));
const WORKERS = Math.max(1, intValue("workers", Number(process.env.PROTOCOL_WORKERS || 2)));
const STATUS_PATH = path.resolve(expandHome(argValue("status", process.env.PROTOCOL_STATUS_PATH || path.join(DEFAULT_RUNTIME_DIR, "protocol-batch.jsonl"))));
const INCLUDE_EXISTING = boolValue("include-existing", false);

const state = readJson(STATE_PATH);
const fingerprint = argValue("fingerprint", process.env.KYL_FINGERPRINT || state.fingerprint || "");
if (!fingerprint) {
  console.error("Missing KYL fingerprint. Set --fingerprint, KYL_FINGERPRINT, or state.fingerprint.");
  process.exit(2);
}

const accounts = Array.isArray(state.accounts) ? state.accounts : [];
const existing = listExistingEmails(AUTH_DIR);
const pending = [];
for (let i = START_INDEX; i < accounts.length; i += 1) {
  const account = accounts[i];
  if (!account || !account.email || !account.sub) continue;
  if (!INCLUDE_EXISTING && existing.has(String(account.email))) continue;
  pending.push({ index: i, account });
  if (LIMIT && pending.length >= LIMIT) break;
}

let cursor = 0;
let done = 0;
let skipped = accounts.length - pending.length;
let failed = 0;
const failures = [];

function next() {
  if (cursor >= pending.length) return null;
  return pending[cursor++];
}

function runOne(item, workerId) {
  return new Promise((resolve) => {
    const env = {
      ...process.env,
      STATE_PATH,
      AUTH_DIR,
      CDP_COOKIE_PATH,
      WORK_DIR: process.env.WORK_DIR || DEFAULT_RUNTIME_DIR,
      KYL_COOKIE_PATH: process.env.KYL_COOKIE_PATH || path.join(DEFAULT_RUNTIME_DIR, "kyl-protocol-cookies.json"),
      KYL_FINGERPRINT: fingerprint,
      ACCOUNT_INDEX: String(item.index),
      PROTOCOL_STATUS_PATH: STATUS_PATH,
      DIRECT_CODEX_SAVE: process.env.DIRECT_CODEX_SAVE || "1",
      CASDOOR_BRIDGE: process.env.CASDOOR_BRIDGE || "kyl",
      HTTP_IMPERSONATE: process.env.HTTP_IMPERSONATE || "chrome136",
    };
    log({ event: "accountProtocolStart", workerId, index: item.index, email: item.account.email, script: REPLAY_SCRIPT });
    const child = spawn(PYTHON, [REPLAY_SCRIPT], { env, stdio: ["ignore", "pipe", "pipe"] });
    child.stdout.on("data", (buf) => process.stdout.write(buf));
    child.stderr.on("data", (buf) => process.stderr.write(buf));
    child.on("close", (code) => {
      if (code === 0) {
        done += 1;
        log({ event: "accountProtocolDone", workerId, index: item.index, email: item.account.email });
      } else {
        failed += 1;
        failures.push({ index: item.index, email: item.account.email, code });
        log({ event: "accountProtocolError", workerId, index: item.index, email: item.account.email, code });
      }
      resolve();
    });
  });
}

async function worker(workerId) {
  for (;;) {
    const item = next();
    if (!item) return;
    await runOne(item, workerId);
  }
}

(async () => {
  log({
    event: "protocolBatchStart",
    state: STATE_PATH,
    authDir: AUTH_DIR,
    cookies: fs.existsSync(CDP_COOKIE_PATH),
    startIndex: START_INDEX,
    pending: pending.length,
    workers: Math.min(WORKERS, pending.length || 1),
  });
  const workerCount = Math.min(WORKERS, pending.length || 1);
  await Promise.all(Array.from({ length: workerCount }, (_, i) => worker(i + 1)));
  log({ event: "protocolBatchStop", done, failed, skipped, failures: failures.slice(0, 20) });
  process.exit(failed ? 1 : 0);
})().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
