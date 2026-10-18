#!/usr/bin/env node
"use strict";

// Persistent wrapper around the pinned Sentinel runner.
//
// `sentinel-runner.js` is a vendored, SHA-256-pinned CLI. This file is ours: it
// loads that module once and serves one JSON request per stdin line, so a batch
// pays Node's startup and module load once instead of once per token.
//
//   request:  {"id": 1, "args": ["--config", "...", "--challenge-file", "..."]}
//   response: {"id": 1, "ok": true, "text": "<token JSON>"}
//             {"id": 1, "ok": false, "error": "..."}
//
// Requirements are serialized: the runner builds an isolated VM context per
// call, but concurrent calls would interleave their stdout/timezone setup, so a
// promise chain keeps one token in flight at a time.

const path = require("node:path");
const readline = require("node:readline");

const runnerPath = process.argv[2];
if (!runnerPath) {
    process.stderr.write("usage: sentinel-worker.js <sentinel-runner.js>\n");
    process.exit(2);
}

let runner;
try {
    runner = require(path.resolve(runnerPath));
} catch (error) {
    process.stderr.write(`cannot load runner: ${error && error.message ? error.message : error}\n`);
    process.exit(2);
}
if (typeof runner.main !== "function") {
    process.stderr.write("runner does not export main()\n");
    process.exit(2);
}

function respond(payload) {
    process.stdout.write(`${JSON.stringify(payload)}\n`);
}

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
let chain = Promise.resolve();
let stopping = false;

input.on("line", (line) => {
    if (!line.trim()) return;
    chain = chain.then(() => handle(line));
});

input.on("close", () => {
    stopping = true;
    chain.finally(() => process.exit(0));
});

async function handle(line) {
    let request;
    try {
        request = JSON.parse(line);
    } catch {
        respond({ id: 0, ok: false, error: "invalid_request" });
        return;
    }
    const id = typeof request.id === "number" ? request.id : 0;
    try {
        const args = Array.isArray(request.args) ? request.args.map(String) : [];
        // writeOutput=false: the caller wants the token text, not a stdout line.
        const text = await runner.main(args, false);
        respond({ id, ok: true, text: typeof text === "string" ? text : String(text ?? "") });
    } catch (error) {
        const message = error && error.message ? error.message : String(error ?? "unknown error");
        respond({ id, ok: false, error: message.slice(0, 500) });
    }
    if (stopping) process.exit(0);
}
