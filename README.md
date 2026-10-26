# Claude Code → BUSY Bar status plugin

An installable Claude Code plugin that mirrors Claude's lifecycle state on the
BUSY Bar front display. It uses Claude Code lifecycle hooks, shared per-plugin
state, and BUSY Bar's HTTP API. It does not add tools or prompt instructions,
so status changes consume no model tokens.

![The bar while Claude is working: the mascot types behind a laptop, "WORKING" in blue, the current tool as a white subtitle, and a comet sweeping along the bottom](docs/working.gif)

*The **WORKING** state on a real BUSY Bar (captured from the device, 2 s loop):
the mascot types behind a laptop, the subtitle names the tool Claude is using
right now, and a comet sweeps the bottom edge. The other states are described
under [Display layout](#display-layout).*

## What the official APIs support

BUSY Bar is an HTTP server. The same API is available through:

| Connection | API base URL | Authentication |
| --- | --- | --- |
| USB | `http://10.0.4.20/api` | None |
| Wi-Fi LAN | `http://DEVICE_IP/api` | Wi-Fi API password in `X-API-Token` |
| BUSY cloud | `https://api.busy.app/busybar` | BUSY account API token as `Authorization: Bearer …` |

Wi-Fi API access is disabled by default. Connect over USB, open
`http://10.0.4.20`, and enable HTTP API access on the Network tab before using
LAN access. The official Python library can browse `_http._tcp` mDNS instances
whose names start with `busybar-`, but its documentation notes that shipped
firmware may not advertise the service. This plugin therefore defaults to the
reliable USB address and accepts a configured LAN address.

The plugin sends `POST /display/draw` with a `text` title for the 72×16 RGB
front display, plus a looping `animation` element for the mascot; while working
it adds a white `text` subtitle and a second `animation` for the comet. Text and
`#RRGGBBAA` colors are documented display features. The animations are small
`.anim` files the plugin uploads to the bar once (see
[Display layout](#display-layout)); no companion application, Bluetooth pairing,
WebSocket, or custom firmware is required.

Claude Code plugins load hook configuration from `hooks/hooks.json`. Command
hooks receive event JSON on standard input, including `session_id`, and receive
`CLAUDE_PLUGIN_ROOT`, `CLAUDE_PLUGIN_DATA`, and configured plugin options in the
environment. This implementation uses those documented interfaces only.

## State behavior

| Claude Code event | Session state |
| --- | --- |
| `SessionStart` (startup, resume, clear, fork) | `READY` (open, waiting for a prompt) |
| `SessionStart` (compact) | unchanged (compaction is not a new session) |
| `UserPromptSubmit` | `WORKING` |
| ordinary `PreToolUse`, `PostToolUse` | `WORKING` |
| `PreCompact` | `WORKING`, subtitle `Compacting` |
| `PostCompact` | back to the state `PreCompact` interrupted: `WORKING` after a mid-turn compaction, `READY` / `FINISHED` after a manual or idle one |
| `PreToolUse` for `AskUserQuestion` or `ExitPlanMode` | `WAITING_FOR_INPUT` |
| `PermissionRequest`, `Elicitation`, relevant wait notifications | `WAITING_FOR_INPUT` |
| `ElicitationResult`, permission denial, resume notifications | `WORKING` |
| `PostToolUseFailure` | `WORKING` (a tool failure is not a terminal failure) |
| `Stop` | `FINISHED` |
| `StopFailure` with `error: rate_limit` | `LIMIT` (held up by a usage limit; Claude may resume on its own, which shows as `WORKING` again) |
| any other `StopFailure` | `ERROR` |
| `SessionEnd` | session removed; `IDLE` if no others remain |

Multiple sessions share an atomically locked state file under
`CLAUDE_PLUGIN_DATA`. The visible aggregate uses this precedence:

`WORKING` → `WAITING_FOR_INPUT` → `LIMIT` → `ERROR` → `FINISHED` → `READY` → `IDLE`

Consequently one active session keeps the bar blue even if another session is
waiting or finished. Identical aggregate states are not resent. Device I/O is
performed in a detached process, so hook execution does not wait on the network.
A single lightweight reaper removes sessions that have emitted no hook event
for the configured stale timeout; this recovers from terminal crashes and
unclean Claude Code exits.

### Detection limits

The plugin only knows what Claude Code's hooks tell it, and a few moments have
no hook. It never uses timers to guess.

- **Permission answered.** No hook fires when you approve or deny a permission
  prompt, so `WAITING` clears on the next tool event (`PostToolUse` or
  `PreToolUse`), not at the moment you answer.
- **Interrupted turn.** `Stop` does not run when you interrupt with Esc, so the
  bar stays on `WORKING` until your next prompt or the stale-session timeout.
- **Crash or kill.** No `SessionEnd` arrives; the session is dropped after the
  stale-session timeout (one hour by default).
- **Idle reminder.** About a minute after a response finishes with no input,
  Claude Code sends an `idle_prompt` notification and the bar changes from
  `FINISHED` to `WAITING`. It does not do this while waiting out a usage limit
  or while a background agent is running.
- **Permission-mode changes.** No hook fires when you switch modes (Shift+Tab, or
  plan mode starting and ending). The badge updates on the next event that carries the
  mode (a prompt or any tool call), so a change made while the session is idle shows up
  late. `SessionStart`, notifications, `StopFailure` and compaction events do not carry it;
  the last known mode is kept for those, and a new session shows no badge until its first prompt.
- **Several sessions.** One bar shows the highest-priority state across all
  open sessions (see the precedence above), so a busy session hides a finished
  one.
- **Subagents and background work.** Their tool hooks arrive under the same
  session and move the subtitle, but there is no separate "background agent
  finished" state.

## Display layout

```text
x 0-15    mascot      16x16 looping animation, one per state
          text        centered on the whole display
            rows 1-5    WORKING          small font, state color (rows 6-10, centered, when alone)
            rows 8-11   Editing          tiny font, white (WORKING only)
            rows 14-15  comet            sweeping the column (WORKING only)
x 52-71   rows 0-5   permission-mode badge (not shown in the default mode)
```

### Mascot

A pixel-art Claude (orange, blocky, eyes as cut-outs) reacts to the state:

| State | Animation |
| --- | --- |
| `WORKING` | sits behind a laptop typing, a glowing logo on the lid, ellipsis dots pulsing overhead |
| `WAITING_FOR_INPUT` | glances left and right, taps a foot, amber `?` bobbing; the label reads `WAITING` followed by animated dots (`.`, `..`, `...`) |
| `LIMIT` | worn out and sweating beside a running hourglass |
| `FINISHED` | jumps twice in a shower of confetti, then waves with twinkling sparkles |
| `ERROR` | dizzy X eyes, shakes, red `!`, a puff of smoke |
| `READY` | waves hello, blinks, a `>_` prompt with a blinking cursor overhead |
| `IDLE` | asleep and breathing, dim, little z's drifting up |

The art is drawn procedurally in `scripts/busybar_mascot.py` (sprite, eye styles,
and one frame builder per state), so it is easy to tweak; bump `MASCOT_VERSION`
afterwards. Set `MASCOT` to `false` to get the full-width text layout back.

### Subtitle and comet

The subtitle says what Claude is doing, derived from the tool in `PreToolUse`:
`Reading`, `Editing`, `Running`, `Searching`, `Browsing`, `Subagent`, `Tool`
(MCP), `Compacting`, `Working` (anything else), or `Thinking` (after a prompt or session start
and after a permission wait). `PostToolUse` keeps the previous subtitle. Every
other state shows the label only.

The comet is a glowing head that eases back and forth along a dim rail, trail
fading behind it, in the configured `WORKING` color
(`scripts/busybar_anim.py`, which also holds a pure-Python encoder for the bar's
`.anim` format). Set `WORKING_ANIMATION` to `false` to skip it.

Text is centered on the whole display. A label too wide to clear the mascot
(over about 8 characters in the title font) is centered in the 56px column
beside it instead, and one too wide for that column (about 11 characters) is
anchored at the column's left edge and scrolls rather than covering the mascot.

### Permission-mode badge

Claude Code reports the session's permission mode in the hook input (`permission_mode`),
so the bar can show it as a small colored pill in the top-right corner, the word cut
out of the pill:

| Mode | Badge |
| --- | --- |
| Plan mode | `PLAN` (teal) |
| Accept edits | `EDIT` (blue) |
| Auto mode | `AUTO` (green) |
| Don't ask | `LOCK` (gray) |
| Bypass permissions | `RISK` (red) |
| Default ("Manual") | no badge |

The badge follows the session whose state the bar is showing and stays across state changes.
Changing it redraws only the pill, so the mascot and comet keep animating. The words are
four letters so the pill (at most 19 px wide) clears the title. Set `MODE_BADGE` to `false`
to turn it off. The tiny-font glyph widths needed to size the pill are in
`scripts/busybar_font.py`.

### Waiting dots

While Claude waits for input the label is followed by three dots that appear one
after another. Text cannot animate, so the dots are a tiny 5×1 looping animation
(`busybar_anim.dots_animation`) placed on the label's baseline one pixel after its
last letter, in the `WAITING` color. The label and dots are centered together as
one unit, which needs exact label widths: `scripts/busybar_font.py` holds the glyph
widths of the title font, measured from a real bar's framebuffer. If the label plus
dots would not fit, or the dots' asset could not be uploaded, the plain label is
shown. Set `WAITING_DOTS` to `false` to turn them off.

### Regenerating the picture

`docs/working.gif` is captured from a real bar: `tools/capture_screenshot.py`
draws the state, reads the front display's framebuffer back from
`GET /api/screen` for one animation loop, and renders the frames as LED pixels.
It needs [Pillow](https://pypi.org/project/pillow/) (the plugin itself needs only
the standard library):

```sh
python3 tools/capture_screenshot.py --url 10.0.4.20 --state WORKING --activity Editing
```

`--state` takes any of the seven states. `tools/make_icon.py` renders the plugin
icon the same way.

### How it is drawn

All animations are uploaded once to the device as versioned assets
(`mascot-<state>-v<N>.anim`, `working-<hash>.anim`, 5-12 KB each) and then play
on the bar itself, with no process of ours running.

Redraws are incremental. The device merges draws by element id, so a tool call
that only changes the subtitle sends just the subtitle and neither the mascot
nor the comet restarts. A state change swaps the mascot's asset, sends the new
label, and erases whatever the new state does not use.

Design constraints learned from the device, all handled in
`scripts/busybar_status.py`:

- A missing asset is drawn as a magenta box, so an animation element is only
  drawn after its asset's presence (name and size) is confirmed through
  `GET /storage/list`. If an upload fails, the bar shows text only (layout
  unchanged) and retries after a minute.
- The device answers HTTP 508 when you re-upload an asset it is displaying, and
  removing an in-use asset can hang its web server. Assets are therefore
  immutable and versioned; the plugin never overwrites or removes them, so a
  color or art change leaves the old file behind (a few KB).
- Font and position constants sit at the top of the script; bump
  `LAYOUT_VERSION` after changing them.

## Requirements

- macOS (primary target; the state lock uses POSIX `flock`)
- Claude Code with plugin support and the hook events in `hooks/hooks.json`
- Python 3.9 or newer (the macOS `/usr/bin/python3` supplied by Xcode tools is
  sufficient)
- BUSY Bar with a current firmware/API

The runtime uses only Python's standard library. Installing `busylib` is not
required. The direct HTTP approach is the simplest reliable option here: the
official API is language-independent, each update is one small request, and it
avoids installing a package into Claude Code's runtime just to draw one text
element.

## Setup

Four steps: install the plugin, connect the bar (USB or LAN), tell the plugin
where the bar is (LAN only), and check that it works.

### 1. Install the plugin

Put this folder in Claude Code's personal skills directory:

```sh
mkdir -p ~/.claude/skills
cp -R /path/to/busy-claude ~/.claude/skills/busy-claude
# or: git clone <repository-url> ~/.claude/skills/busy-claude
```

Claude Code loads every folder under `~/.claude/skills/` that has a
`.claude-plugin/plugin.json` as a plugin, enabled by default, with the id
`<manifest name>@skills-dir`: here `busy-claude@skills-dir`. Start a new Claude
Code session (or run `/reload-plugins` in an open one), then confirm:

```sh
claude plugin list          # busy-claude@skills-dir should be listed as enabled
```

and run `/hooks` inside Claude Code to see the plugin's hooks. If it is missing,
open `/plugin` and check the **Errors** tab. To uninstall, delete the folder.

To try it for one session without installing it, point Claude Code at the folder:

```sh
claude --plugin-dir /path/to/busy-claude
```

`claude plugin validate .` checks the manifest. It passes, with one warning
about the plugin name containing "claude"; that is cosmetic.

### 2. Connect the BUSY Bar

#### Option A: USB (default, nothing to configure)

1. Plug the BUSY Bar into your Mac with a USB-C cable.
2. The bar appears as a network device and answers at `http://10.0.4.20`. USB is
   treated as a secure channel, so there is no password.
3. Check the link:

   ```sh
   ./scripts/busybarctl check
   ```

   It prints the API version (for example `{"api_semver": "27.5.0"}`). The
   plugin's defaults already point at this address, so you can go straight to
   [step 4](#4-check-that-it-works).

The bar must stay connected. If it is unplugged, status updates are silently
dropped; Claude Code is never blocked or slowed by it.

#### Option B: LAN (Wi-Fi)

Use this when the bar is not plugged into the computer that runs Claude Code,
for example when it sits on a desk and is powered from a wall adapter.

1. **Join Wi-Fi.** Connect the bar to your network on the device
   (Settings → Wi-Fi).
2. **Enable API access, once, over USB.** Wi-Fi API access is off by default.
   Connect the bar by USB, open `http://10.0.4.20` in a browser, go to the
   **Network** tab, find the **HTTP API** section, turn on **HTTP API access**,
   then click **Set password and enable** and choose a password.
3. **Find the bar's IP address.** On the device: Settings → Wi-Fi → your network
   name → View IP Address. Give the bar a fixed address (a DHCP reservation in
   your router) so the setting does not go stale.
4. **Check the link** from the machine that runs Claude Code. Keep the password
   out of your shell history by passing it through the environment:

   ```sh
   export BUSYBAR_AUTH_TOKEN='your-api-password'
   ./scripts/busybarctl --url 192.168.1.20 --auth wifi check
   ```

   `--url` accepts a bare IP, a host name, or a full `http://IP/api` URL. A
   JSON version reply means it works; a timeout means the bar is not reachable
   (check the IP, and that both devices are on the same network); HTTP 403 means
   a wrong password.

The password is sent in the clear as an `X-API-Token` header over plain HTTP, so
only use LAN access on a network you trust. (A third mode, `cloud`, goes through
the BUSY cloud with an account API token; see the table at the top.)

### 3. Point the plugin at the bar (LAN only)

USB works with the defaults. For LAN, set the plugin's options in Claude Code:
run `/plugin`, open the **Installed** tab, select `busy-claude`, and choose
**Configure options**.

| Option | USB (default) | LAN |
| --- | --- | --- |
| Device URL | `http://10.0.4.20/api` | `http://192.168.1.20/api` (your bar's IP) |
| Authentication mode | `usb` | `wifi` |
| Authentication token | empty | the API password from step 2 |

The token is declared sensitive, so Claude Code masks it and keeps it in the
platform's secure credential store; it is never written to the plugin's state
file or committed anywhere. Closing the panel applies the change; if it does
not take effect, run `/reload-plugins`.

The same dialog has everything else you can change: enable or disable updates,
the request and stale-session timeouts, the label and `#RRGGBBAA` color of each
of the seven states, the subtitle color (default white), the mascot, the
working comet, the waiting dots, the permission-mode badge, and debug logging. Non-sensitive options also show up as rows in
`/config`. Debug logs go to `debug.log` under `CLAUDE_PLUGIN_DATA`; hooks never
print connection failures into the conversation.

Outside Claude Code (for the `busybarctl` helper, or as a fallback), the same
options are read from `BUSYBAR_DEVICE_URL`, `BUSYBAR_AUTH_MODE`,
`BUSYBAR_AUTH_TOKEN`, `BUSYBAR_MASCOT`, and so on: the option name, upper case,
with a `BUSYBAR_` prefix.

### 4. Check that it works

1. Run the demo. It walks through all seven states, five seconds each, and the
   first run uploads the animations to the bar:

   ```sh
   ./scripts/busybarctl demo
   ```

2. Start a new Claude Code session. The bar shows **READY** with the `>_`
   mascot. Send a prompt: it switches to **WORKING** with the typing mascot, the
   current tool as a subtitle, and the comet, as in the picture above.
3. Nothing happens? Turn on the **Debug logging** option, reproduce it, and read
   `debug.log`. The [troubleshooting](#troubleshooting) section covers the usual
   causes (unreachable bar, wrong password, a magenta box, missing animation).

## Test the real connection

The helper accepts a full URL or just a host/IP. Options must precede the
subcommand.

USB:

```sh
./scripts/busybarctl --url 10.0.4.20 --auth usb check
./scripts/busybarctl --url 10.0.4.20 --auth usb demo
```

Wi-Fi:

```sh
./scripts/busybarctl \
  --url http://192.168.1.20/api \
  --auth wifi \
  --token 'YOUR_API_PASSWORD' \
  check
```

Cloud:

```sh
./scripts/busybarctl \
  --url https://api.busy.app/busybar \
  --auth cloud \
  --token 'YOUR_BUSY_API_TOKEN' \
  check
```

Send one state, optionally overriding its appearance:

```sh
./scripts/busybarctl --url 10.0.4.20 send WORKING
./scripts/busybarctl --url 10.0.4.20 send WAITING_FOR_INPUT \
  --label 'YOUR TURN' --color '#FF8000FF'
./scripts/busybarctl --url 10.0.4.20 send WORKING --activity Editing
./scripts/busybarctl --url 10.0.4.20 assets   # install the mascot and comet animations if missing
```

`demo` sends `WORKING` (with subtitle), `WAITING_FOR_INPUT`, `LIMIT`, `FINISHED`,
`ERROR`, `READY`, and `IDLE`, holding each for five seconds so the mascot can be seen.
The final state is neutral gray `IDLE`.

## Automated tests

```sh
python3 -m unittest discover -s tests -v
```

The suite uses a local mock HTTP server and covers lifecycle transitions,
interactive tools, non-terminal tool failures, API failures, disconnected
devices, stale cleanup, authentication headers, redraw deduplication, and two
simultaneous sessions.

## Real-device validation procedure

1. Update BUSY Bar firmware and connect it over USB.
2. Run `./scripts/busybarctl --url 10.0.4.20 check`; confirm it prints the API
   version response.
3. Run `demo`; verify the seven labels, colors and mascot animations on the front panel.
4. Start `claude --plugin-dir "$PWD"` and submit a prompt. Verify `WORKING`.
5. Ask Claude to perform several tool calls. Verify the display remains blue.
6. Trigger a permission request or `AskUserQuestion`. Verify yellow/orange
   `WAITING`.
7. Respond and let the tool finish. Verify `WORKING`, then green `FINISHED` at
   the end of the response.
8. Exit Claude Code. Verify neutral `IDLE`.
9. Disconnect the BUSY Bar and submit another prompt. Claude Code must continue
   normally. Reconnect it and trigger another lifecycle event.
10. Open two Claude Code sessions. Put one in a waiting state while the other is
    working; verify `WORKING` wins. Finish/end the working session and verify the
    waiting session becomes visible.

The automated behavior has been validated against a mock of the documented HTTP
contract. Physical rendering, device firmware compatibility, and LAN/cloud
credentials require the real-device checks above.

## Troubleshooting

**Connection refused or timed out**

- USB: confirm the device web UI opens at `http://10.0.4.20`.
- Wi-Fi: confirm HTTP API access is enabled and use the IP shown under
  Settings → Wi-Fi → your access point → View IP Address.
- Run `check` with the same URL/authentication values as the plugin.

**A magenta box appears where the mascot or comet should be**

The animation element points at an asset the device does not have (for example
after a factory reset). Run `busybarctl assets`; every state change also
re-checks and re-uploads what it needs.

**The bar shows text but no mascot or comet**

Enable debug logging and look for `animation asset failed`. Common causes are a
USB/Wi-Fi hiccup during the upload (retried after a minute) or
`MASCOT=false` / `WORKING_ANIMATION=false`.

**HTTP 403**

- Wi-Fi requires the configured API password in `X-API-Token`.
- Cloud requires a BUSY account token with BUSY Bar scope.
- Confirm `auth_mode` matches the connection type.

**Plugin loads but hooks do not run**

- Run `claude plugin validate --strict PATH_TO_PLUGIN`.
- Inspect `/plugin` → Errors and `/hooks` inside Claude Code.
- Run Claude Code with `--debug` and trigger a hook event.
- Confirm `python3 --version` is 3.9 or newer.

**The display does not immediately leave WAITING after permission approval**

This is the documented hook gap described under Detection limits. It changes on
the next post-tool event; the plugin does not use timers to guess that approval
occurred.

**The bar stays on WORKING after I interrupt with Esc**

Claude Code fires no hook when you interrupt a turn (`Stop` does not run for a
user interrupt), so nothing tells the plugin the turn ended. The bar corrects
itself on the next prompt or when the session goes stale.

**The bar remains stale after a crash**

Reduce the stale-session timeout option. The default is one hour to avoid expiring a
legitimate long-running tool. A clean `SessionEnd` updates immediately.

## References

- [BUSY Bar developer documentation](https://docs.busy.app/bar/dev)
- [BUSY Bar HTTP API guide](https://docs.busy.app/bar/dev/http-api)
- [Official busylib connection guide](https://busy-app.github.io/busylib-py/guides/connecting/)
- [Official busylib display guide](https://busy-app.github.io/busylib-py/guides/displays/)
- [Claude Code plugin documentation](https://code.claude.com/docs/en/plugins)
- [Claude Code hook reference](https://code.claude.com/docs/en/hooks)
