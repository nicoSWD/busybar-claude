from __future__ import annotations

import json
import re
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
        self.assertEqual(entry["source"], "./", "the plugin is the repository root")
        self.assertTrue(self.marketplace["owner"]["name"])

    def test_names_follow_the_marketplace_rules(self):
        name = self.marketplace["name"]
        self.assertRegex(name, r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
        self.assertNotIn(name, {"claude-plugins-official", "anthropic-plugins"})
        self.assertFalse(re.match(r"^(claude|anthropic|cc-plugin)-", self.manifest["name"]), "reserved plugin prefix")

    def test_version_comes_from_the_commit(self):
        # With no version anywhere, every commit counts as an update. A pinned version would need a bump per release.
        self.assertNotIn("version", self.manifest)
        self.assertNotIn("version", self.marketplace["plugins"][0])
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


if __name__ == "__main__":
    unittest.main()
