# cmux-pi-lazy

**Keep your cmux layout. Start each Pi session only when you focus its tab.**

A small, event-driven launcher workaround for [cmux](https://github.com/manaflow-ai/cmux) and [Pi](https://github.com/badlogic/pi-mono). No cmux rebuild, shell polling loop, or model running in a waiting tab.

```text
[pi-lazy] Conversation: Example coding task
[pi-lazy] Waiting for this tab's focus. Ctrl-C cancels.
```

This is a CLI launcher, not a patch to cmux. It uses cmux's existing Pi shim and public event/identify commands. An **optional** Pi extension can also report accurate session lifecycle (running / idle / needs-input / background-work-pending) to cmux's native Agent Hibernation engine, so already-visited idle tabs are hibernated and woken by cmux itself.

## Why

Restoring many agent tabs can start every Pi process and its MCP helpers at once, even when most tabs are off-screen. This wrapper leaves a lightweight waiter instead. When that exact window/workspace/surface gets focus, it replaces itself with the real Pi command and its original session arguments.

In one macOS test after a normal cmux restart:

| Whole-machine process group | Before | After |
| --- | ---: | ---: |
| Pi processes | 17 | 2 |
| Direct MCP helpers | 68 | 8 |
| Lazy waiters + event clients | 0 | 15 + 15 |
| Summed Pi/MCP/waiter RSS | ~5.8 GiB | ~1.5 GiB |

Only one Pi was running **inside cmux**; the second was an older, unrelated process outside cmux. Waiting processes showed 0% CPU in the idle snapshot. A waiter plus its event CLI measured roughly 30–35 MiB summed RSS. These are observations from one machine, not benchmarks or guaranteed savings; RSS includes shared pages and is not marginal physical memory.

## Requirements

- macOS with cmux's `events` and JSON `identify` commands. Live-tested with cmux **0.65.1**.
- Python **3.9+** at `/usr/bin/python3` (on macOS this may require Command Line Tools).
- Pi; live-tested with **1.2.0**. Saved display names use Pi's `session_info.name` and `--name` support.
- `~/.local/bin` on your interactive shell's PATH, **behind cmux's own per-surface shim**.

## Install

```sh
git clone https://github.com/cosmyo/cmux-pi-lazy.git
cd cmux-pi-lazy
./install.sh
```

The default real Pi is the managed launcher at `~/.pi/agent/bin/pi`. For an npm/global or other installation, specify the original executable explicitly:

```sh
./install.sh --pi /absolute/path/to/your/original/pi
```

Do not use cmux's per-surface shim, `pi-lazy`, or the newly installed `~/.local/bin/pi` as the real executable. Before replacing anything, the installer resolves the original target and rejects targets that installation would overwrite or make recursive. An original local `pi` symlink to a separate real executable is safe; an original regular executable at a path installation replaces is refused rather than relocated silently. The installer saves the previous local launcher before replacing it; it does not modify cmux, the real Pi installation, shell profiles, private title caches, or running sessions.

Keep typing:

```sh
pi
pi --session <session-id>
```

cmux's existing Pi shim skips other cmux shims when resolving the underlying `pi` through PATH. That reaches this wrapper and retains the native cmux hook integration. An alias alone would not catch saved absolute shim paths, so this installation uses a PATH wrapper instead.

**Existing running Pis do not become waiters retroactively.** The change applies to new starts and covered saved launches at your next normal cmux restart. Restart only when no important jobs are running. Saved commands that directly target a different executable, such as an absolute global Pi path, bypass this wrapper; launch through cmux's Pi shim to capture a compatible resume binding. We do not rewrite bindings automatically.

## Behavior and safety

- Already focused: Pi starts after the subscription acknowledgement and live focus check.
- Off-screen/unfocused: wait for this exact surface's keyboard focus. A visible split pane that is not focused can remain waiting.
- Outside cmux, without an interactive stdin/stdout TTY, inside a marked Pi child process, or for print/RPC/JSON/help/version/maintenance commands: pass through without waiting.
- Ctrl-C, SIGHUP, or SIGTERM cancels a waiting launcher and reaps its event helper.
- The event subscription is established **before** the initial focus query. Reconnect acknowledgements and events trigger a fresh live-state query; replayed events are not treated as proof of current focus.
- No process census, idle guessing, terminal keystroke injection, app foregrounding, forced session shutdown, or weakening of cmux's termination safety checks.
- Focus/API failures fail closed instead of launching a background Pi unexpectedly. The event CLI owns reconnect/backoff; there is no shell polling loop.

Force a direct launch when needed:

```sh
PI_LAZY_BYPASS=1 pi --session <session-id>
```

`PI_LAZY_REAL_PI=/absolute/path/to/pi` overrides the saved real executable for a command. The wrapper rejects an empty/missing/non-executable path and recursive configuration.

## Conversation titles

Title priority:

1. Explicit `--name` / `-n`.
2. Latest saved Pi `session_info.name`; an intentionally empty name clears it.
3. An optional private local cache at `~/.local/share/pi-lazy/titles.json`.

Session UUIDs and JSONL paths are supported, including custom session directories. Only header/name records are parsed; large message/tool bodies are not decoded to find a title. Display text is stripped of terminal control characters and limited to 80 characters.

To use a manually prepared title cache, copy and edit the **fictitious** example:

```sh
cp titles.example.json ~/.local/share/pi-lazy/titles.json
chmod 600 ~/.local/share/pi-lazy/titles.json
```

Do not overwrite an existing cache accidentally. This project does not scan/index conversations or run QMD, embeddings, or an LLM to generate names. Prepare inferred names separately if you want them. A cache entry retains its optional provenance, but is not sent to a provider by this launcher.

On wake, a cached title is handed to Pi through `--name` **only if no explicit or saved name exists**. Pi then persists that name itself. The launcher never edits raw session logs. Other user arguments, cwd, environment, PTY and Pi's exit status are retained. Existing waiters do not hot-reload updated Python code; new banner/name behavior starts on their next launch.

## Optional: native hibernation integration

cmux 0.65+ ships an Agent Hibernation engine (off-screen + idle + safe process census → graceful stop, wake on focus). For Pi sessions restored by cmux, its lifecycle stays `unknown` until the next turn, so the engine correctly refuses to touch them. The bundled reporter extension (`extensions/lifecycle.mjs`) fixes the signal, not the engine: it reports the session's true state over cmux's local socket (`set_agent_lifecycle`) on turn start, settle, draft edits, and registered background-work changes. cmux keeps owning all timing, safety checks, teardown, and wake.

Enable for new launches:

```sh
./install.sh --hibernate
```

Also enable cmux's engine and pick the idle delay there:

```sh
cmux agent-hibernation on
cmux config set terminal.agentHibernation.idleSeconds 60
```

Safety rules (fail-awake everywhere):

- A turn in flight or queued messages → `running`.
- An unsent editor draft → `needsInput` (not hibernatable; protects drafts).
- Active work registered through the `pi-subagents` background-work protocol → `backgroundWorkPending`.
- Suppressed/headless child sessions never report.
- Probe failures keep the last non-idle state; the reporter never sends keystrokes, signals, or shutdowns — it only reports.
- Sessions already running before installation don't load the reporter until their next start; they stay `unknown` (never auto-hibernated) — safe by default.

Disable with `./install.sh --no-hibernate` or `PI_LAZY_NATIVE_HIBERNATION=0` per launch. Known limit: extensions with unregistered background work (e.g. a watcher that outlives a turn without registering it) are invisible to the reporter; cmux's census still applies, but enable this only for workloads whose background tasks use the registration protocol.

## Uninstall

From the checkout:

```sh
./uninstall.sh
```

This restores the saved local launcher only while `~/.local/bin/pi` is still our symlink. It refuses to overwrite a subsequently changed launcher. Inactive helper files and private caches remain for inspection. Existing processes are not stopped.

A Pi installer/update may reset `~/.local/bin/pi`; check `readlink ~/.local/bin/pi`. **If it changed, reinstall refuses to overwrite the replacement**, even when an older backup record exists. Leave the updated CLI in place, or preserve its file/symlink separately before manual recovery; do not delete the backup record to force installation. To use the uninstaller, first preserve the replacement and restore the owned `pi -> pi-lazy` link, then uninstall and choose which preserved launcher to restore before a fresh install. If the resolved real executable moved during an update but the owned link remains intact, rerun `./install.sh --pi <new-real-path>` to update that target. A native cmux lazy-restoration setting, once shipped and tested with Pi, is preferable to keeping this workaround indefinitely.

## Tests

```sh
make test
make check
```

The standard-library suite exercises PTYs, fake event streams, exact session/argv/cwd/model preservation, wrong window/workspace/surface refusal, subscription/reconnect acknowledgement, cancellation and helper cleanup, title precedence/clearing/control-character safety, headless bypass, and inferred-name forwarding only after wake. On a machine with cmux installed, it also tests delegation through the actual bundled `cmux-pi-wrapper`; that test skips elsewhere.

A real-cmux background-PTY check used a dummy Pi executable, verified no startup before focus, measured idle resources, and verified cancellation cleanup without changing UI focus. Normal quit/reopen restoration and the current resumed checkpoint were separately verified. These checks do not prove compatibility with every future cmux/Pi release.

## Porting to Claude Code or Codex

See the [agent implementation checklist](docs/porting-agents.md) for the exact files/functions to change, installed CLI contracts, agent-specific headless/resume/title handling, cmux hook integration, and required tests. This is a guide for an implementing agent; **Claude and Codex support is not shipped in this repository**. The common focus/event machinery is reusable, but Pi's session format, child markers and naming arguments are not universal.

## Related upstream reports and work

These reports explain the problem family; **they are not all the same root cause, and this Pi-only launcher does not fix Claude or Codex**.

- [#13024: resumed Claude sessions remain non-restorable until another Stop](https://github.com/manaflow-ai/cmux/issues/13024) — closest reported lifecycle/startup symptom.
- [#5731: resource accumulation and mass agent auto-resume](https://github.com/manaflow-ai/cmux/issues/5731) — broader discussion including first-focus resume.
- [#14481: Pi first-class integration/parity tracker](https://github.com/manaflow-ai/cmux/issues/14481) — Pi-specific upstream context.
- [PR #9145: native first-focus deferred agent restoration](https://github.com/manaflow-ai/cmux/pull/9145) — the native approach; this workaround is not a replacement for that work.
- [PR #18839: remaining live Claude hibernation/scoping fixes](https://github.com/manaflow-ai/cmux/pull/18839) — related safety/lifecycle work, not implemented here.

Linked proposals may change status. No upstream fix is represented as shipped merely because it has a PR.

## License

MIT. This project is independent of the cmux and Pi maintainers.
