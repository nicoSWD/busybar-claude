# AGENTS.md

Guidance for AI coding agents and contributors working in this repository. The most important part is
how releases work, because it is not what you would assume.

## What this is

A Claude Code plugin (`busy-claude`) that shows Claude's state on a BUSY Bar display. The repository is
both the plugin and its own marketplace (`busybar-claude`). Python standard library only.

- `scripts/busybar_status.py`: hooks, state, drawing, `doctor`, `statusline` commands
- `hooks/hooks.json`, `skills/setup/SKILL.md`: what Claude Code loads
- `install.sh`, `install.ps1`: one-line installers
- `tools/release.py`: the release tool (below)
- Tests: `python3 -m unittest discover -s tests -q` (must pass before any commit)

## How releases work

**Users do not get `main`. They get a tag.**

`.claude-plugin/marketplace.json` is read from `main`, but its plugin entry does not point at `main`. It
points at a release:

```json
"version": "1.0.0",
"source": { "source": "url", "url": "https://github.com/nicoSWD/busybar-claude.git",
            "ref": "v1.0.0", "sha": "<commit the tag points at>" }
```

Claude Code installs the plugin from that tag and commit. So:

- Pushing to `main` changes nothing for users, unless the push changes that entry.
- Moving the entry to a new tag is what "releasing" means. Users then get it with
  `claude plugin update busy-claude@busybar-claude`, or through auto-update if they enabled it.
- The `version` field exists only on the marketplace entry. Never add `version` to `plugin.json`
  (`plugin.json` would silently win, and a test enforces this).

### Cutting a release

Only when the maintainer asks for one.

```sh
tools/release.py 1.2.0            # tests, tag v1.2.0, commit "Release 1.2.0"; nothing leaves the machine
tools/release.py 1.2.0 --push     # the same, then push the tag, push main, create the GitHub release
tools/release.py 1.2.0 --dry-run  # check everything, change nothing
```

What it does, in order:

1. Refuses unless the working tree is clean, you are on `main`, the tag does not exist, the version is
   `x.y.z` and newer than the current one, and the tests pass.
2. Creates the annotated tag `vX.Y.Z` on the current commit. This is the code users receive.
3. Makes a second commit, `Release X.Y.Z`, that rewrites the marketplace entry to that tag and its SHA.
   It is a separate commit because a commit cannot contain its own hash.
4. With `--push`: pushes the tag first, then `main`, then runs `gh release create` with generated notes.
   The tag must reach the server before the catalog that names it.

If the catalog edit fails after the tag was made, the script deletes the tag again.

### Rules

- **Never edit `ref`, `sha` or `version` in `marketplace.json` by hand.** Use the script. The test suite
  checks that `ref` is `v` + `version` and that `sha` is a full 40-character commit.
- **Never move, delete or force-push a published tag.** Users and the catalog pin its commit. To fix a
  bad release, make a new patch release.
- **Do not release on your own initiative.** Tagging, pushing and creating a GitHub release are
  outward-facing. Do them only when asked, and say what you are about to publish.
- **Release from `main` only**, with everything intended for the release already committed before the
  tag is made. Anything committed after `Release X.Y.Z` is not in that release.
- Tags are signed if the maintainer's git config signs them; do not override that configuration.
- The release tool's own tests (`tests/test_release.py`) run it against temporary repositories with the
  git config and identity isolated. Keep it that way: never point a test at the real repository.

### Testing changes without a release

Adding this repository as a marketplace installs the released tag from GitHub, not your working tree.
To try local changes, run `claude --plugin-dir .` in a session. To test the installers or marketplace
flow without touching the real Claude Code configuration, use a throwaway home directory, for example
`HOME=$(mktemp -d) claude plugin marketplace add ...`, and delete it afterwards.

## Other conventions

- Run the full test suite before committing. Add tests with new behavior.
- Do not commit `.DS_Store`, `.idea/`, or anything under `__pycache__/` (all ignored already).
- Commit messages: short subject, a body explaining why. Anything you push is public once the repository
  is, so keep secrets, tokens and personal paths out of commits, docs and tests.
- `README.md` is the user-facing documentation and starts with Install. Keep install commands in the
  README consistent with the marketplace name `busybar-claude` and plugin name `busy-claude`
  (`tests/test_install.py` checks this).
