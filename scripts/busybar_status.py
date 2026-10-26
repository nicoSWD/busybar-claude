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
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Sequence, Tuple

import busybar_anim
import busybar_font
import busybar_mascot


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
    "LIMIT": "LIMIT",
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
    "IDLE": "#606060FF",
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
# Estimated advance per character (px), to decide where a label fits. Measured on the device:
# "WORKING" is 32px in `small`, "Searching" 31px in `tiny`.
GLYPH_ADVANCE = {"small": 5, "tiny": 4}
DOTS_ID = "claude-dots"
DOTS_GAP = 1  # pixels between the last letter and the first dot, as between letters
MASCOT_ID = "claude-mascot"
MASCOT_WIDTH = busybar_mascot.SIZE
DISPLAY_WIDTH = 72
MANAGED_IDS = (TITLE_ID, SUBTITLE_ID, MASCOT_ID, ANIMATION_ID, DOTS_ID)
LAYOUT_VERSION = 6  # bump to force a redraw after changing the layout above
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


def aggregate_state(sessions: Mapping[str, Mapping[str, Any]]) -> str:
    if not sessions:
        return "IDLE"
    return max((str(item.get("state", "IDLE")) for item in sessions.values()), key=lambda state: STATE_PRIORITY.get(state, -1))


def aggregate_activity(sessions: Mapping[str, Mapping[str, Any]], state: str) -> str:
    if state != "WORKING":
        return ""
    working = [item for item in sessions.values() if item.get("state") == "WORKING"]
    if not working:
        return ""
    latest = max(working, key=lambda item: float(item.get("updated_at", 0)))
    return str(latest.get("activity", ""))


def apply_event(store: Store, payload: Mapping[str, Any], now: Optional[float] = None) -> Tuple[str, bool]:
    now = time.time() if now is None else now
    event = str(payload.get("hook_event_name", ""))
    session_id = str(payload.get("session_id") or payload.get("transcript_path") or "unknown-session")
    with store.lock():
        data = store.load_unlocked()
        store.prune_unlocked(data, now)
        previous = aggregate_state(data["sessions"])
        previous_activity = aggregate_activity(data["sessions"], previous)
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
                needs_dispatch = data.get("last_sent") != render_fingerprint(store.config, previous, previous_activity)
                return previous, needs_dispatch
            record = {"state": state, "updated_at": now, "event": event}
            if event == "PreCompact":
                resume = old.get("resume_state") if old.get("event") == "PreCompact" else old.get("state")
                if resume:
                    record["resume_state"] = resume
            if state == "WORKING":
                activity = event_activity(payload)
                if activity is None:
                    activity = str(old.get("activity", "")) if old.get("state") == "WORKING" else ""
                record["activity"] = activity or "Thinking"
            data["sessions"][session_id] = record
            if event in REDRAW_EVENTS:
                data["drawn"] = None
        current = aggregate_state(data["sessions"])
        current_activity = aggregate_activity(data["sessions"], current)
        needs_dispatch = (
            current != previous
            or current_activity != previous_activity
            or data.get("last_sent") != render_fingerprint(store.config, current, current_activity)
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
    ) -> Dict[str, Dict[str, Any]]:
        """The complete set of elements to show for a state, keyed by element id.

        `assets` maps the mascot / comet element ids to the uploaded asset each
        should play; an element without an asset is simply not drawn.
        """
        assets = assets or {}
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
                estimate = len(content) * GLYPH_ADVANCE[font]
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
            wanted[SUBTITLE_ID] = text_element(SUBTITLE_ID, subtitle, SUBTITLE_FONT, SUBTITLE_Y, config.subtitle_color)
        if DOTS_ID in assets:
            # Lay the label and its animated dots out as one unit centered like a plain title.
            label_width = busybar_font.text_width(text, TITLE_FONT)
            unit = label_width + DOTS_GAP + busybar_anim.DOTS_WIDTH
            if not config.mascot or unit <= DISPLAY_WIDTH - 2 * MASCOT_WIDTH:
                center = DISPLAY_WIDTH // 2
            else:
                center = config.column_x + config.column_width // 2
            left = center - unit // 2
            if left >= config.column_x:  # otherwise it cannot fit: show the plain label, no dots
                wanted[TITLE_ID].update(align="top_left", x=left)
                wanted[TITLE_ID].pop("width", None)
                wanted[DOTS_ID] = animation_element(DOTS_ID, left + label_width + DOTS_GAP, title_y + 6)
        if MASCOT_ID in assets:
            wanted[MASCOT_ID] = animation_element(MASCOT_ID, 0, 0)
        if ANIMATION_ID in assets:
            wanted[ANIMATION_ID] = animation_element(ANIMATION_ID, config.column_x, ANIMATION_Y)
        return wanted

    def payload(
        self,
        status: str,
        label: Optional[str] = None,
        color: Optional[str] = None,
        activity: str = "",
        assets: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        elements = self.elements(status, label, color, activity, assets)
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

    def show(
        self,
        status: str,
        label: Optional[str] = None,
        color: Optional[str] = None,
        activity: str = "",
        assets: Optional[Mapping[str, str]] = None,
        previous: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Bring the display to `status`, touching only what differs from `previous`.

        Draws merge by element id on the device, so an unchanged animation is left
        alone and keeps playing while the subtitle changes. With no `previous`
        (unknown device state) every wanted element is sent and leftovers of ours
        are erased. Returns the elements now on screen.
        """
        wanted = self.elements(status, label, color, activity, assets)
        if previous is None:
            changed = list(wanted.values())
            stale = [element_id for element_id in MANAGED_IDS if element_id not in wanted]
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
    ) -> None:
        self.show(status, label, color, activity, assets)

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


def render_fingerprint(config: Config, status: str, activity: str = "") -> str:
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
            activity,
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
    if status == "WORKING" and config.animation:
        color, width = config.colors["WORKING"], config.column_width
        plan[ANIMATION_ID] = (
            busybar_anim.animation_asset_name(color, width),
            lambda: busybar_anim.working_animation(color, width),
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
            fingerprint = render_fingerprint(config, status, activity)
            if not force and data.get("last_sent") == fingerprint:
                store.save_unlocked(data)
                return True
            previous = None if force else data.get("drawn")
            store.save_unlocked(data)
        client = BusyBarClient(config)
        try:
            assets = resolve_assets(config, store, client, asset_plan(config, status), previous)
            drawn = client.show(status, activity=activity, assets=assets, previous=previous)
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
            latest_status = aggregate_state(latest["sessions"])
            latest_activity = aggregate_activity(latest["sessions"], latest_status)
            if render_fingerprint(config, latest_status, latest_activity) == fingerprint:
                latest["last_sent"] = fingerprint
            store.save_unlocked(latest)
        log(config, "sent %s %s" % (status, activity))
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
    subparsers.add_parser("demo", help="send all five statuses in sequence")
    subparsers.add_parser("assets", help="install the mascot and WORKING animations on the device if missing")
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
            client.send(args.status, args.label, args.color, args.activity, assets)
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
                client.send(status, activity="Editing" if status == "WORKING" else "", assets=assets)
                print("Sent " + status)
                if status != STATUSES[-1]:
                    time.sleep(5)
            forget_drawn(config)
            return 0
        except Exception as error:
            print("BUSY Bar demo failed: %s: %s" % (type(error).__name__, error), file=sys.stderr)
            return 1
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
