---
name: setup
description: Check that the BUSY Bar plugin is installed and connected, and fix what is not. Run it after installing, or when the bar does not react.
disable-model-invocation: true
---

# BUSY Bar setup check

Run the plugin's checklist and help the user fix anything that fails.

## 1. Run the check

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/busybar_status.py" doctor
```

If that path does not exist (the variable was not expanded), find the script and run it with `doctor`:

```sh
find ~/.claude/plugins/cache -name busybar_status.py -path '*busy-claude*'
```

On Windows use `python3` the same way, with the plugin path from `claude plugin list`.

Show the user the checklist as it came out, then explain in one or two sentences what each `[FAIL]` means. `[--]` lines are information, not problems.

## 2. Fix what failed

- **"No hook has run yet"**: the plugin was installed in this session. Ask the user to run `/reload-plugins` (or start a new session), do something that triggers a hook (send any prompt), then run this setup check again.
- **"The bar does not answer"**: the bar needs to be plugged in with USB-C and awake. If the user connects it over Wi-Fi instead, continue below.
- **"The last attempt to draw failed"**: read the error text. Timeouts and connection errors mean the bar is unreachable or the address is wrong; a 401 or 403 means the password is wrong.
- **"Installed twice"**: tell the user to remove one copy, either the folder `~/.claude/skills/busy-claude` or `claude plugin uninstall busy-claude@busybar-claude`. Do not delete anything yourself.
- **Python or `python3` not found** (mostly Windows): the hooks run `python3`. The README section "Linux and Windows" explains how to make it available.

## 3. Wi-Fi or LAN setup

If the bar is not on USB, the user must enter the connection details themselves, because the password must not appear in this chat. Tell them to:

1. Enable the Wi-Fi API once over USB in the bar's web UI (see the README, "Connect the BUSY Bar").
2. Run `/plugin configure busy-claude@busybar-claude` and set the device URL (`http://<bar-ip>/api`), the authentication mode (`wifi`) and the token (the API password).
3. Run `/reload-plugins`, send any prompt, then run this setup check again.

Never ask the user to type the password to you and never put it in a command.

## 4. Optional: plan usage on the READY screen

Mention once that the READY screen can show plan usage if the statusline wrapper is set up (the check lists whether it is). Only change `~/.claude/settings.json` if the user asks you to, make a backup copy first, and keep their existing `statusLine` command by passing it with `--next` (see the README section on the optional statusline).
