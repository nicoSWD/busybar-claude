#!/usr/bin/env python3
"""Cut a release: tag the code, then point the marketplace entry at that tag.

    tools/release.py 1.2.0            tag v1.2.0 and commit the catalog change (nothing leaves this machine)
    tools/release.py 1.2.0 --push     the same, then push the tag and main and create the GitHub release

Users install from the tag the marketplace entry names, so pushing to main does not reach them until a
release moves that entry. The entry's commit is separate from the tagged one: a commit cannot contain its
own hash, so the tag is made first and the catalog commit records its SHA.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = "busy-claude"
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


class ReleaseError(Exception):
    pass


def pin_entry(marketplace: Dict[str, Any], name: str, version: str, url: str, sha: str) -> Dict[str, Any]:
    """The marketplace with `name`'s entry pinned to the tag for `version` (version only lives on the entry)."""
    entries = marketplace.get("plugins", [])
    for index, entry in enumerate(entries):
        if entry.get("name") == name:
            break
    else:
        raise ReleaseError("the marketplace has no plugin named %s" % name)
    pinned = {"name": entry["name"]}
    if "description" in entry:
        pinned["description"] = entry["description"]
    pinned["version"] = version
    pinned["source"] = {"source": "url", "url": url, "ref": "v" + version, "sha": sha}
    pinned.update({key: value for key, value in entry.items() if key not in pinned and key != "source"})
    updated = dict(marketplace)
    updated["plugins"] = entries[:index] + [pinned] + entries[index + 1:]
    return updated


def current_version(marketplace: Dict[str, Any], name: str) -> Optional[str]:
    for entry in marketplace.get("plugins", []):
        if entry.get("name") == name:
            return entry.get("version")
    return None


def repository_url(root: Path) -> str:
    manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    url = manifest.get("repository")
    if not url:
        raise ReleaseError("plugin.json has no repository URL")
    return url if url.endswith(".git") else url + ".git"


def parse_version(text: str) -> List[int]:
    return [int(part) for part in text.split(".")]


def run_unittests(root: Path) -> int:
    return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], cwd=str(root)).returncode


def release(
    root: Path,
    version: str,
    push: bool = False,
    github: bool = True,
    run_tests: bool = True,
    dry_run: bool = False,
    out: Callable[[str], None] = print,
    test_runner: Optional[Callable[[Path], int]] = None,
) -> None:
    if not SEMVER.match(version):
        raise ReleaseError("the version must look like 1.2.3, not %r" % version)
    tag = "v" + version

    def git(*args: str, check: bool = True) -> str:
        result = subprocess.run(["git"] + list(args), cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if check and result.returncode != 0:
            raise ReleaseError("git %s failed: %s" % (" ".join(args), result.stderr.strip()))
        return result.stdout.strip()

    if git("status", "--porcelain"):
        raise ReleaseError("the working tree has uncommitted changes; commit or stash them first")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        raise ReleaseError("releases are cut from main, you are on %s" % branch)
    if git("tag", "--list", tag):
        raise ReleaseError("the tag %s already exists" % tag)

    marketplace_path = root / ".claude-plugin" / "marketplace.json"
    marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    previous = current_version(marketplace, PLUGIN)
    if previous and parse_version(version) <= parse_version(previous):
        raise ReleaseError("%s is not newer than the current release %s" % (version, previous))
    url = repository_url(root)

    if run_tests:
        out("Running the tests ...")
        if (test_runner or run_unittests)(root) != 0:
            raise ReleaseError("the tests fail; fix them before releasing")

    sha = git("rev-parse", "HEAD")
    if dry_run:
        out("Would tag %s at %s, pin the marketplace entry to it and commit \"Release %s\"." % (tag, sha[:12], version))
        if push:
            out("Would push %s and main, then create the GitHub release." % tag)
        return

    git("tag", "-a", tag, "-m", "%s %s" % (PLUGIN, version))
    try:
        updated = pin_entry(marketplace, PLUGIN, version, url, sha)
        marketplace_path.write_text(json.dumps(updated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        git("add", str(marketplace_path.relative_to(root)))
        git("commit", "-m", "Release %s" % version, "-m", "Point the marketplace entry at %s (%s)." % (tag, sha[:12]))
    except Exception:
        git("tag", "-d", tag, check=False)
        git("checkout", "--", ".claude-plugin/marketplace.json", check=False)
        raise
    out("Tagged %s at %s and committed the catalog change." % (tag, sha[:12]))

    if not push:
        out("Review it, then publish with:\n  git push origin %s && git push origin main" % tag)
        out("  gh release create %s --title \"%s %s\" --generate-notes --verify-tag" % (tag, PLUGIN, version))
        return
    # The tag goes first: the catalog on main must never name a tag that is not on the server yet.
    git("push", "origin", tag)
    git("push", "origin", "main")
    out("Pushed %s and main." % tag)
    if github:
        if not shutil.which("gh"):
            out("The gh CLI was not found; create the release on GitHub for %s." % tag)
            return
        result = subprocess.run(
            ["gh", "release", "create", tag, "--title", "%s %s" % (PLUGIN, version), "--generate-notes", "--verify-tag"],
            cwd=str(root),
        )
        if result.returncode != 0:
            raise ReleaseError("the tag and main are pushed, but gh could not create the release")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Tag a release and point the marketplace at it.")
    parser.add_argument("version", help="the new version, for example 1.2.0")
    parser.add_argument("--push", action="store_true", help="push the tag and main, then create the GitHub release")
    parser.add_argument("--no-github", action="store_true", help="with --push: do not create a GitHub release")
    parser.add_argument("--skip-tests", action="store_true", help="do not run the test suite first")
    parser.add_argument("--dry-run", action="store_true", help="check everything, change nothing")
    args = parser.parse_args(argv)
    try:
        release(
            ROOT, args.version, push=args.push, github=not args.no_github,
            run_tests=not args.skip_tests, dry_run=args.dry_run,
        )
    except ReleaseError as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
