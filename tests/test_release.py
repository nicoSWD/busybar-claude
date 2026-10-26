from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import release  # noqa: E402

URL = "https://github.com/nicoSWD/busybar-claude.git"
ENTRY = {"name": "busy-claude", "description": "d", "source": "./", "category": "productivity"}


class PinEntryTests(unittest.TestCase):
    def test_a_local_entry_becomes_a_pinned_one_and_keeps_its_other_fields(self):
        marketplace = {"name": "m", "plugins": [{"name": "other", "source": "./x"}, dict(ENTRY)]}
        pinned = release.pin_entry(marketplace, "busy-claude", "1.2.0", URL, "a" * 40)
        entry = pinned["plugins"][1]
        self.assertEqual(list(entry), ["name", "description", "version", "source", "category"])
        self.assertEqual(entry["version"], "1.2.0")
        self.assertEqual(entry["source"], {"source": "url", "url": URL, "ref": "v1.2.0", "sha": "a" * 40})
        self.assertEqual(pinned["plugins"][0], {"name": "other", "source": "./x"})
        self.assertEqual(marketplace["plugins"][1]["source"], "./", "the input is not modified")

    def test_a_missing_plugin_is_an_error(self):
        with self.assertRaises(release.ReleaseError):
            release.pin_entry({"plugins": []}, "busy-claude", "1.0.0", URL, "a" * 40)


class ReleaseFlowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        base = Path(self.tempdir.name)
        # Isolate from the developer's git config (signing, hooks) and identity.
        patcher = mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.origin = base / "origin.git"
        self.work = base / "work"
        self.git(base, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git(base, "init", "-q", "-b", "main", str(self.work))
        self.git(self.work, "remote", "add", "origin", str(self.origin))
        (self.work / ".claude-plugin").mkdir()
        (self.work / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "busy-claude", "repository": URL[:-4]}))
        self.write_marketplace([dict(ENTRY)])
        self.commit("initial")
        self.git(self.work, "push", "-q", "-u", "origin", "main")

    def git(self, cwd, *args):
        return subprocess.run(["git"] + list(args), cwd=str(cwd), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True).stdout.strip()

    def commit(self, message):
        self.git(self.work, "add", "-A")
        self.git(self.work, "commit", "-q", "-m", message)

    def write_marketplace(self, plugins):
        (self.work / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"name": "m", "plugins": plugins}))

    def entry(self):
        data = json.loads((self.work / ".claude-plugin" / "marketplace.json").read_text())
        return data["plugins"][0]

    def run_release(self, version, **kwargs):
        messages = []
        options = dict(run_tests=False, github=False, out=messages.append)
        options.update(kwargs)
        release.release(self.work, version, **options)
        return messages

    def test_a_local_release_tags_the_code_then_commits_the_catalog_change(self):
        code = self.git(self.work, "rev-parse", "HEAD")
        messages = self.run_release("1.0.0")
        self.assertEqual(self.git(self.work, "rev-parse", "v1.0.0^{commit}"), code)
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD~1"), code, "the catalog commit sits on top of the tagged one")
        self.assertEqual(self.git(self.work, "cat-file", "-t", "v1.0.0"), "tag", "annotated")
        entry = self.entry()
        self.assertEqual(entry["version"], "1.0.0")
        self.assertEqual(entry["source"], {"source": "url", "url": URL, "ref": "v1.0.0", "sha": code})
        self.assertEqual(self.git(self.work, "log", "-1", "--format=%s"), "Release 1.0.0")
        self.assertEqual(self.git(self.work, "status", "--porcelain"), "")
        self.assertEqual(self.git(self.origin, "tag", "--list"), "", "nothing was pushed")
        self.assertIn("git push origin v1.0.0", "\n".join(messages))

    def test_push_sends_the_tag_and_main(self):
        self.run_release("1.0.0", push=True)
        self.assertEqual(self.git(self.origin, "tag", "--list"), "v1.0.0")
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.git(self.work, "rev-parse", "HEAD"))
        self.assertEqual(
            self.git(self.origin, "rev-parse", "v1.0.0^{commit}"), self.git(self.work, "rev-parse", "HEAD~1")
        )

    def test_a_second_release_moves_the_pin_and_older_versions_are_refused(self):
        self.run_release("1.0.0")
        (self.work / "change.txt").write_text("x")
        self.commit("work")
        second = self.git(self.work, "rev-parse", "HEAD")
        self.run_release("1.1.0")
        self.assertEqual(self.entry()["source"]["sha"], second)
        self.assertEqual(self.entry()["source"]["ref"], "v1.1.0")
        for version in ("1.1.0", "1.0.5"):
            with self.assertRaises(release.ReleaseError):
                self.run_release(version)

    def test_nothing_happens_when_the_checks_fail(self):
        with self.assertRaises(release.ReleaseError):
            self.run_release("1.0")  # not x.y.z
        (self.work / "dirty.txt").write_text("x")
        with self.assertRaises(release.ReleaseError):
            self.run_release("1.0.0")  # uncommitted changes
        self.git(self.work, "add", "-A")
        self.git(self.work, "commit", "-q", "-m", "clean")
        self.git(self.work, "checkout", "-q", "-b", "feature")
        with self.assertRaises(release.ReleaseError):
            self.run_release("1.0.0")  # not on main
        self.assertEqual(self.git(self.work, "tag", "--list"), "")

    def test_a_catalog_without_the_plugin_rolls_the_tag_back(self):
        self.write_marketplace([{"name": "something-else", "source": "./"}])
        self.commit("catalog")
        with self.assertRaises(release.ReleaseError):
            self.run_release("1.0.0")
        self.assertEqual(self.git(self.work, "tag", "--list"), "", "the tag is removed again")
        self.assertEqual(self.git(self.work, "status", "--porcelain"), "")

    def test_failing_tests_stop_the_release(self):
        ran = []

        def failing(root):
            ran.append(root)
            return 1

        with self.assertRaises(release.ReleaseError):
            self.run_release("1.0.0", run_tests=True, test_runner=failing)
        self.assertEqual(ran, [self.work])
        self.assertEqual(self.git(self.work, "tag", "--list"), "")
        self.run_release("1.0.0", run_tests=True, test_runner=lambda root: 0)
        self.assertEqual(self.git(self.work, "tag", "--list"), "v1.0.0")

    def test_dry_run_changes_nothing(self):
        before = self.git(self.work, "rev-parse", "HEAD")
        messages = self.run_release("1.0.0", dry_run=True, push=True)
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), before)
        self.assertEqual(self.git(self.work, "tag", "--list"), "")
        self.assertEqual(self.entry()["source"], "./")
        self.assertTrue(any("Would tag v1.0.0" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
