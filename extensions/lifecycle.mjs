// cmux-pi-lazy lifecycle reporter.
//
// Reports this Pi session's true lifecycle state to cmux's hibernation engine
// through the local v1 socket (`set_agent_lifecycle`). cmux owns all timing,
// hysteresis, process-census safety, termination and wake; this extension only
// reports. It never kills, suspends, or touches the process itself.
//
// States reported:
//   running                - a turn is in flight
//   idle                   - settled, nothing pending, empty editor
//   needsInput             - settled but the editor holds an unsent draft
//   backgroundWorkPending  - registered background providers report active work
//
// Unknown or failed probes never downgrade a state silently: while a probe
// fails, the last known non-idle state is kept (fail-awake).

import { Socket } from "node:net";
import { homedir } from "node:os";
import { join } from "node:path";

const STATE_KEY = "pi";

function makeReporter(env, log) {
  const workspace = env.CMUX_WORKSPACE_ID;
  const surface = env.CMUX_SURFACE_ID;
  const socketPath = env.CMUX_SOCKET_PATH || join(homedir(), ".local/state/cmux/cmux.sock");
  if (!workspace || !surface) return () => {};
  let queue = Promise.resolve();
  // Only transitions may be written: every keyed write refreshes cmux's
  // activity timestamps, so a periodic identical write would both starve
  // recently_active and defeat hasUnconfirmedTerminalInput. A failed write
  // resets lastConfirmed so the next evaluation retries it.
  let lastConfirmed;
  return (state) => {
    if (state === lastConfirmed) return queue;
    queue = queue.then(() => new Promise((resolve) => {
      const sock = new Socket();
      let done = false;
      const finish = () => { if (!done) { done = true; sock.destroy(); resolve(); } };
      const fail = () => { if (!done) { lastConfirmed = undefined; finish(); } };
      sock.setTimeout(3000);
      sock.once("timeout", fail);
      sock.once("error", fail);
      sock.connect(socketPath, () => {
        const cmd = `set_agent_lifecycle ${STATE_KEY} ${state} --tab=${workspace} --panel=${surface}\n`;
        sock.write(cmd);
        let buf = "";
        sock.on("data", (chunk) => {
          buf += chunk.toString();
          if (buf.includes("\n")) {
            if (!buf.startsWith("OK")) {
              log(`lifecycle report '${state}' rejected: ${buf.trim()}`);
              fail();
            } else {
              lastConfirmed = state;
              finish();
            }
          }
        });
        sock.once("close", fail);
      });
    }));
    return queue;
  };
}

export default function lifecycleReporter(pi) {
  if (process.env.CMUX_AGENT_HOOK_SUPPRESS_VISIBLE_MUTATIONS === "1") return;
  if (process.env.PI_LAZY_NATIVE_HIBERNATION === "0" || process.env.PI_LAZY_LIFECYCLE === "0") return;
  const log = (msg) => console.error(`[pi-lazy-lifecycle] ${msg}`);
  const report = makeReporter(process.env, log);

  let ctx;
  let turnActive = false;
  let draft = false;
  let recheckTimer;
  // Programmatic editor writes (external-editor return, ctx.ui.setEditorText)
  // and provider registration changes produce no terminal-input or bus event.
  // Re-evaluate on a slow timer; this is an in-process memory read, not a poll
  // of cmux, and it never spawns work.
  const RECHECK_MS = Number(process.env.PI_LAZY_LIFECYCLE_RECHECK_MS) || 5000;
  const armRecheck = () => {
    if (recheckTimer) return;
    recheckTimer = setInterval(() => {
      // Evaluate unconditionally: also retries a lost transition write (e.g.
      // running rejected mid-turn) on the next tick.
      evaluate();
    }, RECHECK_MS);
    recheckTimer.unref?.();
  };

  // Registered background work (pi-subagents protocol v1) blocks hibernation.
  // Mirror the protocol's conservative snapshot semantics: an ABSENT registry
  // means no registered providers; a PRESENT but malformed one is blocking
  // uncertainty, and reconcile() must run before listActiveWork().
  // Returns true (work active) | false (verified none) | undefined (unknown -> block).
  const backgroundCheck = () => {
    const registry = globalThis[Symbol.for("pi-subagents.background-work.v1")];
    if (registry === undefined) return false;
    if (!registry || registry.version !== 1 || !(registry.providers instanceof Map)) return undefined;
    let sessionId;
    try { sessionId = ctx?.sessionManager.getSessionFile(); } catch { return undefined; }
    for (const provider of registry.providers.values()) {
      try {
        provider.reconcile?.({ sessionId, nowMs: Date.now() });
      } catch { return undefined; }
      let work;
      try {
        work = provider.listActiveWork({ sessionId, nowMs: Date.now() });
      } catch { return undefined; }
      if (!Array.isArray(work)) return undefined;
      if (work.length > 0) return true;
    }
    return false;
  };

  const evaluate = () => {
    if (!ctx || ctx.mode !== "tui" || !ctx.hasUI) return;
    if (turnActive) { void report("running"); return; }
    try {
      if (ctx.hasPendingMessages()) { void report("running"); return; }
    } catch {
      // Probe uncertainty must not preserve a possibly-stale idle.
      void report("unknown");
      return;
    }
    const work = backgroundCheck();
    if (work !== false) { void report(work === true ? "backgroundWorkPending" : "unknown"); return; }
    let editor = "";
    try { editor = ctx.ui.getEditorText(); } catch {
      void report("unknown");
      return;
    }
    if (editor !== "") { draft = true; void report("needsInput"); return; }
    draft = false;
    void report("idle");
  };

  const startTurn = (_event, value) => {
    if (value) ctx = value;
    // Never report from RPC/print contexts, even if cmux ids are present.
    if (!ctx || ctx.mode !== "tui" || !ctx.hasUI) return;
    turnActive = true;
    void report("running");
  };
  pi.on("agent_start", startTurn);
  pi.on("before_agent_start", startTurn);

  pi.on("agent_settled", (_event, value) => {
    if (value) ctx = value;
    turnActive = false;
    evaluate();
  });

  pi.on("session_start", (_event, value) => {
    if (!value || value.mode !== "tui" || !value.hasUI) return;
    ctx = value;
    turnActive = false;
    armRecheck();
    evaluate();
  });

  pi.on("session_switch", (_event, value) => {
    if (!value || value.mode !== "tui") return;
    ctx = value;
    turnActive = false;
    evaluate();
  });

  // Unsent editor text blocks hibernation (needsInput is not hibernatable).
  const wiredUi = new WeakSet();
  const watchInput = (value) => {
    const ui = value?.ui;
    if (!ui?.onTerminalInput || wiredUi.has(ui)) return;
    wiredUi.add(ui);
    ui.onTerminalInput((data) => {
      if (data === "\x1b[I" || data === "\x1b[O" || /^\x1b\[6;\d+;\d+t$/.test(data)) return;
      if (turnActive) return;
      // Re-read the editor on the next tick; input arrives before the edit applies.
      setTimeout(() => {
        if (turnActive || !ctx) return;
        try {
          const text = ctx.ui.getEditorText();
          if (text !== "" && !draft) { draft = true; void report("needsInput"); }
          else if (text === "" && draft) { draft = false; evaluate(); }
        } catch { /* keep last state */ }
      }, 0);
    });
  };
  pi.on("session_start", (_event, value) => watchInput(value));
  pi.on("session_switch", (_event, value) => watchInput(value));

  for (const event of ["subagent:async-started", "subagent:async-complete", "subagent:process-terminal"]) {
    pi.events.on(event, () => setTimeout(() => { if (!turnActive) evaluate(); }, 0));
  }

  pi.on("session_shutdown", () => {
    if (recheckTimer) { clearInterval(recheckTimer); recheckTimer = undefined; }
  });
  // If a turn never settles (crash), a later prompt re-reports running; nothing else on shutdown.
}
