"""Defer an interactive Pi launch until its own cmux surface is focused."""

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys


FOCUS_EVENTS = ("workspace.selected", "surface.selected", "surface.focused", "pane.focused")
COMMANDS = {"install", "remove", "uninstall", "update", "list", "config", "auth", "mcp"}
IMMEDIATE_FLAGS = {"-p", "--print", "-h", "--help", "-v", "--version", "--list-models", "--export"}
# Value-taking options from Pi 1.2's CLI; do not reinterpret their values as flags.
VALUE_OPTIONS = {
    "--mode", "--provider", "--model", "--api-key", "--system-prompt", "--append-system-prompt",
    "--name", "-n", "--session", "--session-id", "--fork", "--session-dir", "--models",
    "--tools", "-t", "--exclude-tools", "-xt", "--thinking", "--extension", "-e",
    "--skill", "--prompt-template", "--theme", "--use-theme",
}


def cli_options(args):
    index = 0
    while index < len(args) and args[index] != "--":
        arg = args[index]
        if arg in VALUE_OPTIONS:
            yield arg, args[index + 1] if index + 1 < len(args) else None
            index += 2
        else:
            yield arg, None
            index += 1


def should_wait(args, env, stdin_tty, stdout_tty):
    if not stdin_tty or not stdout_tty:
        return False
    if not env.get("CMUX_WORKSPACE_ID") or not env.get("CMUX_SURFACE_ID"):
        return False
    if env.get("PI_LAZY_BYPASS") == "1" or env.get("PI_CODING_AGENT") == "true":
        return False
    if env.get("CMUX_AGENT_HOOK_SUPPRESS_VISIBLE_MUTATIONS") == "1":
        return False
    if args and args[0] in COMMANDS:
        return False
    for arg, value in cli_options(args):
        if arg in IMMEDIATE_FLAGS:
            return False
        if arg in VALUE_OPTIONS and value is None:
            return False  # Let Pi report malformed argv immediately.
        if arg == "--mode" and value != "text":
            return False
        if arg.startswith("--mode="):
            return False
    return True


def option(args, names):
    value = None
    for arg, argument in cli_options(args):
        if arg in names:
            value = argument
    return value


def clean_title(value):
    if not isinstance(value, str):
        return None
    text = "".join(char if char.isprintable() else " " for char in value)
    return " ".join(text.split())[:80] or None


def session_title(args, env):
    explicit = option(args, ("--name", "-n"))
    if explicit is not None:
        return clean_title(explicit), False
    selector = option(args, ("--session",))
    if not selector:
        return None, False
    agent = Path(env.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi/agent")))
    directory = option(args, ("--session-dir",)) or env.get("PI_CODING_AGENT_SESSION_DIR")
    sessions = Path(directory) if directory else agent / "sessions"
    path = Path(selector).expanduser()
    if path.is_file():
        matches = [path]
    else:
        matches = list(sessions.glob("*_" + selector + ".jsonl"))
        matches += list(sessions.glob("*/*_" + selector + ".jsonl"))
    session_id, name, has_name = selector, None, False
    try:
        if len(matches) == 1:
            with matches[0].open() as stream:
                for line in stream:
                    # Do not parse large message/tool bodies just to find a name.
                    if not re.match(r'^\s*\{\s*"type"\s*:\s*"session(?:_info)?"', line):
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:  # A concurrent append may leave a partial last line.
                        continue
                    if entry.get("type") == "session":
                        session_id = entry.get("id", session_id)
                    elif entry.get("type") == "session_info":
                        name, has_name = entry.get("name"), True
            if has_name:
                return clean_title(name), False  # An empty latest name intentionally clears it.
        cache_path = Path.home() / ".local/share/pi-lazy/titles.json"
        if cache_path.is_file():
            cache = json.loads(cache_path.read_text())
            if not isinstance(cache, dict) or not isinstance(session_id, str):
                return None, False
            cached = cache.get(session_id, {})
            if not isinstance(cached, dict):
                return None, False
            title = clean_title(cached.get("title"))
            return title, bool(title)
    except (OSError, ValueError):
        pass  # Optional display metadata must not prevent an otherwise valid launch.
    return None, False


def is_focused(cmux, env):
    result = subprocess.run(
        [cmux, "--json", "identify", "--workspace", env["CMUX_WORKSPACE_ID"],
         "--surface", env["CMUX_SURFACE_ID"]],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10,
    )
    state = json.loads(result.stdout)
    caller, focused = state.get("caller", {}), state.get("focused", {})
    return all(caller.get(key) and caller[key] == focused.get(key)
               for key in ("window_ref", "workspace_ref", "surface_ref"))


def stop_events(events):
    if events.poll() is None:
        events.terminate()
        try:
            events.wait(timeout=2)
        except subprocess.TimeoutExpired:
            events.kill()
            events.wait()
    events.stdout.close()


def wait_for_focus(cmux, env, title):
    command = [cmux, "events", "--reconnect", "--no-heartbeat"]
    for name in FOCUS_EVENTS:
        command.extend(["--name", name])
    events = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        assert events.stdout is not None  # stdout=PIPE is required by this protocol.
        if title:
            print("\033]2;π · " + title + "\007", end="", flush=True)
            print("[pi-lazy] Conversation: " + title, flush=True)
        print("[pi-lazy] Waiting for this tab's focus. Ctrl-C cancels.", flush=True)
        # Subscribe BEFORE the first state query, so focus during that query
        # is queued. Re-check live state on reconnect rather than trusting replay.
        for line in events.stdout:
            frame = json.loads(line)
            kind = frame.get("type")
            if kind == "error":
                raise RuntimeError("cmux event subscription failed")
            if kind in ("ack", "event") and is_focused(cmux, env):
                return
        raise RuntimeError("cmux event stream closed before this tab was focused")
    finally:
        stop_events(events)


def cancel(signum, _frame):
    raise SystemExit(128 + signum)


def main():
    args = sys.argv[1:]
    configured = os.environ.get("PI_LAZY_REAL_PI", str(Path.home() / ".pi/agent/bin/pi"))
    real_path = Path(configured).expanduser()
    if not configured or not real_path.is_file() or not os.access(real_path, os.X_OK):
        print("[pi-lazy] Real Pi launcher is not executable: " + configured, file=sys.stderr)
        return 127
    entrypoint = os.environ.get("PI_LAZY_ENTRYPOINT")
    if real_path.resolve() == Path(__file__).resolve() or (
        entrypoint and real_path.resolve() == Path(entrypoint).resolve()
    ):
        print("[pi-lazy] Refusing recursive launcher configuration.", file=sys.stderr)
        return 127
    real_pi = str(real_path)
    if should_wait(args, os.environ, sys.stdin.isatty(), sys.stdout.isatty()):
        cmux = os.environ.get("CMUX_BUNDLED_CLI_PATH") or shutil.which("cmux")
        if not cmux:
            print("[pi-lazy] cmux CLI missing; use PI_LAZY_BYPASS=1 pi to start directly.", file=sys.stderr)
            return 1
        title, inferred = session_title(args, os.environ)
        signal.signal(signal.SIGTERM, cancel)
        signal.signal(signal.SIGHUP, cancel)
        try:
            wait_for_focus(cmux, os.environ, title)
        except KeyboardInterrupt:
            return 130
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            print("[pi-lazy] Not starting Pi: {}. Use PI_LAZY_BYPASS=1 pi to bypass.".format(error),
                  file=sys.stderr)
            return 1
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        if inferred and title:
            # Let Pi persist the inferred name via its supported CLI, only after wake.
            args = ["--name", title] + args
    # Keep cwd, user arguments/environment, the PTY, and Pi's real exit status.
    # The event helper has exited before Pi (or any MCP helper) is started.
    os.execv(real_pi, [real_pi] + args)


if __name__ == "__main__":
    sys.exit(main())
