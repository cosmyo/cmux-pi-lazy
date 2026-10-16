import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lazy-install-test-")
        self.home = Path(self.temp.name).resolve()
        self.real = self.home / ".pi/agent/bin/pi"
        self.real.parent.mkdir(parents=True)
        self.real.write_text('#!/bin/sh\nprintf "real-pi"\n')
        self.real.chmod(0o700)
        self.bin = self.home / ".local/bin"
        self.bin.mkdir(parents=True)
        self.original = "../../.pi/agent/bin/pi"
        (self.bin / "pi").symlink_to(self.original)
        self.env = dict(os.environ, HOME=str(self.home))
        for key in ("PI_LAZY_REAL_PI", "PI_LAZY_ENTRYPOINT"):
            self.env.pop(key, None)

    def tearDown(self):
        self.temp.cleanup()

    def script(self, name, *args):
        return subprocess.run(["/bin/sh", str(ROOT / name), *args], env=self.env,
                              capture_output=True, text=True, timeout=10)

    def test_install_rerun_and_uninstall_restore_relative_symlink(self):
        self.assertEqual(self.script("install.sh").returncode, 0)
        record = self.home / ".local/share/pi-lazy/original-pi-backup"
        backup = record.read_text()
        self.assertEqual(os.readlink(self.bin / "pi"), "pi-lazy")
        self.assertEqual(self.script("install.sh").returncode, 0)
        self.assertEqual(record.read_text(), backup)
        result = subprocess.run([str(self.bin / "pi"), "--version"], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "real-pi")
        private_cache = self.home / ".local/share/pi-lazy/titles.json"
        private_cache.write_text(json.dumps({"private": {"title": "Private example"}}))
        self.assertEqual(self.script("uninstall.sh").returncode, 0)
        self.assertEqual(os.readlink(self.bin / "pi"), self.original)
        self.assertTrue(private_cache.exists())

    def test_hibernate_flag_writes_marker_and_preserves_custom_real_pi(self):
        custom = self.home / "custom-pi"
        custom.write_text('#!/bin/sh\nprintf "custom-pi"\n')
        custom.chmod(0o700)
        self.assertEqual(self.script("install.sh", "--pi", str(custom)).returncode, 0)
        result = self.script("install.sh", "--hibernate")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.home / ".local/share/pi-lazy"
        self.assertEqual((data / "native-hibernation").read_text().strip(), "1")
        self.assertTrue((data / "extensions/lifecycle.mjs").is_file())
        self.assertEqual((data / "real-pi").read_text().strip(), str(custom.resolve()))
        run = subprocess.run([str(self.bin / "pi"), "--version"], env=self.env,
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.stdout, "custom-pi")
        self.assertEqual(self.script("install.sh", "--no-hibernate").returncode, 0)
        self.assertEqual((data / "native-hibernation").read_text().strip(), "0")
        self.assertEqual((data / "real-pi").read_text().strip(), str(custom.resolve()))

    def test_uninstall_refuses_to_overwrite_a_changed_launcher(self):
        self.assertEqual(self.script("install.sh").returncode, 0)
        (self.bin / "pi").unlink()
        (self.bin / "pi").symlink_to("different-pi")
        self.assertNotEqual(self.script("uninstall.sh").returncode, 0)
        self.assertEqual(os.readlink(self.bin / "pi"), "different-pi")

    def test_reinstall_preserves_a_replacement_regular_launcher(self):
        self.assertEqual(self.script("install.sh").returncode, 0)
        record = self.home / ".local/share/pi-lazy/original-pi-backup"
        before = record.read_bytes()
        (self.bin / "pi").unlink()
        replacement = b'#!/bin/sh\nprintf "replacement-pi"\n'
        (self.bin / "pi").write_bytes(replacement)
        (self.bin / "pi").chmod(0o700)
        self.assertNotEqual(self.script("install.sh").returncode, 0)
        self.assertEqual((self.bin / "pi").read_bytes(), replacement)
        self.assertEqual(record.read_bytes(), before)

    def test_reinstall_preserves_a_replacement_symlink(self):
        self.assertEqual(self.script("install.sh").returncode, 0)
        (self.bin / "pi").unlink()
        (self.bin / "pi").symlink_to("different-pi")
        self.assertNotEqual(self.script("install.sh").returncode, 0)
        self.assertEqual(os.readlink(self.bin / "pi"), "different-pi")

    def test_first_install_resolves_an_original_relative_pi_symlink(self):
        result = self.script("install.sh", "--pi", str(self.bin / "pi"))
        self.assertEqual(result.returncode, 0, result.stderr)
        configured = self.home / ".local/share/pi-lazy/real-pi"
        self.assertEqual(configured.read_text().strip(), str(self.real.resolve()))
        result = subprocess.run([str(self.bin / "pi"), "--version"], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "real-pi")

    def test_first_install_refuses_to_overwrite_its_real_regular_executable(self):
        (self.bin / "pi").unlink()
        original = b'#!/bin/sh\nprintf "original-local-pi"\n'
        (self.bin / "pi").write_bytes(original)
        (self.bin / "pi").chmod(0o700)
        self.assertNotEqual(self.script("install.sh", "--pi", str(self.bin / "pi")).returncode, 0)
        self.assertEqual((self.bin / "pi").read_bytes(), original)
        self.assertFalse((self.home / ".local/share/pi-lazy/original-pi-backup").exists())

    def test_recursive_real_pi_is_rejected(self):
        self.assertEqual(self.script("install.sh").returncode, 0)
        result = self.script("install.sh", "--pi", str(self.bin / "pi"))
        self.assertNotEqual(result.returncode, 0)
        env = dict(self.env, PI_LAZY_REAL_PI=str(self.bin / "pi"))
        result = subprocess.run([str(self.bin / "pi"), "--version"], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 127)
        self.assertIn("recursive", result.stderr)

    def test_generated_cmux_shim_is_not_a_real_pi_target(self):
        shim = self.home / "cmux-cli-shims/example/pi"
        shim.parent.mkdir(parents=True)
        shim.write_text('#!/bin/sh\nprintf "not-the-real-agent"\n')
        shim.chmod(0o700)
        self.assertNotEqual(self.script("install.sh", "--pi", str(shim)).returncode, 0)
        self.assertEqual(os.readlink(self.bin / "pi"), self.original)
        self.assertFalse((self.home / ".local/share/pi-lazy/original-pi-backup").exists())

    def test_custom_real_pi_and_unrelated_lazy_launcher(self):
        custom = self.home / "custom-pi"
        custom.write_text('#!/bin/sh\nprintf "custom-pi"\n')
        custom.chmod(0o700)
        (self.bin / "pi-lazy").write_text("unrelated launcher\n")
        self.assertNotEqual(self.script("install.sh", "--pi", str(custom)).returncode, 0)
        self.assertEqual(os.readlink(self.bin / "pi"), self.original)
        (self.bin / "pi-lazy").unlink()
        self.assertEqual(self.script("install.sh", "--pi", str(custom)).returncode, 0)
        result = subprocess.run([str(self.bin / "pi"), "--version"], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.stdout, "custom-pi")


if __name__ == "__main__":
    unittest.main()
