import importlib.util
import json
import os
from pathlib import Path
import pty
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("launcher", ROOT / "launcher.py")
assert spec is not None and spec.loader is not None
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

FAKE_CMUX = '''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
root = Path(os.environ["FIXTURE"])
if "events" in sys.argv:
    (root / "events.pid").write_text(str(os.getpid()))
    print(json.dumps({"type": "ack"}), flush=True)
    with open(root / "events.fifo") as stream:
        for line in stream:
            print(line.strip(), flush=True)
else:
    state = json.loads((root / "state.json").read_text())
    print(json.dumps(state))
    with (root / "queries.log").open("a") as log:
        log.write("query\\n")
'''
FAKE_PI = '''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
root = Path(os.environ["FIXTURE"])
(root / "launched.json").write_text(json.dumps({
    "args": sys.argv[1:], "cwd": os.getcwd(), "model": os.environ.get("PI_MODEL"),
    "shim": os.environ.get("CMUX_PI_WRAPPER_SHIM")
}))
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
'''


class EligibilityTests(unittest.TestCase):
    def test_only_interactive_cmux_launches_wait(self):
        env = {"CMUX_WORKSPACE_ID": "ws", "CMUX_SURFACE_ID": "surface"}
        self.assertTrue(launcher.should_wait(["--session", "exact-id"], env, True, True))
        self.assertTrue(launcher.should_wait(["--mode", "text"], env, True, True))
        self.assertTrue(launcher.should_wait(["--name", "--print"], env, True, True))
        self.assertTrue(launcher.should_wait(["--system-prompt", "--mode"], env, True, True))
        self.assertFalse(launcher.should_wait(["--mode", "tui"], env, True, True))
        self.assertEqual(launcher.option(["--name", "--session", "--session", "id"], ("--session",)), "id")
        self.assertTrue(launcher.should_wait(["--", "--print"], env, True, True))
        for args in (["--print"], ["-p", "prompt"], ["--mode", "rpc"], ["--mode", "json"],
                     ["--mode"], ["--version"], ["--help"], ["--list-models"],
                     ["--export", "session"], ["update"], ["mcp", "list"]):
            with self.subTest(args=args):
                self.assertFalse(launcher.should_wait(args, env, True, True))
        for override in ({}, {"CMUX_SURFACE_ID": "surface"}, dict(env, PI_LAZY_BYPASS="1"),
                         dict(env, PI_CODING_AGENT="true"),
                         dict(env, CMUX_AGENT_HOOK_SUPPRESS_VISIBLE_MUTATIONS="1")):
            with self.subTest(env=override):
                self.assertFalse(launcher.should_wait([], override, True, True))
        self.assertFalse(launcher.should_wait([], env, False, True))
        self.assertFalse(launcher.should_wait([], env, True, False))


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pi-lazy-test-")
        self.root = Path(self.temp.name).resolve()
        real = self.root / ".pi/agent/bin/pi"
        real.parent.mkdir(parents=True)
        real.write_text(FAKE_PI)
        real.chmod(0o700)
        cmux = self.root / "cmux"
        cmux.write_text(FAKE_CMUX)
        cmux.chmod(0o700)
        os.mkfifo(self.root / "events.fifo")
        self.fifo = os.open(self.root / "events.fifo", os.O_RDWR)
        self.env = dict(os.environ, HOME=str(self.root), FIXTURE=str(self.root),
                        CMUX_BUNDLED_CLI_PATH=str(cmux), CMUX_WORKSPACE_ID="ws",
                        CMUX_SURFACE_ID="own", PI_MODEL="preserved-model",
                        CMUX_PI_WRAPPER_SHIM="/original/cmux/pi")
        for key in ("PI_LAZY_BYPASS", "PI_CODING_AGENT", "CMUX_AGENT_HOOK_SUPPRESS_VISIBLE_MUTATIONS",
                    "PI_LAZY_REAL_PI", "PI_LAZY_ENTRYPOINT"):
            self.env.pop(key, None)
        self.state(False)
        self.child = None
        self.master = None

    def tearDown(self):
        if self.child and self.child.poll() is None:
            self.child.terminate()
            self.child.wait(timeout=5)
        if self.master is not None:
            os.close(self.master)
        os.close(self.fifo)
        self.temp.cleanup()

    def state(self, focused, wrong_workspace=False, wrong_window=False):
        caller = {"window_ref": "window:1", "workspace_ref": "workspace:1", "surface_ref": "surface:1"}
        focus = dict(caller, surface_ref="surface:1" if focused else "surface:2")
        if wrong_workspace:
            focus["workspace_ref"] = "workspace:2"
        if wrong_window:
            focus["window_ref"] = "window:2"
        (self.root / "state.json").write_text(json.dumps({"caller": caller, "focused": focus}))

    def start(self, args=(), tty=True, command=None):
        command = command or [sys.executable, str(ROOT / "launcher.py")]
        if tty:
            self.master, slave = pty.openpty()
            self.child = subprocess.Popen([*command, *args],
                                          stdin=slave, stdout=slave, stderr=slave,
                                          cwd=self.root, env=self.env)
            os.close(slave)
        else:
            self.child = subprocess.Popen([*command, *args],
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, cwd=self.root, env=self.env)

    def until(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Fixture did not reach expected state")
            time.sleep(0.01)

    def queries(self):
        path = self.root / "queries.log"
        return len(path.read_text().splitlines()) if path.exists() else 0

    def event(self, kind="event"):
        os.write(self.fifo, (json.dumps({"type": kind, "name": "surface.focused"}) + "\n").encode())

    def assert_helper_reaped(self):
        pid = int((self.root / "events.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_hidden_waiter_starts_only_its_own_surface_and_preserves_launch(self):
        args = ["--session", "exact-id", "--model", "model with spaces", "--", "Unicode λ; $(not-a-command)"]
        self.start(args)
        assert self.child is not None
        self.until(lambda: self.queries() == 1)
        self.assertFalse((self.root / "launched.json").exists())
        # Heartbeats must not cause state polling.
        self.event("heartbeat")
        self.event()
        self.until(lambda: self.queries() == 2)
        self.assertFalse((self.root / "launched.json").exists())
        self.state(True, wrong_workspace=True)
        self.event()
        self.until(lambda: self.queries() == 3)
        self.assertFalse((self.root / "launched.json").exists())
        self.state(True, wrong_window=True)
        self.event()
        self.until(lambda: self.queries() == 4)
        self.assertFalse((self.root / "launched.json").exists())
        self.state(True)
        self.event()
        self.assertEqual(self.child.wait(timeout=5), 0)
        data = json.loads((self.root / "launched.json").read_text())
        self.assertEqual(data, {"args": args, "cwd": str(self.root), "model": "preserved-model",
                                "shim": "/original/cmux/pi"})
        self.assert_helper_reaped()

    def test_already_focused_starts_on_subscription_ack(self):
        self.state(True)
        self.env["FAKE_EXIT"] = "23"
        self.start(["--session", "exact-id"])
        assert self.child is not None
        self.assertEqual(self.child.wait(timeout=5), 23)
        self.assertEqual(self.queries(), 1)
        self.assert_helper_reaped()

    def test_reconnect_ack_rechecks_current_focus(self):
        self.start()
        assert self.child is not None
        self.until(lambda: self.queries() == 1)
        self.state(True)
        self.event("ack")
        self.assertEqual(self.child.wait(timeout=5), 0)
        self.assert_helper_reaped()

    def test_ctrl_c_and_hangup_cancel_without_starting_pi(self):
        for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
            with self.subTest(sig=sig):
                baseline = self.queries()
                self.start()
                assert self.child is not None and self.master is not None
                self.until(lambda: self.queries() > baseline)
                os.kill(self.child.pid, sig)
                self.assertEqual(self.child.wait(timeout=5), 128 + sig)
                self.assertFalse((self.root / "launched.json").exists())
                self.assert_helper_reaped()
                os.close(self.master)
                self.master = None

    def test_subscription_error_fails_closed(self):
        self.start()
        assert self.child is not None
        self.until(lambda: self.queries() == 1)
        self.event("error")
        self.assertEqual(self.child.wait(timeout=5), 1)
        self.assertFalse((self.root / "launched.json").exists())
        self.assert_helper_reaped()

    def test_bundled_cmux_wrapper_delegates_to_lazy_pi_through_path(self):
        wrapper = Path("/Applications/cmux.app/Contents/Resources/bin/cmux-pi-wrapper")
        if not wrapper.exists():
            self.skipTest("macOS cmux bundle is not installed")
        bin_dir = self.root / ".local/bin"
        bin_dir.mkdir(parents=True)
        script = bin_dir / "pi"
        script.write_text('#!/bin/sh\nexec /usr/bin/python3 "' + str(ROOT / "launcher.py") + '" "$@"\n')
        script.chmod(0o700)
        extension = self.root / ".pi/agent/extensions/cmux-session.ts"
        extension.parent.mkdir(parents=True)
        extension.write_text("// cmux-pi-session-extension-marker")
        self.env["PATH"] = str(bin_dir) + ":/usr/bin:/bin"
        self.start(["--session", "exact-id"], command=[str(wrapper)])
        assert self.child is not None
        self.until(lambda: self.queries() == 1)
        self.assertFalse((self.root / "launched.json").exists())
        self.state(True)
        self.event()
        self.assertEqual(self.child.wait(timeout=5), 0)
        self.assertEqual(json.loads((self.root / "launched.json").read_text())["args"],
                         ["--session", "exact-id"])
        self.assert_helper_reaped()

    def test_cached_title_is_added_only_when_the_waiter_wakes(self):
        cache = self.root / ".local/share/pi-lazy/titles.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"exact-id": {"title": "Example coding task"}}))
        args = ["--session", "exact-id", "--", "--print"]
        self.start(args)
        assert self.child is not None and self.master is not None
        self.until(lambda: self.queries() == 1)
        self.assertFalse((self.root / "launched.json").exists())
        # Read while the slave is open; macOS may discard unread output on close.
        banner = os.read(self.master, 4096).decode()
        self.assertIn("[pi-lazy] Conversation: Example coding task", banner)
        self.assertIn("\x1b]2;π · Example coding task\x07", banner)
        self.state(True)
        self.event()
        self.assertEqual(self.child.wait(timeout=5), 0)
        self.assertEqual(json.loads((self.root / "launched.json").read_text())["args"],
                         ["--name", "Example coding task", *args])
        self.assert_helper_reaped()

    def test_saved_name_wins_over_cache_and_empty_latest_name_clears_it(self):
        session = self.root / ".pi/agent/sessions/cwd/date_exact-id.jsonl"
        session.parent.mkdir(parents=True)
        cache = self.root / ".local/share/pi-lazy/titles.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"exact-id": {"title": "Inferred title"}}))
        entries = [{"type": "session", "id": "exact-id"},
                   {"type": "session_info", "name": "Old name"},
                   {"type": "session_info", "name": "Saved name"}]
        session.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
        env = dict(self.env, PI_CODING_AGENT_DIR=str(self.root / ".pi/agent"))
        with patch.dict(os.environ, HOME=str(self.root)):
            self.assertEqual(launcher.session_title(["--session", "exact-id"], env), ("Saved name", False))
            self.assertEqual(launcher.session_title(["--session", str(session)], env), ("Saved name", False))
            self.assertEqual(launcher.session_title(["--session", "exact-id", "--name", "Explicit"], env),
                             ("Explicit", False))
            with session.open("a") as stream:
                stream.write(json.dumps({"type": "session_info", "name": ""}) + "\n")
            self.assertEqual(launcher.session_title(["--session", "exact-id"], env), (None, False))

    def test_custom_session_directory_and_terminal_safe_titles(self):
        session = self.root / "custom/date_exact-id.jsonl"
        session.parent.mkdir()
        session.write_text(json.dumps({"type": "session_info", "name": "Custom name"}) + "\n")
        with patch.dict(os.environ, HOME=str(self.root)):
            self.assertEqual(launcher.session_title(["--session", "exact-id", "--session-dir", str(session.parent)],
                                                    self.env), ("Custom name", False))
        title = launcher.clean_title("Safe\x1b\x07\nTitle\x9b " + "x" * 100)
        assert title is not None
        self.assertTrue(all(char.isprintable() for char in title))
        self.assertLessEqual(len(title), 80)

    def test_headless_does_not_subscribe(self):
        self.start(["--mode", "rpc", "--session", "exact-id"], tty=False)
        assert self.child is not None
        self.assertEqual(self.child.wait(timeout=5), 0)
        self.assertFalse((self.root / "events.pid").exists())
        self.assertEqual(json.loads((self.root / "launched.json").read_text())["args"],
                         ["--mode", "rpc", "--session", "exact-id"])


if __name__ == "__main__":
    unittest.main()
