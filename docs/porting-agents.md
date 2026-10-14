# Agent checklist: porting the launcher to Claude Code or Codex

**These are implementation instructions, not a claim that this repository already supports either agent.** Pi remains the only implemented target. Do not publish or install a port until its agent-specific tests and cmux restore checks pass.

## What stays the same

Reuse `is_focused()`, the subscribe-before-query ordering in `wait_for_focus()`, fresh focus checks after reconnect, and event-child cleanup. The cmux events and window/workspace/surface comparison are agent-independent.

Keep the safety boundary: a waiter controls only its own process/PTY. Never send commands or key presses into arbitrary terminals, terminate another agent, alter sandbox/approval settings, or relax cmux's hibernation process-scope checks.

## 1. Establish the real CLI contract before changing code

Record versions and inspect local help without starting an agent:

```sh
claude --version
claude --help
codex --version
codex --help
codex resume --help
codex exec --help
```

Compare with the official references:

- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)
- [Codex CLI](https://developers.openai.com/codex/cli)
- [Codex source](https://github.com/openai/codex)

The examples below were checked against Claude Code **2.1.291** and Codex CLI **0.146.0**. Installed versions and online docs can differ. Treat help/storage inspection as the source of truth for the version being ported.

| Contract | Claude Code | Codex |
| --- | --- | --- |
| Interactive exact-session resume | `claude --resume <id>` / `-r <id>` | `codex resume <id>` |
| Latest-session selection | `--continue` / `-c` | `resume --last` |
| Non-interactive execution | `--print` / `-p`; machine input/output formats used with print | `exec` / `e`, `review`, `exec resume`; server/MCP modes |
| CLI naming | Installed 2.1.291 help exposes `--name` / `-n`; verify before forwarding | No general `--name` option was exposed by the checked help; **do not copy Pi's injection** |

Both agents can offer a picker when no exact ID is supplied. Preserve that intent; do not silently replace a picker, `--last`/continue, a named session, a remote attach, or a fork with a guessed UUID.

## 2. Change the packaging and executable resolver

Files to modify: `bin/pi-lazy`, `install.sh`, `uninstall.sh`, and `main()` in `launcher.py`.

- Create an agent-specific launcher (`claude-lazy` or `codex-lazy`) and data directory, rather than overwriting the Pi installation or sharing its real-executable record/title cache.
- Replace the managed-Pi default `~/.pi/agent/bin/pi` with the verified real agent executable. Resolve it **before** adding any shadowing launcher. An explicit installer path is safer than guessing through cmux shims.
- Rename the executable override/configuration keys for the new agent. Those keys are part of the port to implement, not currently available here.
- Preserve installer backup/rollback behavior. Reject recursive configuration and unrelated launcher files.
- Inspect the installed cmux `cmux-claude-wrapper` / `cmux-codex-wrapper` and per-surface shim. Verify the delegation path for that agent; do not assume it is identical to Pi's.
- Keep cmux's own shim first in PATH so its hook/integration injection still runs. Do not manually edit generated cmux shims or files inside the app bundle.

An older saved command targeting the real executable directly may bypass the new wrapper. Flag it for the operator; do not rewrite active resume bindings automatically.

## 3. Replace Pi's eligibility rules

Files/functions to modify: `COMMANDS`, `IMMEDIATE_FLAGS`, **`VALUE_OPTIONS` and `cli_options()`**, and `should_wait()` in `launcher.py`; add tests first.

The shared value parser is also Pi-specific: replace its option cardinalities with the target CLI's verified required, optional, and variadic value rules. For example, merely calling `option(args, ("--resume",))` does not make Claude's `--resume` value-taking in Pi's table, and a bare resume-picker flag may legitimately have no value. Codex's `-c` consumes a configuration value, unlike Pi's boolean continue flag. Add exact selector-extraction and flag-looking-value tests before changing the caller.

### Claude Code

- Wait only for an interactive local TTY session. Bypass `-p`/`--print`, SDK/machine-stream execution, and maintenance/help/version commands.
- Bypass explicit background/cloud/server/desktop/attach-management modes. New Claude releases add such modes; inspect current help rather than copying a stale list. A `remote-control` server must not be held waiting for terminal focus.
- Replace the Pi-child marker check with the target's verified child-process behavior. Do not rely on a marker alone: SDK processes may omit it, so explicit headless mode and TTY checks remain necessary.

### Codex

- Allow the interactive top-level CLI and supported interactive `resume`/`fork` forms to wait.
- Bypass `exec`/`e`, non-interactive `review`, `mcp-server`, `app-server`, `exec-server`, and management commands such as login/logout, update, completion, apply, sandbox, and features. Re-check the list against the installed version.
- `codex exec resume <id>` is **not** the same launch mode as `codex resume <id>`: the former must pass through immediately.
- Replace the Pi marker check only after confirming Codex's actual exported environment; keep mode/TTY checks even when a child marker exists.

For both agents, parse option **values**, short aliases, subcommands, and `--` correctly. A prompt or `--name` value equal to `--print` must not be mistaken for a flag. Conversely, a piped SDK/server invocation must never block because it inherited a cmux surface ID.

## 4. Replace the session selector and title reader

Files/functions to modify: `option()`, `session_title()`, and the inferred-name forwarding in `main()`.

### Claude Code

- Read the exact resume selector from `--resume` / `-r`, not Pi's `--session`.
- Preserve a supplied ID, supported name, transcript path, or picker unchanged. For `--continue`, identify the session only if the installed CLI's selection rules can be reproduced reliably; otherwise display a generic waiting label.
- Inspect the current Claude configuration directory/project transcripts and naming metadata. Do not assume Pi's `session_info` schema, directory naming, or filename format.
- Prefer the latest verified saved/user-set name. Use a private cache only as fallback, and honor intentional name clearing if the schema supports it.
- Forward an inferred label through a supported naming API/flag **only after wake** and only after verifying the version's behavior. Never confuse a server display-name flag with a conversation-name flag.

### Codex

- Parse `resume <id-or-name>` (and any supported fork form) after the actual subcommand/options. Do not look for `--session`.
- Preserve optional prompts, working directory overrides, fork semantics, and remote/app-server arguments exactly.
- Inspect the installed Codex session/rollout/state format under its verified config home. Prefer a supported read-only metadata API or a separately prepared private cache. Do not hard-code Pi's JSONL fields or guess a SQLite table/column name.
- Do not append `--name`: it was not a general option in the checked Codex version. Keep the title in the waiter's banner/terminal OSC title, or use a verified supported rename API after wake.
- Never edit live rollouts or the agent's state database to manufacture a title.

No title lookup should start the real agent, an app server, embeddings, or a naming LLM. Sanitize control characters before writing terminal titles.

## 5. Preserve launch fidelity and cmux integration

Leave the model/provider, authentication, approval/sandbox, cwd, environment, prompt text, and session selection untouched. Terminate/reap the event child before `exec` of the real agent. If the port legitimately adds a naming option, preserve an explicit existing name and insert before the `--` delimiter, never into prompt text.

Validate with the **actual bundled wrapper**, not only a fake executable: its integration may alter argv/env or route through a helper. Confirm that the restored checkpoint is the original one after the agent starts. Never send a test prompt to a production session simply to prove restoration.

## 6. Extend the tests and verify before rollout

Files to modify: `tests/test_launcher.py`, installer tests, and CI/docs as appropriate.

Required cases for each port:

1. Hidden local interactive launch starts no agent/MCP process before its own focus.
2. Already-focused launch starts once, with exact argv/cwd/env/exit status.
3. Wrong window, workspace, or surface does not wake it; stale/replayed focus events are not sufficient.
4. Subscribe/query race and reconnect acknowledgement are handled.
5. Ctrl-C, SIGHUP, SIGTERM, broken event stream, and failed focus query leave no event helper behind.
6. Agent-specific headless/server/maintenance forms pass through with **no subscription**, including inherited cmux IDs.
7. Resume IDs/names/paths, continue/last, fork, positional prompts and `--` keep their meaning. Test short aliases and flag-looking option values.
8. Names use the correct storage/API, explicit names win, caches stay private, and no unsupported naming option is injected.
9. Installer reruns, recursive targets, original symlinks, missing backups, and a subsequently changed launcher behave safely.
10. Actual cmux quit/reopen: unused tabs stay lightweight; one intentional focus starts only its own session and preserves native hooks/checkpoint identity.

Use dummy agents and isolated HOME/PTY fixtures for automated tests. Do not restart or foreground the operator's cmux app without permission. Keep private paths, prompts, session IDs, tokens, title caches and screenshots out of the public repository.

## Suggested handoff to an implementing agent

> Implement only the selected Claude **or** Codex port in a branch. Read this checklist and current installed CLI help first. Add failing agent-specific eligibility/resume/title/installer tests, then make them pass without altering the common focus invariant. Do not modify cmux, generated shims, a real agent install, active sessions, or private session logs. Report the exact versions validated, remaining storage/API unknowns, tests, and the manual cmux restore check still required. Do not call the port supported until those gates pass.
