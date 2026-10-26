#!/usr/bin/env python3
"""Claude Code lifecycle hook and BUSY Bar HTTP client.

The hook path only updates local shared state and launches detached I/O. All
errors are contained so a BUSY Bar problem can never block Claude Code.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

import busybar_anim
import busybar_font
import busybar_mascot
import busybar_sound


STATUSES = ("WORKING", "WAITING_FOR_INPUT", "LIMIT", "FINISHED", "ERROR", "READY", "IDLE")
STATE_PRIORITY = {
    "WORKING": 50,
    "WAITING_FOR_INPUT": 40,
    "LIMIT": 35,
    "ERROR": 30,
    "FINISHED": 20,
    "READY": 10,
    "IDLE": 0,
}
INTERACTIVE_TOOLS = {"AskUserQuestion", "ExitPlanMode"}
NOTIFICATION_STATES = {
    "permission_prompt": "WAITING_FOR_INPUT",
    "idle_prompt": "WAITING_FOR_INPUT",
    "elicitation_dialog": "WAITING_FOR_INPUT",
    "elicitation_url_dialog": "WAITING_FOR_INPUT",
    "agent_needs_input": "WAITING_FOR_INPUT",
    "quota_auto_resume_stale": "WAITING_FOR_INPUT",
    "quota_auto_resume_disabled": "WAITING_FOR_INPUT",
    "elicitation_complete": "WORKING",
    "elicitation_response": "WORKING",
    "quota_auto_resume_fired": "WORKING",
}
DEFAULT_LABELS = {
    "WORKING": "WORKING",
    "WAITING_FOR_INPUT": "WAITING",
    "LIMIT": "LIMIT HIT",
    "FINISHED": "FINISHED",
    "ERROR": "ERROR",
    "READY": "READY",
    "IDLE": "IDLE",
}
DEFAULT_COLORS = {
    "WORKING": "#0080FFFF",
    "WAITING_FOR_INPUT": "#FFB000FF",
    "LIMIT": "#A060FFFF",
    "FINISHED": "#00FF66FF",
    "ERROR": "#FF2020FF",
    "READY": "#00C8D0FF",
    "IDLE": "#7D8CB0FF",
}
TOOL_ACTIVITY = {
    "Read": "Reading",
    "Edit": "Editing",
    "Write": "Editing",
    "NotebookEdit": "Editing",
    "Bash": "Running",
    "Monitor": "Running",
    "Grep": "Searching",
    "Glob": "Searching",
    "LSP": "Searching",
    "WebFetch": "Browsing",
    "WebSearch": "Browsing",
    "Agent": "Subagent",
    "Task": "Subagent",
}
THINKING_EVENTS = {"UserPromptSubmit", "PermissionDenied", "ElicitationResult", "PostCompact"}
RATE_LIMIT_ERRORS = {"rate_limit"}
# States whose subtitle says more: what Claude is doing, what it waits for, which limit it hit, what
# failed, how long the turn took.
SUBTITLE_STATES = ("WORKING", "WAITING_FOR_INPUT", "LIMIT", "ERROR", "FINISHED")
SUBTITLE_MAX_WIDTH = 40  # px; wider text no longer centers on the display beside the mascot
INTERACTIVE_SUBTITLES = {"AskUserQuestion": "Question", "ExitPlanMode": "Plan ready"}
NOTIFICATION_SUBTITLES = {
    "permission_prompt": "Approve?",
    "idle_prompt": "Your turn",
    "elicitation_dialog": "MCP input",
    "elicitation_url_dialog": "MCP input",
    "agent_needs_input": "Needs you",
    "quota_auto_resume_stale": "Press Enter",
    "quota_auto_resume_disabled": "Your turn",
}
# StopFailure `error` -> subtitle candidates, first one that fits SUBTITLE_MAX_WIDTH wins.
ERROR_SUBTITLES = {
    "overloaded": ["Overloaded"],
    "authentication_failed": ["Auth failed"],
    "oauth_org_not_allowed": ["Org blocked"],
    "account_on_hold": ["On hold"],
    "billing_error": ["Billing error"],
    "invalid_request": ["Bad input"],
    "model_not_found": ["Bad model"],
    "server_error": ["Server error", "Server err"],
    "max_output_tokens": ["Max tokens"],
    "cloud_credential_error": ["Cloud creds"],
}
RESET_CLOCK = re.compile(r"reset[s]?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m", re.IGNORECASE)
RESET_IN = re.compile(
    r"\bin\s+(?:(\d+)\s*(?:h|hr|hrs|hours?))?\s*(?:(\d+)\s*(?:m|mins?|minutes?))?\b", re.IGNORECASE
)
# Events after which the device may no longer show what we last drew (new turn or session).
REDRAW_EVENTS = {"SessionStart", "UserPromptSubmit"}

# Front display is 72x16. Rows, measured on the device: `small` caps are 5px tall
# and sit 2px below y, `tiny` caps are 4px, the animation strip is 2px.
#
#   x 0-15   mascot    (16x16 animation, one per state)
#   text     centered on the whole display when it clears the mascot (up to 40px wide),
#            else centered in the 56px column x 16-71, else scrolling inside that column
#     rows 1-5    title      (small, state color); rows 6-10 when there is no subtitle
#     rows 8-11   subtitle   (tiny, white; descenders reach row 12)
#     rows 14-15  comet      (WORKING only, sweeps the column x 16-71)
#
# Without the mascot the text column is the whole display.
TITLE_ID = "claude-status"
TITLE_FONT = "small"
TITLE_Y = -1
TITLE_CENTER_Y = 4  # title alone (no subtitle): caps on rows 6-10, vertically centered
SUBTITLE_ID = "claude-activity"
SUBTITLE_FONT = "tiny"
SUBTITLE_Y = 7
SUBTITLE_COLOR = "#FFFFFFFF"
ANIMATION_ID = "claude-anim"
ANIMATION_Y = 14
COMET_WIDTH = 32  # the rail under the text (comet while working, ripple while waiting): as wide as "WORKING", leaves the corner for the badge
COMET_X = (72 - COMET_WIDTH) // 2
MODE_ID = "claude-mode"
BADGE_Y = 11  # `tiny` caps sit one row below y: rows 12-15, flush with the bottom edge
# permission_mode -> (word, text color). Plain text in the bottom-right corner. Words are at most
# 17px wide so they start at x>=55: right of the comet (x 20-51) and of a subtitle's last letter
# (the longest, "Compacting", ends at x=54).
MODE_BADGES = {
    "plan": ("PLAN", "#20C9A6FF"),
    "acceptEdits": ("EDIT", "#4A8CFFFF"),
    "auto": ("AUTO", "#3DDC84FF"),
    "dontAsk": ("LOCK", "#C0C4D0FF"),
    "bypassPermissions": ("RISK", "#FF3030FF"),
}
BANG_ID = "claude-bang"
FIREWORKS_ID = "claude-fireworks"
SKY_X = 56  # the free area right of the text (x 56-71, rows 0-11): fireworks, or the night sky
FIREWORKS_X = SKY_X
NIGHT_ID = "claude-night"
IDLE_SUBTITLE_DIM = 0.5
DEFAULT_SUBTITLES = {"IDLE": "No session"}  # states with no session to take a subtitle from
DOTS_ID = "claude-dots"
SUFFIX_GAP = 1  # pixels between the last letter and an animated suffix, as between letters
MASCOT_ID = "claude-mascot"
MASCOT_WIDTH = busybar_mascot.SIZE
DISPLAY_WIDTH = 72
MANAGED_IDS = (TITLE_ID, SUBTITLE_ID, MASCOT_ID, ANIMATION_ID, DOTS_ID, BANG_ID, FIREWORKS_ID, NIGHT_ID, MODE_ID)
LAYOUT_VERSION = 12  # bump to force a redraw after changing the layout above
SOUND_ID = "sound"  # key for the sound asset in an asset plan (not a display element)
SOUND_STATES = {"WAITING_FOR_INPUT": "waiting", "FINISHED": "finished"}  # state -> sound kind
SOUND_COOLDOWN = 3.0  # seconds between sounds
DEMO_SUBTITLES = {
    "WORKING": "Editing",
    "WAITING_FOR_INPUT": "Allow Bash?",
    "LIMIT": "Resets 3pm",
    "FINISHED": "Done in 42s",
    "ERROR": "Overloaded",
}
ASSET_RETRY_SECONDS = 60.0
ASSET_UPLOAD_TIMEOUT = 15.0


def _env(name: str, fallback: str = "") -> str:
    return os.environ.get("CLAUDE_PLUGIN_OPTION_" + name, os.environ.get("BUSYBAR_" + name, fallback))


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


def normalize_base_url(value: str) -> str:
    value = (value or "http://10.0.4.20/api").strip().rstrip("/")
    if "://" not in value:
        value = "http://" + value
    if not value.endswith("/api") and not value.endswith("/busybar"):
        value += "/api"
    return value


def valid_color(value: str, default: str) -> str:
    value = value.strip().upper()
    return value if re.fullmatch(r"#[0-9A-F]{8}", value) else default


def scale_color(value: str, factor: float) -> str:
    """`#RRGGBBAA` with its color channels scaled by `factor` (alpha kept)."""
    channels = [int(int(value[i:i + 2], 16) * factor) for i in (1, 3, 5)]
    return "#%02X%02X%02X%s" % (channels[0], channels[1], channels[2], value[7:9])


def safe_label(value: str, default: str) -> str:
    # BUSY Bar fonts do not render arbitrary Unicode/control characters.
    clean = "".join(char for char in value if char == " " or 33 <= ord(char) <= 126)
    return clean.strip()[:64] or default


@dataclasses.dataclass(frozen=True)
class Config:
    base_url: str
    auth_mode: str
    token: str
    enabled: bool
    timeout: float
    stale_seconds: float
    debug: bool
    state_dir: Path
    labels: Mapping[str, str]
    colors: Mapping[str, str]
    application_name: str = "claude-code-status"
    animation: bool = True
    mascot: bool = True
    waiting_dots: bool = True
    mode_badge: bool = True
    sounds: bool = True
    sound_min_seconds: float = 20.0
    subtitle_color: str = SUBTITLE_COLOR

    @property
    def column_x(self) -> int:
        """Left edge of the column the text and comet use."""
        return MASCOT_WIDTH if self.mascot else 0

    @property
    def column_width(self) -> int:
        return DISPLAY_WIDTH - self.column_x

    @classmethod
    def from_environment(cls) -> "Config":
        auth_mode = _env("AUTH_MODE", "usb").strip().lower()
        if auth_mode not in {"usb", "wifi", "cloud"}:
            auth_mode = "usb"
        configured_data = os.environ.get("BUSYBAR_STATE_DIR") or os.environ.get("CLAUDE_PLUGIN_DATA")
        if configured_data:
            state_dir = Path(configured_data).expanduser()
        else:
            state_dir = Path.home() / "Library" / "Caches" / "claude-busy-bar"
        labels = {
            status: safe_label(_env(status.replace("WAITING_FOR_INPUT", "WAITING") + "_LABEL", default), default)
            for status, default in DEFAULT_LABELS.items()
        }
        colors = {
            status: valid_color(_env(status.replace("WAITING_FOR_INPUT", "WAITING") + "_COLOR", default), default)
            for status, default in DEFAULT_COLORS.items()
        }
        return cls(
            base_url=normalize_base_url(_env("DEVICE_URL", "http://10.0.4.20/api")),
            auth_mode=auth_mode,
            token=_env("AUTH_TOKEN", ""),
            enabled=_as_bool(_env("STATUS_UPDATES_ENABLED", "true"), True),
            timeout=_as_float(_env("REQUEST_TIMEOUT", "1.5"), 1.5, 0.1, 30.0),
            stale_seconds=_as_float(_env("STALE_SESSION_SECONDS", "3600"), 3600.0, 1.0, 86400.0),
            debug=_as_bool(_env("DEBUG_LOGGING", "false"), False),
            state_dir=state_dir,
            labels=labels,
            colors=colors,
            animation=_as_bool(_env("WORKING_ANIMATION", "true"), True),
            mascot=_as_bool(_env("MASCOT", "true"), True),
            waiting_dots=_as_bool(_env("WAITING_DOTS", "true"), True),
            mode_badge=_as_bool(_env("MODE_BADGE", "true"), True),
            sounds=_as_bool(_env("SOUNDS", "true"), True),
            sound_min_seconds=_as_float(_env("SOUND_MIN_SECONDS", "20"), 20.0, 0.0, 3600.0),
            subtitle_color=valid_color(_env("SUBTITLE_COLOR", SUBTITLE_COLOR), SUBTITLE_COLOR),
        )


class Store:
    def __init__(self, config: Config):
        self.config = config
        self.directory = config.state_dir
        self.path = self.directory / "state.json"
        self.lock_path = self.directory / "state.lock"
        self.dispatch_lock_path = self.directory / "dispatch.lock"
        self.pid_path = self.directory / "reaper.pid"

    def ensure(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextlib.contextmanager
    def lock(self, dispatch: bool = False) -> Iterator[None]:
        self.ensure()
        path = self.dispatch_lock_path if dispatch else self.lock_path
        with path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield

    def load_unlocked(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("sessions", {}), dict):
                data.setdefault("sessions", {})
                data.setdefault("last_sent", None)
                return data
        except (OSError, ValueError, TypeError):
            pass
        return {"version": 1, "sessions": {}, "last_sent": None}

    def save_unlocked(self, data: Mapping[str, Any]) -> None:
        self.ensure()
        temporary = self.path.with_suffix(".tmp.%d" % os.getpid())
        temporary.write_text(json.dumps(data, separators=(",", ":"), sort_keys=True), encoding="utf-8")
        os.chmod(str(temporary), 0o600)
        os.replace(str(temporary), str(self.path))

    def prune_unlocked(self, data: Dict[str, Any], now: float) -> bool:
        sessions = data.setdefault("sessions", {})
        expired = [
            session_id
            for session_id, item in sessions.items()
            if now - float(item.get("updated_at", 0)) > self.config.stale_seconds
        ]
        for session_id in expired:
            sessions.pop(session_id, None)
        return bool(expired)


def log(config: Config, message: str) -> None:
    if not config.debug:
        return
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (config.state_dir / "debug.log").open("a", encoding="utf-8") as handle:
            handle.write("%s pid=%d %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), os.getpid(), message))
    except OSError:
        pass


def event_state(payload: Mapping[str, Any]) -> Optional[str]:
    event = payload.get("hook_event_name")
    if event == "SessionEnd":
        return None
    if event == "StopFailure":
        # A usage/rate limit is not a failure: Claude may resume on its own.
        return "LIMIT" if payload.get("error") in RATE_LIMIT_ERRORS else "ERROR"
    if event == "Stop":
        return "FINISHED"
    if event == "SessionStart":
        # A session that just opened (or was cleared/resumed) is waiting for a prompt, not
        # working. After compaction the turn may still be running; PreCompact/PostCompact
        # decide that.
        return None if payload.get("source") == "compact" else "READY"
    if event in {"PermissionRequest", "Elicitation"}:
        return "WAITING_FOR_INPUT"
    if event == "Notification":
        return NOTIFICATION_STATES.get(str(payload.get("notification_type", "")))
    if event == "PreToolUse" and payload.get("tool_name") in INTERACTIVE_TOOLS:
        return "WAITING_FOR_INPUT"
    if event in {
        "UserPromptSubmit",
        "PreCompact",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "PermissionDenied",
        "ElicitationResult",
    }:
        return "WORKING"
    return None


def event_activity(payload: Mapping[str, Any]) -> Optional[str]:
    """Subtitle for a WORKING event, or None when the event keeps the previous one."""
    event = payload.get("hook_event_name")
    if event == "PreToolUse":
        tool = str(payload.get("tool_name", ""))
        if tool.startswith("mcp__"):
            return "Tool"
        return TOOL_ACTIVITY.get(tool, "Working")
    if event == "PreCompact":
        return "Compacting"
    if event in THINKING_EVENTS:
        return "Thinking"
    return None


def _fits(text: str) -> bool:
    return busybar_font.text_width(text, SUBTITLE_FONT) <= SUBTITLE_MAX_WIDTH


def permission_subtitle(tool: str) -> str:
    if tool in INTERACTIVE_SUBTITLES:
        return INTERACTIVE_SUBTITLES[tool]
    if tool and not tool.startswith("mcp__"):
        text = "Allow %s?" % tool
        if _fits(safe_label(text, "")):
            return text
    return "Allow tool?" if tool.startswith("mcp__") else "Approve?"


def waiting_subtitle(payload: Mapping[str, Any]) -> Optional[str]:
    """What Claude is waiting for, or None when the event says nothing specific."""
    event = payload.get("hook_event_name")
    tool = str(payload.get("tool_name", ""))
    if event == "PermissionRequest":
        return permission_subtitle(tool)
    if event == "PreToolUse" and tool in INTERACTIVE_SUBTITLES:
        return INTERACTIVE_SUBTITLES[tool]
    if event == "Elicitation":
        return "MCP input"
    if event == "Notification":
        return NOTIFICATION_SUBTITLES.get(str(payload.get("notification_type", "")))
    return None


def limit_subtitle(payload: Mapping[str, Any]) -> str:
    """When the limit resets if the error says so ("Resets 3pm", "Back in 2h"), else the plain cause."""
    text = " ".join(str(payload.get(key, "")) for key in ("last_assistant_message", "error_details"))
    clock = RESET_CLOCK.search(text)
    if clock:
        hour, minute, half = int(clock.group(1)), clock.group(2), clock.group(3).lower()
        if 1 <= hour <= 12 and (minute is None or int(minute) < 60):
            when = "%d%s" % (hour, ":" + minute if minute and minute != "00" else "")
            return "Resets %s%sm" % (when, half)
    relative = RESET_IN.search(text)
    if relative and (relative.group(1) or relative.group(2)):
        hours, minutes = relative.group(1), relative.group(2)
        return "Back in " + (hours + "h" if hours else "") + (minutes + "m" if minutes else "")
    return "Rate limit"


def error_subtitle(payload: Mapping[str, Any]) -> str:
    for candidate in ERROR_SUBTITLES.get(str(payload.get("error", "")), ["API error"]):
        if _fits(candidate):
            return candidate
    return "API error"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return "%ds" % seconds
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return "%dm %ds" % (minutes, seconds) if seconds else "%dm" % minutes
    hours, minutes = divmod(minutes, 60)
    return "%dh %dm" % (hours, minutes) if minutes else "%dh" % hours


def finished_subtitle(seconds: float) -> str:
    duration = format_duration(seconds)
    return "Done in " + duration if _fits("Done in " + duration) else duration


def aggregate_state(sessions: Mapping[str, Mapping[str, Any]]) -> str:
    if not sessions:
        return "IDLE"
    return max((str(item.get("state", "IDLE")) for item in sessions.values()), key=lambda state: STATE_PRIORITY.get(state, -1))


def aggregate_activity(sessions: Mapping[str, Mapping[str, Any]], state: str) -> str:
    if state not in SUBTITLE_STATES:
        return ""
    candidates = [item for item in sessions.values() if item.get("state") == state]
    if not candidates:
        return ""
    latest = max(candidates, key=lambda item: float(item.get("updated_at", 0)))
    return str(latest.get("activity", ""))


def aggregate_mode(sessions: Mapping[str, Mapping[str, Any]], state: str) -> str:
    """Permission mode of the session that decides what the bar shows ("" for default or unknown)."""
    candidates = [item for item in sessions.values() if item.get("state") == state]
    if not candidates:
        return ""
    latest = max(candidates, key=lambda item: float(item.get("updated_at", 0)))
    return str(latest.get("mode", ""))


def aggregate_turn_seconds(sessions: Mapping[str, Mapping[str, Any]], state: str) -> Optional[float]:
    """How long the finished turn took, for the session that decides what the bar shows."""
    candidates = [item for item in sessions.values() if item.get("state") == state]
    if not candidates:
        return None
    latest = max(candidates, key=lambda item: float(item.get("updated_at", 0)))
    value = latest.get("turn_seconds")
    return float(value) if value is not None else None


def apply_event(store: Store, payload: Mapping[str, Any], now: Optional[float] = None) -> Tuple[str, bool]:
    now = time.time() if now is None else now
    event = str(payload.get("hook_event_name", ""))
    session_id = str(payload.get("session_id") or payload.get("transcript_path") or "unknown-session")
    with store.lock():
        data = store.load_unlocked()
        store.prune_unlocked(data, now)
        previous = aggregate_state(data["sessions"])
        previous_activity = aggregate_activity(data["sessions"], previous)
        previous_mode = aggregate_mode(data["sessions"], previous)
        if event == "SessionEnd":
            data["sessions"].pop(session_id, None)
        else:
            old = data["sessions"].get(session_id, {})
            if event == "PostCompact":
                # Return to whatever PreCompact interrupted: mid-turn compaction keeps working,
                # a manual /compact or idle compaction goes back to being idle.
                state = old.get("resume_state") or ("READY" if payload.get("trigger") == "manual" else "WORKING")
            else:
                state = event_state(payload)
            if state is None:
                needs_dispatch = data.get("last_sent") != render_fingerprint(
                    store.config, previous, previous_activity, previous_mode
                )
                return previous, needs_dispatch
            record = {"state": state, "updated_at": now, "event": event}
            mode = payload.get("permission_mode")
            if isinstance(mode, str):  # not every event carries it; otherwise keep what we knew
                record["mode"] = "" if mode == "default" else mode
            elif old.get("mode"):
                record["mode"] = old["mode"]
            turn_started = old.get("turn_started")
            if event == "UserPromptSubmit" and old.get("state") not in ("WORKING", "WAITING_FOR_INPUT", "LIMIT"):
                turn_started = now  # a new turn (a prompt while one is running just continues it)
            if turn_started is not None:
                record["turn_started"] = turn_started
            if event == "PreCompact":
                carried = old if old.get("event") == "PreCompact" else {"resume_state": old.get("state"), "resume_activity": old.get("activity")}
                if carried.get("resume_state"):
                    record["resume_state"] = carried["resume_state"]
                if carried.get("resume_activity"):
                    record["resume_activity"] = carried["resume_activity"]
            if state == "WORKING":
                activity = event_activity(payload)
                if activity is None:
                    activity = str(old.get("activity", "")) if old.get("state") == "WORKING" else ""
                record["activity"] = activity or "Thinking"
            elif state in ("WAITING_FOR_INPUT", "LIMIT", "ERROR", "FINISHED"):
                if state == "LIMIT":
                    detail: Optional[str] = limit_subtitle(payload)
                elif state == "ERROR":
                    detail = error_subtitle(payload)
                elif state == "FINISHED":
                    if event == "PostCompact":
                        detail = old.get("resume_activity")
                    else:
                        detail = finished_subtitle(now - turn_started) if turn_started is not None else None
                        if turn_started is not None:
                            record["turn_seconds"] = now - turn_started
                else:
                    detail = waiting_subtitle(payload)
                if event == "Notification" and old.get("state") == state and old.get("activity"):
                    detail = str(old["activity"])  # a permission prompt right after PermissionRequest: keep the specific one
                if detail:
                    record["activity"] = detail
            data["sessions"][session_id] = record
            if event in REDRAW_EVENTS:
                data["drawn"] = None
        current = aggregate_state(data["sessions"])
        current_activity = aggregate_activity(data["sessions"], current)
        current_mode = aggregate_mode(data["sessions"], current)
        needs_dispatch = (
            current != previous
            or current_activity != previous_activity
            or current_mode != previous_mode
            or data.get("last_sent") != render_fingerprint(store.config, current, current_activity, current_mode)
        )
        store.save_unlocked(data)
        return current, needs_dispatch


class BusyBarClient:
    def __init__(self, config: Config):
        self.config = config

    def headers(self, content_type: Optional[str] = None) -> Dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "claude-busy-bar/1.0"}
        if content_type:
            headers["Content-Type"] = content_type
        if self.config.auth_mode == "wifi" and self.config.token:
            headers["X-API-Token"] = self.config.token
        elif self.config.auth_mode == "cloud" and self.config.token:
            headers["Authorization"] = "Bearer " + self.config.token
        return headers

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        content_type: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> bytes:
        url = self.config.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, data=body, headers=self.headers(content_type), method=method)
        with urllib.request.urlopen(request, timeout=timeout or self.config.timeout) as response:
            return response.read()

    def elements(
        self,
        status: str,
        label: Optional[str] = None,
        color: Optional[str] = None,
        activity: str = "",
        assets: Optional[Mapping[str, str]] = None,
        mode: str = "",
    ) -> Dict[str, Dict[str, Any]]:
        """The complete set of elements to show for a state, keyed by element id.

        `assets` maps the mascot / comet element ids to the uploaded asset each
        should play; an element without an asset is simply not drawn.
        """
        assets = assets or {}
        activity = activity or DEFAULT_SUBTITLES.get(status, "")
        text = safe_label(label or self.config.labels[status], DEFAULT_LABELS[status])
        rgba = valid_color(color or self.config.colors[status], DEFAULT_COLORS[status])
        config = self.config

        def text_element(element_id: str, content: str, font: str, y: int, element_color: str) -> Dict[str, Any]:
            element = {
                "id": element_id,
                "type": "text",
                "display": "front",
                "text": content,
                "font": font,
                "color": element_color,
                "align": "top_mid",
                "x": DISPLAY_WIDTH // 2,
                "y": y,
                "scroll_rate": 800,
                "scroll_start_delay": 1000,
                "scroll_repeat_delay": 2500,
            }
            if config.mascot:
                estimate = busybar_font.text_width(content, font)
                if estimate > DISPLAY_WIDTH - 2 * MASCOT_WIDTH:
                    # Would reach the mascot if centered on the display: center in the column instead.
                    element["x"] = config.column_x + config.column_width // 2
                if estimate > config.column_width:
                    # Too long even for the column: anchor it at the column's left edge and let it
                    # scroll, rather than centering text that would run over the mascot.
                    element.update(align="top_left", x=config.column_x, width=config.column_width)
            return element

        def animation_element(element_id: str, x: int, y: int) -> Dict[str, Any]:
            return {
                "id": element_id,
                "type": "animation",
                "display": "front",
                "path": assets[element_id],
                "x": x,
                "y": y,
                "loop": True,
            }

        subtitle = safe_label(activity, "")
        title_y = TITLE_Y if subtitle else TITLE_CENTER_Y
        wanted = {TITLE_ID: text_element(TITLE_ID, text, TITLE_FONT, title_y, rgba)}
        if subtitle:
            subtitle_color = config.subtitle_color
            if status == "IDLE":  # the resting screen is calm: a dimmed subtitle
                subtitle_color = scale_color(subtitle_color, IDLE_SUBTITLE_DIM)
            wanted[SUBTITLE_ID] = text_element(SUBTITLE_ID, subtitle, SUBTITLE_FONT, SUBTITLE_Y, subtitle_color)
        for suffix_id, suffix_width, suffix_dy in (
            (DOTS_ID, busybar_anim.DOTS_WIDTH, 6),  # dots sit on the baseline
            (BANG_ID, busybar_anim.BANG_WIDTH, 2),  # the "!!" spans the caps
        ):
            if suffix_id not in assets:
                continue
            # Lay the label and its animated suffix out as one unit centered like a plain title.
            label_width = busybar_font.text_width(text, TITLE_FONT)
            unit = label_width + SUFFIX_GAP + suffix_width
            if not config.mascot or unit <= DISPLAY_WIDTH - 2 * MASCOT_WIDTH:
                center = DISPLAY_WIDTH // 2
            else:
                center = config.column_x + config.column_width // 2
            left = center - unit // 2
            if left >= config.column_x:  # otherwise it cannot fit: show the plain label, no suffix
                wanted[TITLE_ID].update(align="top_left", x=left)
                wanted[TITLE_ID].pop("width", None)
                wanted[suffix_id] = animation_element(suffix_id, left + label_width + SUFFIX_GAP, title_y + suffix_dy)
        if MASCOT_ID in assets:
            wanted[MASCOT_ID] = animation_element(MASCOT_ID, 0, 0)
        if ANIMATION_ID in assets:
            wanted[ANIMATION_ID] = animation_element(ANIMATION_ID, COMET_X, ANIMATION_Y)
        if FIREWORKS_ID in assets:
            wanted[FIREWORKS_ID] = animation_element(FIREWORKS_ID, SKY_X, 0)
        if NIGHT_ID in assets:
            wanted[NIGHT_ID] = animation_element(NIGHT_ID, SKY_X, 0)
        if config.mode_badge and mode in MODE_BADGES:
            word, word_color = MODE_BADGES[mode]
            wanted[MODE_ID] = {
                "id": MODE_ID,
                "type": "text",
                "display": "front",
                "text": word,
                "font": SUBTITLE_FONT,
                "color": word_color,
                "align": "top_left",
                "x": DISPLAY_WIDTH - busybar_font.text_width(word, SUBTITLE_FONT),
                "y": BADGE_Y,
            }
        return wanted

    def payload(
        self,
        status: str,
        label: Optional[str] = None,
        color: Optional[str] = None,
        activity: str = "",
        assets: Optional[Mapping[str, str]] = None,
        mode: str = "",
    ) -> Dict[str, Any]:
        elements = self.elements(status, label, color, activity, assets, mode)
        return {"application_name": self.config.application_name, "elements": list(elements.values())}

    def draw(self, elements: Sequence[Mapping[str, Any]]) -> None:
        body = {"application_name": self.config.application_name, "elements": list(elements)}
        self.request(
            "POST", "/display/draw", body=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            content_type="application/json",
        )

    def erase(self, element_ids: Sequence[str]) -> None:
        """Remove elements. The device answers 400 and removes nothing if any id is not on screen,
        so a rejected batch is retried one id at a time and absent ids count as already gone."""

        def delete(ids: Sequence[str]) -> None:
            self.request(
                "DELETE", "/display/draw", {"application_name": self.config.application_name},
                body=json.dumps({"element_ids": list(ids)}, separators=(",", ":")).encode("utf-8"),
                content_type="application/json",
            )

        try:
            delete(element_ids)
            return
        except urllib.error.HTTPError as error:
            if error.code != 400:
                raise
        if len(element_ids) == 1:
            return
        for element_id in element_ids:
            try:
                delete([element_id])
            except urllib.error.HTTPError as error:
                if error.code != 400:
                    raise

    def erase_all(self) -> None:
        """Clear everything this app has drawn. The device answers 400 when there is nothing to clear."""
        try:
            self.request("DELETE", "/display/draw", {"application_name": self.config.application_name})
        except urllib.error.HTTPError as error:
            if error.code != 400:
                raise

    def show(
        self,
        status: str,
        label: Optional[str] = None,
        color: Optional[str] = None,
        activity: str = "",
        assets: Optional[Mapping[str, str]] = None,
        previous: Optional[Mapping[str, Mapping[str, Any]]] = None,
        mode: str = "",
    ) -> Dict[str, Dict[str, Any]]:
        """Bring the display to `status`, touching only what differs from `previous`.

        Draws merge by element id on the device, so an unchanged animation is left
        alone and keeps playing while the subtitle changes. With no `previous`
        (unknown device state) our display is cleared and everything is drawn
        afresh. Returns the elements now on screen.
        """
        wanted = self.elements(status, label, color, activity, assets, mode)
        if previous is None:
            self.erase_all()
            changed = list(wanted.values())
            stale: List[str] = []
        else:
            changed = [element for element_id, element in wanted.items() if previous.get(element_id) != element]
            stale = [element_id for element_id in previous if element_id not in wanted]
        if changed:
            self.draw(changed)
        if stale:
            self.erase(stale)
        return wanted

    def send(
        self,
        status: str,
        label: Optional[str] = None,
        color: Optional[str] = None,
        activity: str = "",
        assets: Optional[Mapping[str, str]] = None,
        mode: str = "",
    ) -> None:
        self.show(status, label, color, activity, assets, mode=mode)

    def asset_dir(self) -> str:
        return "/ext/user_assets/" + self.config.application_name

    def asset_listing(self) -> Dict[str, int]:
        """File name -> size of this app's uploaded assets."""
        try:
            raw = self.request("GET", "/storage/list", {"path": self.asset_dir()})
        except urllib.error.HTTPError:
            return {}  # the device answers 400 until the app has uploaded anything
        try:
            listing = json.loads(raw.decode("utf-8")).get("list", [])
        except (UnicodeDecodeError, ValueError, AttributeError):
            return {}
        return {
            str(item["name"]): int(item.get("size", 0))
            for item in listing
            if isinstance(item, dict) and item.get("type") == "file" and "name" in item
        }

    def play(self, name: str) -> None:
        """Play an uploaded sound asset on the bar's speaker."""
        body = {"application_name": self.config.application_name, "path": name}
        self.request(
            "POST", "/audio/play", body=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            content_type="application/json",
        )

    def install_asset(self, name: str, blob: bytes) -> None:
        """Upload `blob` as `name`.

        The name is versioned, so this never overwrites a file the display may be
        using (the device answers 508 for that, and removing an in-use asset can
        hang its web server, so old versions are deliberately left in place).
        Uploads are retried because the device briefly answers 5xx while its
        filesystem is busy.
        """
        params = {"file": name, "application_name": self.config.application_name}
        for attempt in range(3):
            try:
                self.request(
                    "POST", "/assets/upload", params, blob, "application/octet-stream",
                    timeout=max(self.config.timeout, ASSET_UPLOAD_TIMEOUT),
                )
                return
            except urllib.error.HTTPError as error:
                if error.code < 500 or attempt == 2:
                    raise
                time.sleep(0.4 * (attempt + 1))

    def check(self) -> Mapping[str, Any]:
        raw = self.request("GET", "/version")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {"response": raw.decode("utf-8", errors="replace")}


def render_fingerprint(config: Config, status: str, activity: str = "", mode: str = "") -> str:
    material = json.dumps(
        [
            LAYOUT_VERSION,
            config.base_url,
            config.auth_mode,
            config.application_name,
            status,
            config.labels[status],
            config.colors[status],
            config.subtitle_color,
            config.animation,
            config.mascot,
            config.waiting_dots,
            config.mode_badge,
            activity,
            mode,
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def asset_plan(config: Config, status: str) -> Dict[str, Tuple[str, Callable[[], bytes]]]:
    """element id -> (asset name, builder) for what `status` animates."""
    plan: Dict[str, Tuple[str, Callable[[], bytes]]] = {}
    if config.mascot:
        plan[MASCOT_ID] = (busybar_mascot.asset_name(status), lambda: busybar_mascot.animation(status))
    if status == "WAITING_FOR_INPUT" and config.waiting_dots:
        dots_color = config.colors[status]
        plan[DOTS_ID] = (busybar_anim.dots_asset_name(dots_color), lambda: busybar_anim.dots_animation(dots_color))
    if status == "IDLE" and config.animation:
        idle_color = config.colors[status]
        plan[ANIMATION_ID] = (
            busybar_anim.breath_asset_name(idle_color, COMET_WIDTH),
            lambda: busybar_anim.breath_animation(idle_color, COMET_WIDTH),
        )
        plan[NIGHT_ID] = (busybar_anim.celebration_asset_name("night"), busybar_anim.night_animation)
    if status == "FINISHED" and config.animation:
        plan[BANG_ID] = (busybar_anim.celebration_asset_name("bang"), busybar_anim.bang_animation)
        plan[ANIMATION_ID] = (
            busybar_anim.celebration_asset_name("rainbow", COMET_WIDTH),
            lambda: busybar_anim.rainbow_animation(COMET_WIDTH),
        )
        plan[FIREWORKS_ID] = (busybar_anim.celebration_asset_name("fireworks"), busybar_anim.fireworks_animation)
    if status == "WAITING_FOR_INPUT" and config.animation:
        ping_color = config.colors[status]
        plan[ANIMATION_ID] = (
            busybar_anim.ping_asset_name(ping_color, COMET_WIDTH),
            lambda: busybar_anim.ping_animation(ping_color, COMET_WIDTH),
        )
    if status == "WORKING" and config.animation:
        color = config.colors["WORKING"]
        plan[ANIMATION_ID] = (
            busybar_anim.animation_asset_name(color, COMET_WIDTH),
            lambda: busybar_anim.working_animation(color, COMET_WIDTH),
        )
    return plan


def resolve_assets(
    config: Config,
    store: Optional[Store],
    client: BusyBarClient,
    plan: Mapping[str, Tuple[str, Callable[[], bytes]]],
    previous: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, str]:
    """Make sure the device holds each planned asset; return element id -> asset name for those that do.

    A missing asset makes the device draw a magenta placeholder, so an element is
    only ever drawn after its asset is confirmed present (versioned name and
    size). Assets are never overwritten or removed. Elements already on screen
    with the right asset are left alone. Failures are remembered for a minute
    (when a store is given) so a flaky device is not hammered on every hook.
    """
    ready: Dict[str, str] = {}
    pending = {}
    for element_id, (name, build) in plan.items():
        if previous and previous.get(element_id, {}).get("path") == name:
            ready[element_id] = name
        else:
            pending[element_id] = (name, build)
    if not pending:
        return ready
    now = time.time()
    failures: Dict[str, float] = {}
    if store is not None:
        with store.lock():
            failures = {
                str(name): float(at)
                for name, at in (store.load_unlocked().get("asset_failures") or {}).items()
                if now - float(at) < ASSET_RETRY_SECONDS
            }
    listing: Optional[Dict[str, int]] = None
    for element_id, (name, build) in pending.items():
        if name in failures:
            continue
        try:
            blob = build()
            if listing is None:
                listing = client.asset_listing()
            if listing.get(name) != len(blob):
                client.install_asset(name, blob)
                listing[name] = len(blob)
                log(config, "installed %s (%d bytes)" % (name, len(blob)))
            ready[element_id] = name
        except Exception as error:
            log(config, "asset %s failed: %s: %s" % (name, type(error).__name__, error))
            failures[name] = now
            if not isinstance(error, urllib.error.HTTPError):
                # Device unreachable: back off from everything instead of timing out once per asset.
                failures.update({other: now for other, _ in pending.values()})
                break
    if store is not None:
        with store.lock():
            data = store.load_unlocked()
            data["asset_failures"] = failures
            store.save_unlocked(data)
    return ready


def play_sound(config: Config, store: Optional[Store], client: BusyBarClient, kind: str) -> bool:
    """Make sure the sound is on the bar (uploaded once, versioned) and play it. Never raises."""
    try:
        name = busybar_sound.sound_asset_name(kind)
        plan = {SOUND_ID: (name, lambda: busybar_sound.sound(kind))}
        if SOUND_ID not in resolve_assets(config, store, client, plan):
            return False
        client.play(name)
        log(config, "played %s" % kind)
        return True
    except Exception as error:
        log(config, "sound %s failed: %s: %s" % (kind, type(error).__name__, error))
        return False


def sound_due(
    config: Config,
    status: str,
    previous_status: Optional[str],
    turn_seconds: Optional[float],
    last_sound_at: float,
    now: float,
) -> bool:
    """A sound plays when the bar changes into WAITING, or into FINISHED after a long enough turn."""
    if not config.sounds or status not in SOUND_STATES or status == previous_status:
        return False
    if status == "FINISHED" and (turn_seconds is None or turn_seconds < config.sound_min_seconds):
        return False
    return now - last_sound_at >= SOUND_COOLDOWN


def dispatch(config: Config, force: bool = False, delay: float = 0.04) -> bool:
    if not config.enabled:
        return True
    if delay:
        time.sleep(delay)
    store = Store(config)
    with store.lock(dispatch=True):
        with store.lock():
            data = store.load_unlocked()
            store.prune_unlocked(data, time.time())
            status = aggregate_state(data["sessions"])
            activity = aggregate_activity(data["sessions"], status)
            mode = aggregate_mode(data["sessions"], status)
            turn_seconds = aggregate_turn_seconds(data["sessions"], status)
            previous_status = data.get("last_status")
            last_sound_at = float(data.get("last_sound_at", 0))
            fingerprint = render_fingerprint(config, status, activity, mode)
            if not force and data.get("last_sent") == fingerprint:
                store.save_unlocked(data)
                return True
            previous = None if force else data.get("drawn")
            store.save_unlocked(data)
        client = BusyBarClient(config)
        try:
            assets = resolve_assets(config, store, client, asset_plan(config, status), previous)
            drawn = client.show(status, activity=activity, assets=assets, previous=previous, mode=mode)
        except Exception as error:  # A device failure must never reach Claude Code.
            log(config, "send %s failed: %s: %s" % (status, type(error).__name__, error))
            with contextlib.suppress(Exception):
                with store.lock():  # the screen may be half-updated: redraw everything next time
                    latest = store.load_unlocked()
                    latest["drawn"] = None
                    store.save_unlocked(latest)
            return False
        with store.lock():
            latest = store.load_unlocked()
            latest["drawn"] = drawn
            latest["last_status"] = status
            latest_status = aggregate_state(latest["sessions"])
            latest_activity = aggregate_activity(latest["sessions"], latest_status)
            latest_mode = aggregate_mode(latest["sessions"], latest_status)
            if render_fingerprint(config, latest_status, latest_activity, latest_mode) == fingerprint:
                latest["last_sent"] = fingerprint
            store.save_unlocked(latest)
        log(config, "sent %s %s" % (status, activity))
        now = time.time()
        if sound_due(config, status, previous_status, turn_seconds, last_sound_at, now) and play_sound(
            config, store, client, SOUND_STATES[status]
        ):
            with contextlib.suppress(Exception):
                with store.lock():
                    latest = store.load_unlocked()
                    latest["last_sound_at"] = now
                    store.save_unlocked(latest)
        return True


def detached(config: Config, mode: str) -> Optional[int]:
    if os.environ.get("BUSYBAR_NO_DETACH") == "1":
        if mode == "_dispatch":
            dispatch(config, delay=0)
        return None
    try:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), mode],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=os.environ.copy(),
        )
        return process.pid
    except OSError as error:
        log(config, "could not launch %s: %s" % (mode, error))
        return None


def pid_is_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def ensure_reaper(config: Config) -> None:
    store = Store(config)
    with store.lock():
        try:
            pid = int(store.pid_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            pid = 0
        if pid_is_alive(pid):
            return
        pid = detached(config, "_reaper")
        if pid:
            store.pid_path.write_text(str(pid), encoding="ascii")
            os.chmod(str(store.pid_path), 0o600)


def reaper(config: Config) -> int:
    store = Store(config)
    store.ensure()
    try:
        store.pid_path.write_text(str(os.getpid()), encoding="ascii")
        os.chmod(str(store.pid_path), 0o600)
        while True:
            time.sleep(min(30.0, max(1.0, config.stale_seconds / 4.0)))
            with store.lock():
                data = store.load_unlocked()
                changed = store.prune_unlocked(data, time.time())
                has_sessions = bool(data["sessions"])
                if changed:
                    store.save_unlocked(data)
            if changed:
                dispatch(config, delay=0)
            if not has_sessions:
                break
        return 0
    finally:
        try:
            if store.pid_path.read_text(encoding="ascii").strip() == str(os.getpid()):
                store.pid_path.unlink()
        except OSError:
            pass


def handle_hook(config: Config, payload: Mapping[str, Any]) -> int:
    try:
        status, changed = apply_event(Store(config), payload)
        log(config, "event=%s aggregate=%s changed=%s" % (payload.get("hook_event_name"), status, changed))
        if config.enabled and changed:
            detached(config, "_dispatch")
        if payload.get("hook_event_name") != "SessionEnd":
            ensure_reaper(config)
    except Exception as error:
        log(config, "hook failed safely: %s: %s" % (type(error).__name__, error))
    return 0


def forget_drawn(config: Config) -> None:
    """After drawing outside the hook path, make the next hook redraw from scratch."""
    store = Store(config)
    with contextlib.suppress(Exception):
        with store.lock():
            data = store.load_unlocked()
            data["drawn"] = None
            data["last_sent"] = None
            store.save_unlocked(data)


def config_with_cli(config: Config, args: argparse.Namespace) -> Config:
    updates: Dict[str, Any] = {}
    if getattr(args, "url", None):
        updates["base_url"] = normalize_base_url(args.url)
    if getattr(args, "auth", None):
        updates["auth_mode"] = args.auth
    if getattr(args, "token", None) is not None:
        updates["token"] = args.token
    if getattr(args, "timeout", None) is not None:
        updates["timeout"] = args.timeout
    return dataclasses.replace(config, **updates)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send Claude Code status to a BUSY Bar")
    parser.add_argument("--url", help="API base URL, device address, or hostname")
    parser.add_argument("--auth", choices=("usb", "wifi", "cloud"), help="authentication mode")
    parser.add_argument("--token", help="Wi-Fi password or cloud API token")
    parser.add_argument("--timeout", type=float, help="HTTP timeout in seconds")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="{check,send,demo,assets,state}")
    subparsers.add_parser("hook")
    subparsers.add_parser("check", help="verify the connection and print the API version response")
    send = subparsers.add_parser("send", help="send one status immediately")
    send.add_argument("status", choices=STATUSES)
    send.add_argument("--label", help="one-off display label")
    send.add_argument("--color", help="one-off #RRGGBBAA text color")
    send.add_argument("--activity", default="", help="one-off subtitle under the status label")
    send.add_argument("--mode", default="", choices=[""] + list(MODE_BADGES), help="one-off permission-mode badge")
    subparsers.add_parser("demo", help="send every status in sequence")
    subparsers.add_parser("assets", help="install the animations and sounds on the device if missing")
    sound = subparsers.add_parser("sound", help="play one of the notification sounds on the bar")
    sound.add_argument("kind", choices=sorted(busybar_sound.KINDS))
    subparsers.add_parser("state", help="print the shared session state")
    subparsers.add_parser("_dispatch")
    subparsers.add_parser("_reaper")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = config_with_cli(Config.from_environment(), args)
    if args.command == "hook":
        try:
            payload = json.load(sys.stdin)
            if not isinstance(payload, dict):
                payload = {}
        except (ValueError, OSError):
            payload = {}
        return handle_hook(config, payload)
    if args.command == "_dispatch":
        dispatch(config)
        return 0
    if args.command == "_reaper":
        return reaper(config)
    if args.command == "check":
        try:
            print(json.dumps(BusyBarClient(config).check(), indent=2, sort_keys=True))
            return 0
        except Exception as error:
            print("BUSY Bar connection failed: %s: %s" % (type(error).__name__, error), file=sys.stderr)
            return 1
    if args.command == "send":
        try:
            client = BusyBarClient(config)
            plan = asset_plan(config, args.status)
            assets = resolve_assets(config, None, client, plan)
            if len(assets) < len(plan):
                print("warning: could not install every animation; drawing without it (try DEBUG_LOGGING)", file=sys.stderr)
            client.send(args.status, args.label, args.color, args.activity, assets, args.mode)
            forget_drawn(config)
            print("Sent %s to %s" % (args.status, config.base_url))
            return 0
        except Exception as error:
            print("BUSY Bar update failed: %s: %s" % (type(error).__name__, error), file=sys.stderr)
            return 1
    if args.command == "demo":
        try:
            client = BusyBarClient(config)
            for status in STATUSES:
                assets = resolve_assets(config, None, client, asset_plan(config, status))
                client.send(status, activity=DEMO_SUBTITLES.get(status, ""), assets=assets)
                print("Sent " + status)
                if config.sounds and status in SOUND_STATES:
                    play_sound(config, None, client, SOUND_STATES[status])
                if status != STATUSES[-1]:
                    time.sleep(5)
            forget_drawn(config)
            return 0
        except Exception as error:
            print("BUSY Bar demo failed: %s: %s" % (type(error).__name__, error), file=sys.stderr)
            return 1
    if args.command == "sound":
        client = BusyBarClient(config)
        if not play_sound(config, None, client, args.kind):
            print("BUSY Bar sound failed (enable DEBUG_LOGGING for details)", file=sys.stderr)
            return 1
        print("Played %s" % args.kind)
        return 0
    if args.command == "assets":
        try:
            client = BusyBarClient(config)
            missing = 0
            for status in STATUSES:
                plan = asset_plan(config, status)
                ready = resolve_assets(config, None, client, plan)
                missing += len(plan) - len(ready)
                for name in sorted(set(ready.values())):
                    print("ready  " + name)
            if config.sounds:
                for kind in sorted(busybar_sound.KINDS):
                    name = busybar_sound.sound_asset_name(kind)
                    plan = {SOUND_ID: (name, lambda kind=kind: busybar_sound.sound(kind))}
                    if SOUND_ID in resolve_assets(config, None, client, plan):
                        print("ready  " + name)
                    else:
                        missing += 1
            if missing:
                print("BUSY Bar asset install incomplete (enable DEBUG_LOGGING for details)", file=sys.stderr)
                return 1
            return 0
        except Exception as error:
            print("BUSY Bar asset install failed: %s: %s" % (type(error).__name__, error), file=sys.stderr)
            return 1
    if args.command == "state":
        store = Store(config)
        with store.lock():
            print(json.dumps(store.load_unlocked(), indent=2, sort_keys=True))
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
