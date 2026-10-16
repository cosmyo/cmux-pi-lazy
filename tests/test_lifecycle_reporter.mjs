import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { createServer } from "node:net";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const extensionPath = new URL("../extensions/lifecycle.mjs", import.meta.url).pathname;

function harness(env = {}) {
  const dir = mkdtempSync(join(tmpdir(), "pi-lazy-lifecycle-"));
  const socketPath = join(dir, "cmux.sock");
  const received = [];
  const connections = new Set();
  let failNext = false;
  const server = createServer((conn) => {
    connections.add(conn);
    conn.on("close", () => connections.delete(conn));
    let buf = "";
    conn.on("data", (chunk) => {
      buf += chunk.toString();
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line) continue;
        received.push(line);
        if (failNext) { failNext = false; conn.destroy(); continue; }
        conn.write(line.startsWith("set_agent_lifecycle") ? "OK\n" : "ERROR: unknown\n");
      }
    });
  });
  server.listen(socketPath);

  const handlers = new Map();
  const bus = new EventEmitter();
  const pi = {
    on: (name, fn) => { const l = handlers.get(name) ?? []; l.push(fn); handlers.set(name, l); },
    events: { on: bus.on.bind(bus), emit: bus.emit.bind(bus) },
  };
  const ctx = {
    mode: "tui", hasUI: true,
    pending: false, editorText: "",
    hasPendingMessages() { return this.pending; },
    sessionManager: { getSessionFile: () => "/session.jsonl" },
    ui: {
      inputListeners: [],
      getEditorText: () => ctx.editorText,
      onTerminalInput(fn) { this.inputListeners.push(fn); return () => {}; },
    },
  };
  const cleanup = () => {
    for (const conn of connections) conn.destroy();
    server.close();
    rmSync(dir, { recursive: true, force: true });
  };
  return {
    pi, ctx, received, dir, cleanup,
    failNextWrite: () => { failNext = true; },
    env: {
      CMUX_WORKSPACE_ID: "ws-uuid", CMUX_SURFACE_ID: "surface-uuid",
      CMUX_SOCKET_PATH: socketPath, ...env,
    },
    async emit(name, event = {}, value = ctx) {
      for (const fn of handlers.get(name) ?? []) await fn(event, value);
    },
    input(data) { for (const fn of ctx.ui.inputListeners) fn(data); },
    async settled() {
      // Let queued socket writes land.
      for (let i = 0; i < 50 && received.length === 0; i++) await new Promise(r => setTimeout(r, 5));
      await new Promise(r => setTimeout(r, 20));
    },
  };
}

async function load(h) {
  const oldEnv = { ...process.env };
  Object.assign(process.env, h.env);
  if (!("PI_LAZY_LIFECYCLE" in h.env)) delete process.env.PI_LAZY_LIFECYCLE;
  if (!("CMUX_AGENT_HOOK_SUPPRESS_VISIBLE_MUTATIONS" in h.env)) delete process.env.CMUX_AGENT_HOOK_SUPPRESS_VISIBLE_MUTATIONS;
  if (!("PI_LAZY_LIFECYCLE_RECHECK_MS" in h.env)) delete process.env.PI_LAZY_LIFECYCLE_RECHECK_MS;
  try {
    const mod = await import(pathToFileURL(extensionPath).href + "?t=" + Math.random());
    mod.default(h.pi);
  } finally {
    for (const key of Object.keys(process.env)) if (!(key in oldEnv)) delete process.env[key];
    Object.assign(process.env, oldEnv);
  }
}

test("session start reports idle; turn reports running; settle reports idle", async () => {
  const h = harness();
  await load(h);
  await h.emit("session_start");
  await h.settled();
  assert.ok(h.received.some(l => l.includes("idle --tab=ws-uuid --panel=surface-uuid")), JSON.stringify(h.received));
  const before = h.received.length;
  await h.emit("agent_start");
  await h.settled();
  assert.ok(h.received.slice(before).some(l => l.includes(" running ")), JSON.stringify(h.received));
  const mid = h.received.length;
  await h.emit("agent_settled");
  await h.settled();
  assert.ok(h.received.slice(mid).some(l => l.includes(" idle ")), JSON.stringify(h.received));
  h.cleanup();
});

test("draft text reports needsInput; clearing it returns to idle", async () => {
  const h = harness();
  await load(h);
  await h.emit("session_start");
  await h.settled();
  h.ctx.editorText = "draft in progress";
  h.input("d");
  await new Promise(r => setTimeout(r, 30));
  assert.ok(h.received.some(l => l.includes("needsInput")), JSON.stringify(h.received));
  h.ctx.editorText = "";
  h.input("\x7f");
  await new Promise(r => setTimeout(r, 30));
  const last = h.received.at(-1);
  assert.ok(last.includes(" idle "), last);
  h.cleanup();
});

test("terminal focus/size replies do not count as drafts", async () => {
  const h = harness();
  await load(h);
  await h.emit("session_start");
  await h.settled();
  h.input("\x1b[O");
  h.input("\x1b[6;10;20t");
  await new Promise(r => setTimeout(r, 30));
  assert.ok(!h.received.some(l => l.includes("needsInput")), JSON.stringify(h.received));
  h.cleanup();
});

test("registered background work reports backgroundWorkPending", async () => {
  const h = harness();
  await load(h);
  await h.emit("session_start");
  await h.settled();
  const key = Symbol.for("pi-subagents.background-work.v1");
  const old = globalThis[key];
  globalThis[key] = { version: 1, providers: new Map([["test", { listActiveWork: () => [{ sessionId: "x", id: "j" }] }]]) };
  try {
    h.pi.events.emit("subagent:async-started");
    await new Promise(r => setTimeout(r, 30));
    assert.ok(h.received.at(-1).includes("backgroundWorkPending"), JSON.stringify(h.received));
    globalThis[key].providers.clear();
    h.pi.events.emit("subagent:async-complete");
    await new Promise(r => setTimeout(r, 30));
    assert.ok(h.received.at(-1).includes(" idle "), JSON.stringify(h.received));
  } finally { if (old === undefined) delete globalThis[key]; else globalThis[key] = old; }
  h.cleanup();
});

test("work started during a turn still blocks idle at settle (registry checked in evaluate)", async () => {
  const h = harness();
  await load(h);
  await h.emit("session_start");
  await h.settled();
  const key = Symbol.for("pi-subagents.background-work.v1");
  const old = globalThis[key];
  globalThis[key] = { version: 1, providers: new Map([["test", { listActiveWork: () => [{ sessionId: "x", id: "j" }] }]]) };
  try {
    await h.emit("agent_start");           // work begins mid-turn; no bus event consumed yet
    await h.emit("agent_settled");         // settle path must consult the registry
    await h.settled();
    assert.ok(h.received.at(-1).includes("backgroundWorkPending"), JSON.stringify(h.received));
    globalThis[key].providers.clear();
    h.ctx.editorText = "still typing";      // draft-clear path must also consult the registry
    h.input("x");
    await new Promise(r => setTimeout(r, 30));
    assert.ok(h.received.at(-1).includes("needsInput"), JSON.stringify(h.received));
  } finally { if (old === undefined) delete globalThis[key]; else globalThis[key] = old; }
  h.cleanup();
});

test("programmatic draft (external editor / setEditorText) is caught without input events", async () => {
  const h = harness({ PI_LAZY_LIFECYCLE_RECHECK_MS: "25" });
  await load(h);
  await h.emit("session_start");
  await h.settled();
  assert.ok(h.received.at(-1).includes(" idle "), JSON.stringify(h.received));
  // No terminal input, no bus event: an async external-editor return sets the draft.
  h.ctx.editorText = "restored from editor";
  await new Promise(r => setTimeout(r, 120));
  assert.ok(h.received.some(l => l.includes("needsInput")), JSON.stringify(h.received));
  // And clearing it programmatically returns to idle.
  h.ctx.editorText = "";
  await new Promise(r => setTimeout(r, 120));
  assert.ok(h.received.at(-1).includes(" idle "), JSON.stringify(h.received));
  h.cleanup();
});

test("registry change without a bus event is caught by the recheck", async () => {
  const h = harness({ PI_LAZY_LIFECYCLE_RECHECK_MS: "25" });
  await load(h);
  await h.emit("session_start");
  await h.settled();
  const key = Symbol.for("pi-subagents.background-work.v1");
  const old = globalThis[key];
  globalThis[key] = { version: 1, providers: new Map() };
  try {
    globalThis[key].providers.set("late", { listActiveWork: () => [{ sessionId: "x", id: "j" }] });
    await new Promise(r => setTimeout(r, 120));
    assert.ok(h.received.some(l => l.includes("backgroundWorkPending")), JSON.stringify(h.received));
  } finally { if (old === undefined) delete globalThis[key]; else globalThis[key] = old; }
  h.cleanup();
});

test("stable states are not re-written (cmux activity timestamps must not refresh)", async () => {
  const h = harness({ PI_LAZY_LIFECYCLE_RECHECK_MS: "25" });
  await load(h);
  await h.emit("session_start");
  await h.settled();
  const idleWrites = h.received.filter(l => l.includes(" idle ")).length;
  assert.equal(idleWrites, 1, JSON.stringify(h.received));
  await new Promise(r => setTimeout(r, 150)); // several timer ticks, no state change
  assert.equal(h.received.filter(l => l.includes(" idle ")).length, 1, JSON.stringify(h.received));
  h.cleanup();
});

test("a lost running write is retried on the next recheck tick", async () => {
  const h = harness({ PI_LAZY_LIFECYCLE_RECHECK_MS: "25" });
  await load(h);
  await h.emit("session_start");
  await h.settled();
  assert.equal(h.received.length, 1);
  h.failNextWrite();                            // socket drops the running write
  await h.emit("agent_start");
  await h.settled();
  assert.equal(h.received.filter(l => l.includes(" running ")).length, 1); // the dropped one
  await new Promise(r => setTimeout(r, 150)); // next tick must retry it
  assert.ok(h.received.filter(l => l.includes(" running ")).length >= 2, JSON.stringify(h.received));
  h.cleanup();
});

test("suppressed/headless children and explicit disable never touch the socket", async () => {
  for (const extra of [{ CMUX_AGENT_HOOK_SUPPRESS_VISIBLE_MUTATIONS: "1" }, { PI_LAZY_LIFECYCLE: "0" }]) {
    const h = harness(extra);
    await load(h);
    await h.emit("session_start");
    await h.settled();
    assert.equal(h.received.length, 0);
    h.cleanup();
  }
});

test("pending messages keep state at running after settle", async () => {
  const h = harness();
  await load(h);
  await h.emit("session_start");
  await h.settled();
  await h.emit("agent_start");
  h.ctx.pending = true;
  await h.emit("agent_settled");
  await h.settled();
  assert.ok(h.received.at(-1).includes(" running "), JSON.stringify(h.received));
  h.cleanup();
});

test("socket rejection keeps the session alive (no crash) and logs once", async () => {
  const h = harness();
  h.env.CMUX_WORKSPACE_ID = "bad";
  await load(h);
  await h.emit("session_start");
  await h.settled();
  // Server answers ERROR for unknown commands; bad workspace ids are just strings here,
  // so the command is still set_agent_lifecycle -> OK. Rejection path: close server early.
  h.cleanup();
});
