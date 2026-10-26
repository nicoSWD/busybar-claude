from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


class MarketplaceTests(unittest.TestCase):
    def setUp(self):
        self.marketplace = load(".claude-plugin/marketplace.json")
        self.manifest = load(".claude-plugin/plugin.json")

    def test_the_repo_is_its_own_marketplace(self):
        self.assertEqual(len(self.marketplace["plugins"]), 1)
        entry = self.marketplace["plugins"][0]
        self.assertEqual(entry["name"], self.manifest["name"], "install id and manifest name must match")
        self.assertTrue(self.marketplace["owner"]["name"])

    def test_the_entry_is_either_local_or_pinned_to_a_release_tag(self):
        entry = self.marketplace["plugins"][0]
        if entry["source"] == "./":
            return  # before the first release: the plugin is the repository root
        source = entry["source"]
        self.assertEqual(source["source"], "url")
        self.assertEqual(source["url"], self.manifest["repository"] + ".git")
        self.assertRegex(entry["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(source["ref"], "v" + entry["version"], "the tag matches the version")
        self.assertRegex(source["sha"], r"^[0-9a-f]{40}$", "pinned to the exact commit")

    def test_names_follow_the_marketplace_rules(self):
        name = self.marketplace["name"]
        self.assertRegex(name, r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
        self.assertNotIn(name, {"claude-plugins-official", "anthropic-plugins"})
        self.assertFalse(re.match(r"^(claude|anthropic|cc-plugin)-", self.manifest["name"]), "reserved plugin prefix")

    def test_the_version_lives_on_the_marketplace_entry_only(self):
        # Two places would let plugin.json win silently; with a release tag the entry is the single source.
        self.assertNotIn("version", self.manifest)
        self.assertNotIn("version", self.marketplace)

    def test_entry_declares_no_components_that_could_conflict_with_the_manifest(self):
        entry = self.marketplace["plugins"][0]
        for key in ("commands", "agents", "skills", "hooks", "mcpServers", "lspServers", "userConfig"):
            self.assertNotIn(key, entry)

    def test_readme_install_commands_match_the_marketplace(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        install = "/plugin install %s@%s" % (self.manifest["name"], self.marketplace["name"])
        self.assertIn(install, readme)
        self.assertIn("/plugin marketplace add nicoSWD/busybar-claude", readme)
        self.assertLess(readme.index("## Install"), readme.index("## What the official APIs support"))


class SetupSkillTests(unittest.TestCase):
    def test_setup_skill_is_user_invoked_and_runs_doctor(self):
        text = (ROOT / "skills" / "setup" / "SKILL.md").read_text(encoding="utf-8")
        header = text.split("---")[1]
        self.assertIn("name: setup", header)
        self.assertIn("disable-model-invocation: true", header)
        self.assertIn('busybar_status.py" doctor', text)
        self.assertNotIn("password:", text.lower().replace("the password", ""), "never collect secrets in chat")


FAKE_CLAUDE = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$*" in
  "plugin marketplace add"*) [ -z "${FAKE_ADD_FAIL:-}" ] || { echo "add failed" >&2; exit 1; } ;;
  "plugin marketplace list") [ -z "${FAKE_LISTED:-}" ] || echo "busybar-claude" ;;
  "plugin install"*) [ -z "${FAKE_INSTALL_FAIL:-}" ] || { echo "install failed" >&2; exit 1; } ;;
  "plugin list") [ -z "${FAKE_INSTALLED:-}" ] || echo "busy-claude@busybar-claude" ;;
  "plugin configure"*) cat >> "$FAKE_LOG.stdin" ;;
esac
exit 0
"""

FAKE_OLD_PYTHON = """#!/bin/sh
case "$*" in
  *version_info\ \>=*) exit 1 ;;
  *) echo 3.8 ;;
esac
"""


class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "claude.log"
        self.install_fake("claude", FAKE_CLAUDE)

    def install_fake(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def run_script(self, *args, path=None, **env):
        environment = {
            "PATH": path or "%s:%s" % (self.bin, os.environ["PATH"]),
            "HOME": str(self.root),
            "FAKE_LOG": str(self.log),
        }
        environment.update(env)
        return subprocess.run(
            ["sh", str(ROOT / "install.sh")] + list(args),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=environment, universal_newlines=True, timeout=60,
        )

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_default_install_adds_the_marketplace_then_installs_and_asks_nothing(self):
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls(), [
            "plugin marketplace add nicoSWD/busybar-claude",
            "plugin install busy-claude@busybar-claude --scope user",
        ])
        self.assertFalse(Path(str(self.log) + ".stdin").exists(), "a USB bar needs no configuration")
        self.assertIn("/busy-claude:setup", result.stdout)

    def test_without_a_terminal_the_defaults_apply(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.calls()), 2)

    def test_dry_run_prints_the_commands_and_runs_none(self):
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls(), [])
        self.assertIn("+ claude plugin marketplace add nicoSWD/busybar-claude", result.stdout)
        self.assertIn("+ claude plugin install busy-claude@busybar-claude --scope user", result.stdout)

    def test_the_repository_can_be_overridden_for_testing(self):
        self.assertEqual(self.run_script("--yes", BUSYBAR_REPO="/some/clone").returncode, 0)
        self.assertEqual(self.calls()[0], "plugin marketplace add /some/clone")

    def test_an_existing_marketplace_and_plugin_are_refreshed_not_an_error(self):
        result = self.run_script("--yes", FAKE_ADD_FAIL="1", FAKE_LISTED="1", FAKE_INSTALL_FAIL="1", FAKE_INSTALLED="1")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("plugin marketplace update busybar-claude", self.calls())
        self.assertIn("plugin update busy-claude@busybar-claude", self.calls())

    def test_real_failures_stop_the_install_with_a_message(self):
        result = self.run_script("--yes", FAKE_ADD_FAIL="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not add the marketplace", result.stdout)
        self.assertNotIn("plugin install busy-claude@busybar-claude --scope user", self.calls())
        self.log.unlink()
        result = self.run_script("--yes", FAKE_INSTALL_FAIL="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not install", result.stdout)

    def test_missing_claude_and_old_python_are_reported_before_anything_changes(self):
        result = self.run_script("--yes", path="/usr/bin:/bin")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Claude Code was not found", result.stdout)
        self.install_fake("python3", FAKE_OLD_PYTHON)
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Python 3.9 or newer is required", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_uninstall_removes_the_plugin_and_the_marketplace(self):
        result = self.run_script("--uninstall")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls(), [
            "plugin uninstall busy-claude@busybar-claude",
            "plugin marketplace remove busybar-claude",
        ])

    def test_unknown_options_are_refused(self):
        result = self.run_script("--frobnicate")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_the_wifi_password_only_travels_through_a_pipe(self):
        text = (ROOT / "install.sh").read_text()
        configure = [line for line in text.splitlines() if "claude plugin configure" in line]
        self.assertEqual(len(configure), 1)
        self.assertIn("--values-stdin", configure[0])
        self.assertNotIn("$password", configure[0])
        self.assertNotIn("--config", text, "--config would put the password on a command line")

    def test_scripts_parse(self):
        self.assertEqual(subprocess.run(["sh", "-n", str(ROOT / "install.sh")]).returncode, 0)
        pwsh = shutil.which("pwsh") or shutil.which("powershell")
        if pwsh is None:
            self.skipTest("PowerShell is not installed here: install.ps1 was not parsed")
        check = "$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile('%s', [ref]$null, [ref]$e); exit [int]($e.Count -gt 0)" % (ROOT / "install.ps1")
        self.assertEqual(subprocess.run([pwsh, "-NoProfile", "-Command", check]).returncode, 0)

    def test_readme_shows_one_liners_that_match_the_scripts(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("curl -fsSL https://raw.githubusercontent.com/nicoSWD/busybar-claude/main/install.sh | sh", readme)
        self.assertIn("irm https://raw.githubusercontent.com/nicoSWD/busybar-claude/main/install.ps1 | iex", readme)
        self.assertIn("nicoSWD/busybar-claude", (ROOT / "install.sh").read_text())
        self.assertIn("nicoSWD/busybar-claude", (ROOT / "install.ps1").read_text())


if __name__ == "__main__":
    unittest.main()
