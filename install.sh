#!/bin/sh
# Installs the busy-claude plugin (BUSY Bar status display) into Claude Code.
#
#   curl -fsSL https://raw.githubusercontent.com/nicoSWD/busybar-claude/main/install.sh | sh
#
# Options (pass them after `sh -s --` when piping):
#   --yes, -y          do not ask anything, use the defaults (USB bar, no statusline)
#   --dry-run          print what would run, change nothing
#   --statusline       also set up the optional statusline (plan usage on the READY screen)
#   --uninstall        remove the plugin, the marketplace and the statusline wrapper
#   --help, -h         show this text
#
# It runs only Claude Code's own commands (`claude plugin ...`). The password for a
# Wi-Fi bar is handed to Claude Code through a pipe, never on a command line.
set -eu

REPO="${BUSYBAR_REPO:-nicoSWD/busybar-claude}"
MARKETPLACE="busybar-claude"
PLUGIN="busy-claude"
ID="$PLUGIN@$MARKETPLACE"

usage() {
  cat <<'EOF'
Installs the busy-claude plugin (BUSY Bar status display) into Claude Code.

  curl -fsSL https://raw.githubusercontent.com/nicoSWD/busybar-claude/main/install.sh | sh

Options (pass them after `sh -s --` when piping):
  --yes, -y       do not ask anything, use the defaults (USB bar, no statusline)
  --dry-run       print what would run, change nothing
  --statusline    also set up the optional statusline (plan usage on the READY screen)
  --uninstall     remove the plugin, the marketplace and the statusline wrapper
  --help, -h      show this text

It runs only Claude Code's own commands (`claude plugin ...`). The password for a
Wi-Fi bar is handed to Claude Code through a pipe, never on a command line.
EOF
}

YES=0
DRY=0
UNINSTALL=0
STATUSLINE=ask

for arg in "$@"; do
  case "$arg" in
    --yes|-y) YES=1 ;;
    --dry-run) DRY=1 ;;
    --statusline) STATUSLINE=yes ;;
    --uninstall) UNINSTALL=1 ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown option: %s (try --help)\n' "$arg" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Questions are read from the terminal, so they work under `curl | sh` too.
# Without a terminal (or with --yes) the default answer is used.
interactive() { [ "$YES" -eq 0 ] && [ "$DRY" -eq 0 ] && [ -t 1 ] && { : </dev/tty; } 2>/dev/null; }

ask() { # ask "question" y|n  -> status 0 for yes
  if ! interactive; then [ "$2" = y ]; return; fi
  if [ "$2" = y ]; then hint="Y/n"; else hint="y/N"; fi
  printf '%s [%s] ' "$1" "$hint" >/dev/tty
  read -r reply </dev/tty || reply=""
  case "$reply" in
    [Yy]*) return 0 ;;
    [Nn]*) return 1 ;;
    *) [ "$2" = y ] ;;
  esac
}

run() { printf '+ %s\n' "$*"; if [ "$DRY" -eq 0 ]; then "$@"; fi; }

# Runs the plugin's own script from the folder Claude Code installed it to.
plugin_script() {
  python3 - "$ID" <<'PY'
import json, os, sys
base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
try:
    plugins = json.load(open(os.path.join(base, "plugins", "installed_plugins.json"))).get("plugins", {})
    print(os.path.join(plugins[sys.argv[1]][0]["installPath"], "scripts", "busybar_status.py"))
except Exception:
    sys.exit(1)
PY
}

command -v claude >/dev/null 2>&1 || die "Claude Code was not found. Install it first: https://claude.com/claude-code"

if [ "$UNINSTALL" -eq 1 ]; then
  step "Removing busy-claude"
  if [ "$DRY" -eq 0 ] && script=$(plugin_script 2>/dev/null) && [ -f "$script" ]; then
    python3 "$script" statusline-uninstall || true
  fi
  run claude plugin uninstall "$ID" || true
  run claude plugin marketplace remove "$MARKETPLACE" || true
  say "Done."
  exit 0
fi

step "Checking requirements"
command -v python3 >/dev/null 2>&1 || die "python3 was not found. The plugin needs Python 3.9 or newer."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "Python 3.9 or newer is required (found $(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])'))."
say "claude: $(command -v claude)"
say "python3: $(python3 --version 2>&1)"
if [ -f "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/busy-claude/.claude-plugin/plugin.json" ]; then
  say "warning: ~/.claude/skills/busy-claude also exists. Remove it, or every hook will run twice."
fi

step "Adding the marketplace ($REPO)"
if [ "$DRY" -eq 1 ]; then
  run claude plugin marketplace add "$REPO"
elif out=$(claude plugin marketplace add "$REPO" 2>&1); then
  say "$out"
elif claude plugin marketplace list 2>/dev/null | grep -q "$MARKETPLACE"; then
  say "Already added, refreshing it."
  run claude plugin marketplace update "$MARKETPLACE"
else
  printf '%s\n' "$out" >&2
  die "could not add the marketplace $REPO"
fi

step "Installing $ID"
if [ "$DRY" -eq 1 ]; then
  run claude plugin install "$ID" --scope user
elif out=$(claude plugin install "$ID" --scope user 2>&1); then
  say "$out"
  say "(\"options not yet set\" is fine: the defaults suit a bar on USB.)"
elif claude plugin list 2>/dev/null | grep -q "$ID"; then
  say "Already installed, updating it."
  run claude plugin update "$ID"
else
  printf '%s\n' "$out" >&2
  die "could not install $ID"
fi

if ! ask "Is the BUSY Bar connected with a USB cable?" y; then
  step "Wi-Fi setup"
  say "Enable the Wi-Fi API once over USB first (README: Connect the BUSY Bar)."
  printf 'Address of the bar (for example 192.168.1.20): ' >/dev/tty
  read -r address </dev/tty
  printf 'API password (hidden): ' >/dev/tty
  stty -echo </dev/tty 2>/dev/null || true
  trap 'stty echo </dev/tty 2>/dev/null || true' EXIT INT TERM
  read -r password </dev/tty || password=""
  stty echo </dev/tty 2>/dev/null || true
  printf '\n' >/dev/tty
  printf '%s\n%s\n' "$address" "$password" | python3 -c '
import json, sys
address = sys.stdin.readline().strip()
password = sys.stdin.readline().rstrip("\n")
if "://" not in address:
    address = "http://" + address
address = address.rstrip("/")
if not address.endswith("/api"):
    address += "/api"
values = {"DEVICE_URL": address, "AUTH_MODE": "wifi"}
if password:
    values["AUTH_TOKEN"] = password
print(json.dumps(values))
' | claude plugin configure "$ID" --values-stdin || say "Could not save the settings. In Claude Code, run: /plugin configure $ID"
fi

if [ "$STATUSLINE" = ask ]; then
  if ask "Show your plan usage on the bar's READY screen (sets up the optional statusline)?" n; then
    STATUSLINE=yes
  else
    STATUSLINE=no
  fi
fi
if [ "$STATUSLINE" = yes ]; then
  step "Setting up the statusline"
  if [ "$DRY" -eq 1 ]; then
    say "+ python3 <installed plugin>/scripts/busybar_status.py statusline-install"
  elif script=$(plugin_script) && [ -f "$script" ]; then
    python3 "$script" statusline-install
  else
    say "Could not find the installed plugin; run /busy-claude:setup in Claude Code instead."
  fi
fi

step "Done"
say "Plug the bar in, then start a new Claude Code session (or run /reload-plugins in an open one)."
say "Run /busy-claude:setup there to check that everything is connected."
