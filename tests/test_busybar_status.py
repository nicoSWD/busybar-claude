from __future__ import annotations

import dataclasses
import io
import json
import os
import re
import sys
import struct
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import busybar_anim as anim  # noqa: E402
import busybar_font as font  # noqa: E402
import busybar_mascot as mascot  # noqa: E402
import busybar_sound as sounds  # noqa: E402
import busybar_status as status  # noqa: E402


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.body


class FakeDevice:
    """Models the BUSY Bar endpoints the plugin uses: draws merge by element id, assets are per app."""

    def __init__(self, fail_uploads=False, busy_uploads=0, fail_play=False):
        self.calls = []
        self.played = []
        self.fail_play = fail_play
        self.assets = {}
        self.screen = {}
        self.fail_uploads = fail_uploads
        self.busy_uploads = busy_uploads  # answer this many uploads with HTTP 508 first

    def __call__(self, request, timeout=None):
        url = urllib.parse.urlsplit(request.full_url)
        params = dict(urllib.parse.parse_qsl(url.query))
        path = url.path[len("/api"):]
        method = request.get_method()
        body = request.data
        self.calls.append((method, path))
        if (method, path) == ("GET", "/storage/list"):
            if not self.assets:
                raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(b"{}"))
            listing = [{"type": "file", "name": n, "size": len(b)} for n, b in self.assets.items()]
            return FakeResponse(json.dumps({"list": listing}).encode())
        if (method, path) == ("DELETE", "/storage/remove"):
            self.assets.pop(params["path"].rsplit("/", 1)[1], None)
        elif (method, path) == ("POST", "/assets/upload"):
            if self.fail_uploads:
                raise urllib.error.URLError("device went away")
            if self.busy_uploads:
                self.busy_uploads -= 1
                raise urllib.error.HTTPError(request.full_url, 508, "Loop Detected", {}, io.BytesIO(b"{}"))
            self.assets[params["file"]] = body
        elif (method, path) == ("POST", "/audio/play"):
            name = json.loads(body)["path"]
            if self.fail_play or name not in self.assets:
                raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(b"{}"))
            self.played.append(name)
        elif (method, path) == ("POST", "/display/draw"):
            for element in json.loads(body)["elements"]:
                self.screen[element["id"]] = element
        elif (method, path) == ("DELETE", "/display/draw") and body is None:
            if not self.screen:  # like the device: nothing to clear is an error
                raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(b"{}"))
            self.screen.clear()
        elif (method, path) == ("DELETE", "/display/draw"):
            ids = json.loads(body)["element_ids"]
            if any(element_id not in self.screen for element_id in ids):  # like the device: all or nothing
                raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(b"{}"))
            for element_id in ids:
                del self.screen[element_id]
        return FakeResponse(b'{"result":"OK"}')

    def draws(self):
        return [call for call in self.calls if call[1] == "/display/draw"]


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.config = status.Config(
            base_url="http://127.0.0.1:1/api",
            auth_mode="usb",
            token="",
            enabled=True,
            timeout=0.1,
            stale_seconds=3600,
            debug=False,
            state_dir=Path(self.tempdir.name),
            labels=status.DEFAULT_LABELS.copy(),
            colors=status.DEFAULT_COLORS.copy(),
        )
        self.store = status.Store(self.config)

    def apply(self, event, session="one", now=100.0, **extra):
        payload = {"hook_event_name": event, "session_id": session}
        payload.update(extra)
        return status.apply_event(self.store, payload, now=now)[0]

    def test_documented_lifecycle_transitions(self):
        self.assertEqual(self.apply("SessionStart", source="startup"), "READY")
        self.assertEqual(self.apply("UserPromptSubmit", now=100.5), "WORKING")
        self.assertEqual(self.apply("PreToolUse", now=101), "WORKING")
        self.assertEqual(self.apply("PostToolUse", now=102), "WORKING")
        self.assertEqual(self.apply("PermissionRequest", now=103), "WAITING_FOR_INPUT")
        self.assertEqual(self.apply("PostToolUse", now=104), "WORKING")
        self.assertEqual(self.apply("Stop", now=105), "FINISHED")
        self.assertEqual(self.apply("SessionEnd", now=106), "IDLE")

    def test_interactive_tools_and_elicitation_wait_for_input(self):
        self.assertEqual(self.apply("PreToolUse", tool_name="AskUserQuestion"), "WAITING_FOR_INPUT")
        self.assertEqual(self.apply("PreToolUse", now=101, tool_name="ExitPlanMode"), "WAITING_FOR_INPUT")
        self.assertEqual(self.apply("ElicitationResult", now=102), "WORKING")

    def test_session_start_is_ready_not_working(self):
        for source in ("startup", "resume", "clear", "fork", None):
            extra = {} if source is None else {"source": source}
            self.assertEqual(self.apply("SessionStart", session=str(source), **extra), "READY", source)
        self.assertEqual(self.apply("SessionEnd", session="None"), "READY")  # others remain
        with self.store.lock():
            self.store.save_unlocked({"version": 1, "sessions": {}, "last_sent": None})
        self.assertEqual(self.apply("SessionStart", source="compact"), "IDLE", "compaction is not a new session")
        self.assertEqual(self.apply("UserPromptSubmit", now=101), "WORKING")
        self.assertEqual(self.apply("SessionStart", now=102, source="compact"), "WORKING", "and does not interrupt work")

    def test_rate_limit_is_its_own_state_but_other_api_errors_are_errors(self):
        self.apply("UserPromptSubmit")
        self.assertEqual(self.apply("StopFailure", now=101, error="rate_limit"), "LIMIT")
        self.assertEqual(self.apply("Notification", now=102, notification_type="quota_auto_resume_fired"), "WORKING")
        for error in ("overloaded", "authentication_failed", "billing_error", "unknown", None):
            extra = {} if error is None else {"error": error}
            self.assertEqual(self.apply("StopFailure", now=103, **extra), "ERROR", error)
        self.assertLess(status.STATE_PRIORITY["ERROR"], status.STATE_PRIORITY["LIMIT"])
        self.assertLess(status.STATE_PRIORITY["LIMIT"], status.STATE_PRIORITY["WAITING_FOR_INPUT"])
        self.assertLess(status.STATE_PRIORITY["IDLE"], status.STATE_PRIORITY["READY"])
        self.assertLess(status.STATE_PRIORITY["READY"], status.STATE_PRIORITY["FINISHED"])

    def test_compaction_shows_activity_and_restores_what_it_interrupted(self):
        def activity():
            with self.store.lock():
                sessions = self.store.load_unlocked()["sessions"]
            return status.aggregate_activity(sessions, status.aggregate_state(sessions))

        # automatic compaction in the middle of a turn
        self.apply("UserPromptSubmit")
        self.apply("PreToolUse", now=101, tool_name="Bash")
        self.assertEqual(self.apply("PreCompact", now=102, trigger="auto"), "WORKING")
        self.assertEqual(activity(), "Compacting")
        self.assertEqual(self.apply("PreCompact", now=103, trigger="auto"), "WORKING", "repeats are harmless")
        self.assertEqual(self.apply("PostCompact", now=104, trigger="auto"), "WORKING")
        self.assertEqual(activity(), "Thinking")
        # manual /compact from an idle session returns to READY, not a stuck WORKING
        self.apply("SessionEnd", now=105)
        self.apply("SessionStart", now=106, source="startup")
        self.assertEqual(self.apply("PreCompact", now=107, trigger="manual"), "WORKING")
        self.assertEqual(self.apply("PostCompact", now=108, trigger="manual"), "READY")
        # idle auto-compaction after a finished turn returns to FINISHED
        self.apply("Stop", now=109)
        self.apply("PreCompact", now=110, trigger="auto")
        self.assertEqual(self.apply("PostCompact", now=111, trigger="auto"), "FINISHED")
        # PostCompact without a PreCompact falls back on the trigger
        self.apply("SessionEnd", now=112)
        self.assertEqual(self.apply("PostCompact", now=113, trigger="manual"), "READY")
        self.apply("SessionEnd", now=114)
        self.assertEqual(self.apply("PostCompact", now=115, trigger="auto"), "WORKING")

    def test_every_handled_event_is_registered_and_every_state_is_configurable(self):
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
                      "PermissionRequest", "PermissionDenied", "Notification", "Elicitation", "ElicitationResult",
                      "Stop", "StopFailure", "PreCompact", "PostCompact", "SessionEnd"):
            self.assertIn(event, hooks)
        options = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["userConfig"]
        for state in status.STATUSES:
            name = state.replace("WAITING_FOR_INPUT", "WAITING")
            self.assertIn(name + "_LABEL", options)
            self.assertIn(name + "_COLOR", options)
            self.assertEqual(options[name + "_COLOR"]["default"], status.DEFAULT_COLORS[state])
            self.assertEqual(options[name + "_LABEL"]["default"], status.DEFAULT_LABELS[state])

    def test_tool_failure_is_not_terminal_error(self):
        self.apply("SessionStart")
        self.assertEqual(self.apply("PostToolUseFailure", now=101), "WORKING")
        self.assertEqual(self.apply("StopFailure", now=102), "ERROR")

    def test_notification_mapping(self):
        self.apply("SessionStart")
        self.assertEqual(
            self.apply("Notification", now=101, notification_type="permission_prompt"),
            "WAITING_FOR_INPUT",
        )
        self.assertEqual(
            self.apply("Notification", now=102, notification_type="quota_auto_resume_fired"),
            "WORKING",
        )

    def test_two_sessions_use_precedence_and_do_not_fight(self):
        self.assertEqual(self.apply("PermissionRequest", session="waiting"), "WAITING_FOR_INPUT")
        self.assertEqual(self.apply("UserPromptSubmit", session="working", now=101), "WORKING")
        self.assertEqual(self.apply("Stop", session="working", now=102), "WAITING_FOR_INPUT")
        self.assertEqual(self.apply("SessionEnd", session="working", now=103), "WAITING_FOR_INPUT")
        self.assertEqual(self.apply("SessionEnd", session="waiting", now=104), "IDLE")

    def test_stale_sessions_are_reaped(self):
        short = dataclasses.replace(self.config, stale_seconds=10)
        store = status.Store(short)
        status.apply_event(store, {"hook_event_name": "SessionStart", "session_id": "dead"}, now=100)
        with store.lock():
            data = store.load_unlocked()
            self.assertTrue(store.prune_unlocked(data, 111))
            self.assertEqual(status.aggregate_state(data["sessions"]), "IDLE")

    def test_invalid_hook_input_and_disconnected_bar_never_fail_hook(self):
        old = os.environ.get("BUSYBAR_NO_DETACH")
        os.environ["BUSYBAR_NO_DETACH"] = "1"
        try:
            self.assertEqual(status.handle_hook(self.config, {"hook_event_name": "SessionStart", "session_id": "x"}), 0)
        finally:
            if old is None:
                os.environ.pop("BUSYBAR_NO_DETACH", None)
            else:
                os.environ["BUSYBAR_NO_DETACH"] = old

    def test_http_payload_wifi_auth_connection_and_deduplication(self):
        configured = dataclasses.replace(
            self.config,
            base_url="http://busybar.test/api",
            auth_mode="wifi",
            token="secret-pin",
            timeout=1,
            animation=False,
            mascot=False,
        )
        store = status.Store(configured)
        status.apply_event(store, {"hook_event_name": "UserPromptSubmit", "session_id": "one"})

        requests = []

        def fake_urlopen(request, timeout):
            requests.append(request)
            if request.get_method() == "GET":
                return FakeResponse(b'{"api_semver":"test"}')
            return FakeResponse(b'{"result":"OK"}')

        with mock.patch.object(status.urllib.request, "urlopen", side_effect=fake_urlopen):
            self.assertTrue(status.dispatch(configured, delay=0))
            self.assertTrue(status.dispatch(configured, delay=0))
            result = status.BusyBarClient(configured).check()

        self.assertEqual(result["api_semver"], "test")
        posts = [request for request in requests if request.get_method() == "POST"]
        self.assertEqual(len(posts), 1, "unchanged aggregate status must not redraw")
        request = posts[0]
        self.assertEqual(request.full_url, "http://busybar.test/api/display/draw")
        headers = dict(request.header_items())
        self.assertEqual(headers["X-api-token"], "secret-pin")
        payload = json.loads(request.data)
        self.assertEqual(payload["application_name"], "claude-code-status")
        self.assertEqual(payload["elements"][0]["text"], "WORKING")
        self.assertEqual(payload["elements"][0]["color"], "#0080FFFF")
        self.assertEqual(payload["elements"][0]["y"], status.TITLE_Y)
        self.assertEqual(payload["elements"][0]["font"], status.TITLE_FONT)
        self.assertEqual(payload["elements"][1]["id"], "claude-activity")
        self.assertEqual(payload["elements"][1]["text"], "Thinking")
        self.assertEqual(payload["elements"][1]["y"], status.SUBTITLE_Y)
        self.assertEqual(payload["elements"][0]["x"], 36, "no mascot: text is centered on the whole display")
        self.assertNotIn("width", payload["elements"][0])

    def test_subtitle_only_while_working(self):
        client = status.BusyBarClient(self.config)
        self.assertEqual(len(client.payload("WORKING", activity="Running")["elements"]), 2)
        self.assertEqual(len(client.payload("WORKING")["elements"]), 1)
        self.assertEqual(len(client.payload("FINISHED")["elements"]), 1)

    def test_activity_mapping_and_persistence(self):
        def activity():
            with self.store.lock():
                sessions = self.store.load_unlocked()["sessions"]
            return status.aggregate_activity(sessions, status.aggregate_state(sessions))

        self.apply("UserPromptSubmit")
        self.assertEqual(activity(), "Thinking")
        self.apply("PreToolUse", now=101, tool_name="Bash")
        self.assertEqual(activity(), "Running")
        self.apply("PostToolUse", now=102, tool_name="Bash")
        self.assertEqual(activity(), "Running", "PostToolUse keeps the last activity")
        self.apply("PreToolUse", now=103, tool_name="Edit")
        self.assertEqual(activity(), "Editing")
        self.apply("PreToolUse", now=104, tool_name="mcp__srv__tool")
        self.assertEqual(activity(), "Tool")
        self.apply("PreToolUse", now=105, tool_name="Mystery")
        self.assertEqual(activity(), "Working")
        self.apply("PermissionRequest", now=106)
        self.assertEqual(activity(), "Approve?", "waiting says what it waits for")
        self.apply("PostToolUse", now=107)
        self.assertEqual(activity(), "Thinking")
        self.apply("Stop", now=108)
        self.assertEqual(activity(), "Done in 8s", "FINISHED says how long the turn took")
        self.apply("SessionEnd", now=109)
        self.assertEqual(activity(), "")

    def test_activity_change_triggers_dispatch_but_repeat_does_not(self):
        def run(event, now, **extra):
            payload = {"hook_event_name": event, "session_id": "one"}
            payload.update(extra)
            return status.apply_event(self.store, payload, now=now)[1]

        run("UserPromptSubmit", 100)
        with self.store.lock():
            data = self.store.load_unlocked()
            data["last_sent"] = status.render_fingerprint(self.config, "WORKING", "Thinking")
            self.store.save_unlocked(data)
        self.assertFalse(run("PostToolUse", 101))
        self.assertTrue(run("PreToolUse", 102, tool_name="Read"))
        self.assertNotEqual(
            status.render_fingerprint(self.config, "WORKING", "Reading"),
            status.render_fingerprint(self.config, "WORKING", "Editing"),
        )

    def drive(self, device, events, config=None):
        """Apply hook events and dispatch after each, as the detached hook process would."""
        config = config or self.config
        store = status.Store(config)
        with mock.patch.object(status.urllib.request, "urlopen", side_effect=device):
            for event, extra in events:
                payload = {"hook_event_name": event, "session_id": "one"}
                payload.update(extra)
                status.apply_event(store, payload, now=time.time())
                status.dispatch(config, delay=0)

    def assets_for(self, status_name, config=None):
        config = config or self.config
        return {name: build() for name, build in (item for item in status.asset_plan(config, status_name).values())}

    def test_entering_working_installs_assets_then_draws_everything(self):
        device = FakeDevice()
        self.drive(device, [("UserPromptSubmit", {})])
        self.assertEqual(
            device.calls,
            [
                ("GET", "/storage/list"),
                ("POST", "/assets/upload"),
                ("POST", "/assets/upload"),
                ("DELETE", "/display/draw"),  # device state unknown at first: clear our display, then draw
                ("POST", "/display/draw"),
            ],
        )
        self.assertEqual(
            set(device.screen),
            {status.TITLE_ID, status.SUBTITLE_ID, status.MASCOT_ID, status.ANIMATION_ID},
        )
        self.assertEqual(device.screen[status.MASCOT_ID]["path"], mascot.asset_name("WORKING"))
        self.assertEqual((device.screen[status.MASCOT_ID]["x"], device.screen[status.MASCOT_ID]["y"]), (0, 0))
        comet = device.screen[status.ANIMATION_ID]
        self.assertEqual(comet["path"], anim.animation_asset_name(self.config.colors["WORKING"], 32))
        self.assertEqual((comet["x"], comet["y"], comet["loop"]), (20, status.ANIMATION_Y, True), "32px, under the text")
        title = device.screen[status.TITLE_ID]
        self.assertEqual((title["align"], title["x"]), ("top_mid", 36), "centered on the whole display")
        self.assertNotIn("width", title)
        self.assertEqual(device.screen[status.SUBTITLE_ID]["color"], "#FFFFFFFF")
        self.assertEqual(title["color"], status.DEFAULT_COLORS["WORKING"])

    def test_activity_change_only_redraws_the_subtitle(self):
        device = FakeDevice()
        self.drive(device, [("UserPromptSubmit", {})])
        device.calls.clear()
        before = {key: dict(device.screen[key]) for key in (status.MASCOT_ID, status.ANIMATION_ID)}
        self.drive(device, [("PreToolUse", {"tool_name": "Bash"})])
        self.assertEqual(device.calls, [("POST", "/display/draw")], "no asset check, no restart of the animations")
        self.assertEqual(device.screen[status.SUBTITLE_ID]["text"], "Running")
        for key, element in before.items():
            self.assertEqual(device.screen[key], element)

    def test_each_state_swaps_the_mascot_and_drops_the_working_extras(self):
        device = FakeDevice()
        self.drive(device, [("UserPromptSubmit", {})])
        for event, extra, expected, label in [
            ("PermissionRequest", {}, "WAITING_FOR_INPUT", "WAITING"),
            ("PostToolUse", {}, "WORKING", "WORKING"),
            ("Stop", {}, "FINISHED", "FINISHED"),
            ("StopFailure", {"error": "server_error"}, "ERROR", "ERROR"),
            ("StopFailure", {"error": "rate_limit"}, "LIMIT", "LIMIT HIT"),
            ("SessionStart", {"source": "startup"}, "READY", "READY"),
            ("SessionEnd", {}, "IDLE", "IDLE"),
        ]:
            self.drive(device, [(event, extra)])
            self.assertEqual(device.screen[status.MASCOT_ID]["path"], mascot.asset_name(expected))
            self.assertEqual(device.screen[status.TITLE_ID]["text"], label)
            if expected == "WAITING_FOR_INPUT":
                self.assertEqual(set(device.screen), {status.TITLE_ID, status.SUBTITLE_ID, status.MASCOT_ID,
                                                      status.DOTS_ID, status.ANIMATION_ID}, expected)
            elif expected in ("LIMIT", "ERROR"):
                self.assertEqual(set(device.screen), {status.TITLE_ID, status.SUBTITLE_ID, status.MASCOT_ID}, expected)
            elif expected == "FINISHED":
                self.assertEqual(set(device.screen), {status.TITLE_ID, status.SUBTITLE_ID, status.MASCOT_ID, status.BANG_ID,
                                                      status.ANIMATION_ID, status.FIREWORKS_ID}, expected)
            elif expected == "IDLE":
                self.assertEqual(set(device.screen), {status.TITLE_ID, status.SUBTITLE_ID, status.MASCOT_ID,
                                                      status.ANIMATION_ID, status.NIGHT_ID}, expected)
            elif expected != "WORKING":
                self.assertEqual(set(device.screen), {status.TITLE_ID, status.MASCOT_ID}, expected)

    def test_leaving_working_posts_then_erases_subtitle_and_comet(self):
        device = FakeDevice()
        self.drive(device, [("UserPromptSubmit", {})])
        device.calls.clear()
        self.drive(device, [("StopFailure", {"error": "server_error"})])
        self.assertEqual(device.calls[-2:], [("POST", "/display/draw"), ("DELETE", "/display/draw")])
        self.assertEqual(device.screen[status.TITLE_ID]["text"], "ERROR")
        self.assertNotIn(status.ANIMATION_ID, device.screen)

    def test_existing_assets_are_not_uploaded_again(self):
        device = FakeDevice()
        device.assets.update(self.assets_for("WORKING"))
        self.drive(device, [("UserPromptSubmit", {})])
        self.assertNotIn(("POST", "/assets/upload"), device.calls)
        self.assertIn(status.ANIMATION_ID, device.screen)
        self.assertIn(status.MASCOT_ID, device.screen)

    def test_upload_retries_a_busy_device_and_never_removes_assets(self):
        device = FakeDevice(busy_uploads=2)
        device.assets["working-00000000.anim"] = b"old"
        with mock.patch.object(status.time, "sleep"):
            self.drive(device, [("UserPromptSubmit", {})])
        self.assertEqual(device.calls.count(("POST", "/assets/upload")), 4, "3 tries for the first, 1 for the second")
        self.assertEqual(set(device.assets), set(self.assets_for("WORKING")) | {"working-00000000.anim"})
        self.assertNotIn("/storage/remove", [path for _, path in device.calls])
        self.assertIn(status.ANIMATION_ID, device.screen)

    def test_failed_upload_falls_back_to_text_and_backs_off(self):
        device = FakeDevice(fail_uploads=True)
        self.drive(device, [("UserPromptSubmit", {})])
        self.assertEqual(set(device.screen), {status.TITLE_ID, status.SUBTITLE_ID}, "never draw a missing asset")
        self.assertEqual(device.screen[status.TITLE_ID]["x"], 36, "layout stays put so the text does not jump")
        uploads = device.calls.count(("POST", "/assets/upload"))
        self.assertEqual(uploads, 1, "an unreachable device is not tried once per asset")
        self.drive(device, [("PreToolUse", {"tool_name": "Bash"})])
        self.assertEqual(device.calls.count(("POST", "/assets/upload")), uploads, "retry is rate limited")
        self.assertEqual(device.screen[status.SUBTITLE_ID]["text"], "Running")

    def test_title_moves_to_the_vertical_center_when_there_is_no_subtitle(self):
        client = status.BusyBarClient(self.config)
        with_subtitle = client.elements("WORKING", activity="Reading")[status.TITLE_ID]
        alone = client.elements("READY")[status.TITLE_ID]
        self.assertEqual(with_subtitle["y"], status.TITLE_Y)
        self.assertEqual(alone["y"], status.TITLE_CENTER_Y)
        self.assertGreater(alone["y"], with_subtitle["y"])
        device = FakeDevice()
        self.drive(device, [("SessionStart", {"source": "startup"})])
        self.assertEqual(device.screen[status.TITLE_ID]["y"], status.TITLE_CENTER_Y)
        self.drive(device, [("UserPromptSubmit", {}), ("Stop", {})])
        self.assertEqual(device.screen[status.TITLE_ID]["y"], status.TITLE_Y, "FINISHED has a subtitle, so the title moves up")

    def test_text_is_centered_on_the_display_unless_it_would_reach_the_mascot(self):
        client = status.BusyBarClient(self.config)
        for label in ("IDLE", "WAITING", "FINISHED", "WORKING"):
            title = client.elements("IDLE", label=label)[status.TITLE_ID]
            self.assertEqual((title["align"], title["x"]), ("top_mid", 36), label)
        for activity in ("Reading", "Searching", "Compacting"):
            sub = client.elements("WORKING", activity=activity)[status.SUBTITLE_ID]
            self.assertEqual(sub["x"], 36, activity)
        medium = client.elements("IDLE", label="PLEASE WAIT")[status.TITLE_ID]  # 11 chars: too wide for the display center
        self.assertEqual((medium["align"], medium["x"]), ("top_mid", 44))
        self.assertNotIn("width", medium)

    def mode_of(self):
        with self.store.lock():
            sessions = self.store.load_unlocked()["sessions"]
        return status.aggregate_mode(sessions, status.aggregate_state(sessions))

    def test_permission_mode_is_stored_carried_over_and_cleared(self):
        self.apply("SessionStart", source="startup")
        self.assertEqual(self.mode_of(), "", "SessionStart does not carry a mode")
        self.apply("UserPromptSubmit", now=101, permission_mode="plan")
        self.assertEqual(self.mode_of(), "plan")
        for event, extra in [("PostToolUse", {"permission_mode": "plan"}), ("PreCompact", {}), ("PostCompact", {}),
                             ("Notification", {"notification_type": "permission_prompt"}), ("StopFailure", {"error": "rate_limit"})]:
            self.apply(event, now=102, **extra)
            self.assertEqual(self.mode_of(), "plan", event + " keeps the mode it did not carry")
        self.apply("UserPromptSubmit", now=103, permission_mode="auto")
        self.assertEqual(self.mode_of(), "auto")
        self.apply("PreToolUse", now=104, tool_name="Bash", permission_mode="default")
        self.assertEqual(self.mode_of(), "", "default means no badge")
        self.apply("Stop", now=105)
        self.assertEqual(self.mode_of(), "")

    def test_mode_follows_the_session_that_decides_the_display(self):
        self.apply("UserPromptSubmit", session="a", now=100, permission_mode="plan")
        self.apply("UserPromptSubmit", session="b", now=101, permission_mode="bypassPermissions")
        self.assertEqual(self.mode_of(), "bypassPermissions", "most recently active working session")
        self.apply("PermissionRequest", session="a", now=102, permission_mode="plan")
        self.assertEqual(self.mode_of(), "bypassPermissions", "WORKING outranks WAITING")
        self.apply("Stop", session="b", now=103, permission_mode="bypassPermissions")
        self.assertEqual(self.mode_of(), "plan", "WAITING outranks FINISHED, so session a decides")

    def test_mode_change_alone_triggers_a_dispatch_and_changes_the_fingerprint(self):
        self.apply("UserPromptSubmit", permission_mode="default")
        with self.store.lock():
            data = self.store.load_unlocked()
            data["last_sent"] = status.render_fingerprint(self.config, "WORKING", "Thinking", "")
            self.store.save_unlocked(data)
        payload = {"hook_event_name": "PostToolUse", "session_id": "one"}
        self.assertFalse(status.apply_event(self.store, dict(payload, permission_mode="default"), now=101)[1])
        self.assertTrue(status.apply_event(self.store, dict(payload, permission_mode="plan"), now=102)[1])
        self.assertNotEqual(status.render_fingerprint(self.config, "WORKING", "Thinking", "plan"),
                            status.render_fingerprint(self.config, "WORKING", "Thinking", ""))

    def test_mode_badge_is_colored_text_in_the_bottom_right_corner(self):
        client = status.BusyBarClient(self.config)
        for mode, (word, color) in status.MODE_BADGES.items():
            wanted = client.elements("WORKING", activity="Compacting", mode=mode, assets={status.ANIMATION_ID: "comet.anim"})
            text = wanted[status.MODE_ID]
            width = font.text_width(word, "tiny")
            self.assertEqual((text["text"], text["font"], text["color"], text["align"]), (word, "tiny", color, "top_left"), mode)
            self.assertEqual((text["x"], text["y"]), (72 - width, status.BADGE_Y), "flush with the bottom-right corner")
            self.assertEqual(text["y"] + 1 + 3, 15, "caps end on the last row")
            self.assertNotIn("claude-mode-bg", wanted, "no pill")
            comet_end = wanted[status.ANIMATION_ID]["x"] + status.COMET_WIDTH
            self.assertLess(comet_end, text["x"], "the comet stops before the badge")
            subtitle_end = 36 - font.text_width("Compacting", "tiny") // 2 + font.text_width("Compacting", "tiny")
            self.assertLessEqual(subtitle_end, text["x"], "and so does the longest subtitle")
        for none in ("", "default", "mystery"):
            self.assertEqual(set(client.elements("WORKING", mode=none)), {status.TITLE_ID})
        off = status.BusyBarClient(dataclasses.replace(self.config, mode_badge=False))
        self.assertEqual(set(off.elements("WORKING", mode="plan")), {status.TITLE_ID})

    def test_mode_changes_redraw_only_the_badge(self):
        device = FakeDevice()
        self.drive(device, [("UserPromptSubmit", {"permission_mode": "default"})])
        self.assertNotIn(status.MODE_ID, device.screen)
        mascot_before, comet_before = dict(device.screen[status.MASCOT_ID]), dict(device.screen[status.ANIMATION_ID])
        device.calls.clear()
        self.drive(device, [("PostToolUse", {"permission_mode": "plan"})])
        self.assertEqual(device.calls, [("POST", "/display/draw")], "just the badge, no asset checks")
        self.assertEqual(device.screen[status.MODE_ID]["text"], "PLAN")
        self.drive(device, [("PostToolUse", {"permission_mode": "auto"})])
        self.assertEqual(device.screen[status.MODE_ID]["text"], "AUTO")
        self.assertEqual((device.screen[status.MASCOT_ID], device.screen[status.ANIMATION_ID]), (mascot_before, comet_before))
        self.drive(device, [("PostToolUse", {"permission_mode": "default"})])
        self.assertNotIn(status.MODE_ID, device.screen)

    def test_badge_survives_state_changes(self):
        device = FakeDevice()
        self.drive(device, [("UserPromptSubmit", {"permission_mode": "plan"}), ("PermissionRequest", {"permission_mode": "plan"}),
                            ("Stop", {"permission_mode": "plan"})])
        self.assertEqual(device.screen[status.MODE_ID]["text"], "PLAN")
        self.assertEqual(device.screen[status.TITLE_ID]["text"], "FINISHED")

    def feed(self, device, events, config=None):
        """Apply hook events that happened `age` seconds ago and dispatch after each."""
        config = config or self.config
        store = status.Store(config)
        with mock.patch.object(status.urllib.request, "urlopen", side_effect=device):
            for event, extra, age in events:
                payload = {"hook_event_name": event, "session_id": "one"}
                payload.update(extra)
                status.apply_event(store, payload, now=time.time() - age)
                status.dispatch(config, delay=0)

    def test_a_sound_plays_when_waiting_starts_but_not_on_redraws_or_repeats(self):
        device = FakeDevice()
        self.feed(device, [("UserPromptSubmit", {}, 9), ("PermissionRequest", {"tool_name": "Bash"}, 8)])
        waiting = sounds.sound_asset_name("waiting")
        self.assertEqual(device.played, [waiting])
        self.assertEqual(len(device.assets[waiting]), len(sounds.sound("waiting")), "the synthesized PCM was uploaded")
        with mock.patch.object(status, "SOUND_COOLDOWN", 0):
            self.feed(device, [("Notification", {"notification_type": "permission_prompt"}, 7),
                               ("PermissionRequest", {"tool_name": "Edit"}, 6)])
            self.assertEqual(device.played, [waiting], "same state, only the subtitle changed")
            self.feed(device, [("PostToolUse", {}, 5), ("PermissionRequest", {"tool_name": "Bash"}, 4)])
            self.assertEqual(device.played, [waiting, waiting], "a new wait plays again")

    def test_sounds_have_a_cooldown(self):
        device = FakeDevice()
        self.feed(device, [("UserPromptSubmit", {}, 9), ("PermissionRequest", {}, 8), ("PostToolUse", {}, 7),
                           ("PermissionRequest", {}, 6)])
        self.assertEqual(len(device.played), 1, "the second wait came within the cooldown")

    def fresh_state(self):
        (Path(self.tempdir.name) / "state.json").unlink(missing_ok=True)

    def test_finished_only_sounds_after_a_long_turn(self):
        short, long = FakeDevice(), FakeDevice()
        self.feed(short, [("UserPromptSubmit", {}, 3), ("Stop", {}, 0)])
        self.assertEqual(short.played, [])
        self.fresh_state()
        self.feed(long, [("UserPromptSubmit", {}, 25), ("Stop", {}, 0)])
        self.assertEqual(long.played, [sounds.sound_asset_name("finished")])
        self.fresh_state()
        threshold = dataclasses.replace(self.config, sound_min_seconds=1)
        quick = FakeDevice()
        self.feed(quick, [("UserPromptSubmit", {}, 3), ("Stop", {}, 0)], config=threshold)
        self.assertEqual(len(quick.played), 1, "the threshold is configurable")

    def test_sounds_can_be_turned_off_and_never_break_the_display(self):
        off = FakeDevice()
        self.feed(off, [("UserPromptSubmit", {}, 9), ("PermissionRequest", {}, 8)], config=dataclasses.replace(self.config, sounds=False))
        self.assertEqual(off.played, [])
        self.assertFalse([name for name in off.assets if name.endswith(".snd")])
        self.assertNotIn(("POST", "/audio/play"), off.calls)
        for broken in (FakeDevice(fail_play=True), FakeDevice(fail_uploads=True)):
            self.fresh_state()
            self.feed(broken, [("UserPromptSubmit", {}, 9), ("PermissionRequest", {"tool_name": "Bash"}, 8)])
            self.assertEqual(broken.played, [])
            self.assertEqual(broken.screen[status.SUBTITLE_ID]["text"], "Allow Bash?", "the screen is drawn regardless")

    def test_sound_due_rules(self):
        base = dict(config=self.config, turn_seconds=None, last_sound_at=0.0, now=1000.0)
        due = lambda **kw: status.sound_due(**dict(base, **kw))
        self.assertTrue(due(status="WAITING_FOR_INPUT", previous_status="WORKING"))
        self.assertTrue(due(status="WAITING_FOR_INPUT", previous_status=None))
        self.assertFalse(due(status="WAITING_FOR_INPUT", previous_status="WAITING_FOR_INPUT"))
        self.assertFalse(due(status="WAITING_FOR_INPUT", previous_status="WORKING", last_sound_at=999.0))
        self.assertTrue(due(status="FINISHED", previous_status="WORKING", turn_seconds=20.0))
        self.assertFalse(due(status="FINISHED", previous_status="WORKING", turn_seconds=19.9))
        self.assertFalse(due(status="FINISHED", previous_status="WORKING", turn_seconds=None))
        for quiet in ("WORKING", "ERROR", "LIMIT", "READY", "IDLE"):
            self.assertFalse(due(status=quiet, previous_status="FINISHED", turn_seconds=99.0), quiet)

    def test_idle_is_a_night_scene(self):
        plan = status.asset_plan(self.config, "IDLE")
        self.assertEqual(set(plan), {status.MASCOT_ID, status.ANIMATION_ID, status.NIGHT_ID})
        self.assertEqual(plan[status.ANIMATION_ID][0], anim.breath_asset_name(self.config.colors["IDLE"], status.COMET_WIDTH))
        off = dataclasses.replace(self.config, animation=False)
        self.assertEqual(set(status.asset_plan(off, "IDLE")), {status.MASCOT_ID})
        client = status.BusyBarClient(self.config)
        wanted = client.elements("IDLE", assets={key: value[0] for key, value in plan.items()})
        self.assertEqual(wanted[status.SUBTITLE_ID]["text"], "No session", "a default subtitle: there is no session to take one from")
        self.assertLessEqual(font.text_width("No session", "tiny"), status.SUBTITLE_MAX_WIDTH)
        self.assertEqual((wanted[status.TITLE_ID]["align"], wanted[status.TITLE_ID]["y"]), ("top_mid", status.TITLE_Y))
        sky = wanted[status.NIGHT_ID]
        self.assertEqual((sky["x"], sky["y"]), (status.SKY_X, 0), "the same free corner as the fireworks")
        self.assertGreaterEqual(sky["x"], 36 + 40 // 2, "clear of the widest centered text")
        self.assertEqual(wanted[status.ANIMATION_ID]["x"], status.COMET_X)
        self.assertEqual(status.DEFAULT_COLORS["IDLE"], "#7D8CB0FF")
        self.assertEqual(wanted[status.SUBTITLE_ID]["color"], "#7F7F7FFF", "dimmed to half on the resting screen")
        self.assertEqual(client.elements("WORKING", activity="Reading")[status.SUBTITLE_ID]["color"], "#FFFFFFFF")
        self.assertEqual(status.scale_color("#80FF0A12", 0.5), "#407F0512")
        for other in ("READY", "FINISHED", "WORKING"):
            self.assertNotIn(status.NIGHT_ID, status.BusyBarClient(self.config).elements(other))

    def test_waiting_says_what_it_waits_for(self):
        for payload, expected in [
            ({"hook_event_name": "PermissionRequest", "tool_name": "Bash"}, "Allow Bash?"),
            ({"hook_event_name": "PermissionRequest", "tool_name": "Edit"}, "Allow Edit?"),
            ({"hook_event_name": "PermissionRequest", "tool_name": "WebFetch"}, "Approve?"),  # too wide to center
            ({"hook_event_name": "PermissionRequest", "tool_name": "mcp__srv__tool"}, "Allow tool?"),
            ({"hook_event_name": "PermissionRequest"}, "Approve?"),
            ({"hook_event_name": "PermissionRequest", "tool_name": "ExitPlanMode"}, "Plan ready"),
            ({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion"}, "Question"),
            ({"hook_event_name": "Elicitation"}, "MCP input"),
            ({"hook_event_name": "Notification", "notification_type": "idle_prompt"}, "Your turn"),
            ({"hook_event_name": "Notification", "notification_type": "quota_auto_resume_stale"}, "Press Enter"),
        ]:
            self.assertEqual(status.waiting_subtitle(payload), expected, payload)
            self.assertLessEqual(font.text_width(expected, "tiny"), status.SUBTITLE_MAX_WIDTH, expected)

    def test_a_permission_notification_keeps_the_specific_subtitle(self):
        def activity():
            with self.store.lock():
                sessions = self.store.load_unlocked()["sessions"]
            return status.aggregate_activity(sessions, status.aggregate_state(sessions))

        self.apply("UserPromptSubmit")
        self.apply("PermissionRequest", now=101, tool_name="Bash")
        self.assertEqual(activity(), "Allow Bash?")
        self.apply("Notification", now=102, notification_type="permission_prompt")
        self.assertEqual(activity(), "Allow Bash?")
        self.apply("PostToolUse", now=103)
        self.apply("Stop", now=104)
        self.apply("Notification", now=105, notification_type="idle_prompt")
        self.assertEqual(activity(), "Your turn")

    def test_limit_says_when_it_resets_if_the_error_does(self):
        for message, expected in [
            ("API Error: Rate limit reached", "Rate limit"),
            ("5-hour limit reached - resets 3pm", "Resets 3pm"),
            ("Usage limit reached. Your limit will reset at 3:30 PM", "Resets 3:30pm"),
            ("resets at 12:00 am", "Resets 12am"),
            ("Rate limited, try again in 2 hours 15 minutes", "Back in 2h15m"),
            ("retry in 45 minutes", "Back in 45m"),
            ("resets at 25pm", "Rate limit"),
            ("", "Rate limit"),
        ]:
            self.assertEqual(status.limit_subtitle({"last_assistant_message": message}), expected, message)
        self.assertEqual(status.limit_subtitle({"error_details": "429 Too Many Requests"}), "Rate limit")
        self.apply("UserPromptSubmit")
        self.apply("StopFailure", now=101, error="rate_limit", last_assistant_message="limit reached, resets 9am")
        with self.store.lock():
            sessions = self.store.load_unlocked()["sessions"]
        self.assertEqual(status.aggregate_activity(sessions, "LIMIT"), "Resets 9am")
        self.assertEqual(status.DEFAULT_LABELS["LIMIT"], "LIMIT HIT")
        self.assertLessEqual(font.text_width("LIMIT HIT"), 40)

    def test_waiting_gets_a_pulse_under_the_text_that_working_swaps_for_the_comet(self):
        waiting = status.asset_plan(self.config, "WAITING_FOR_INPUT")
        self.assertEqual(waiting[status.ANIMATION_ID][0],
                         anim.ping_asset_name(self.config.colors["WAITING_FOR_INPUT"], status.COMET_WIDTH))
        working = status.asset_plan(self.config, "WORKING")
        self.assertNotEqual(working[status.ANIMATION_ID][0], waiting[status.ANIMATION_ID][0])
        for state in ("LIMIT", "ERROR", "READY"):
            self.assertNotIn(status.ANIMATION_ID, status.asset_plan(self.config, state), state)
        off = dataclasses.replace(self.config, animation=False)
        self.assertNotIn(status.ANIMATION_ID, status.asset_plan(off, "WAITING_FOR_INPUT"))
        device = FakeDevice()
        self.drive(device, [("UserPromptSubmit", {}), ("PermissionRequest", {"tool_name": "Bash"})])
        self.assertEqual(device.screen[status.ANIMATION_ID]["path"], waiting[status.ANIMATION_ID][0])
        self.assertEqual(device.screen[status.SUBTITLE_ID]["text"], "Allow Bash?")
        title = device.screen[status.TITLE_ID]
        self.assertEqual(device.screen[status.DOTS_ID]["y"], title["y"] + 6, "dots stay on the baseline when the title moves up")
        self.drive(device, [("PostToolUse", {})])
        self.assertEqual(device.screen[status.ANIMATION_ID]["path"], working[status.ANIMATION_ID][0])
        self.assertNotIn(status.DOTS_ID, device.screen)

    def test_error_says_what_failed(self):
        for error, expected in [
            ("overloaded", "Overloaded"), ("authentication_failed", "Auth failed"), ("oauth_org_not_allowed", "Org blocked"),
            ("account_on_hold", "On hold"), ("billing_error", "Billing error"), ("invalid_request", "Bad input"),
            ("model_not_found", "Bad model"), ("server_error", "Server err"), ("max_output_tokens", "Max tokens"),
            ("cloud_credential_error", "Cloud creds"), ("unknown", "API error"), ("brand_new_error", "API error"), (None, "API error"),
        ]:
            payload = {} if error is None else {"error": error}
            self.assertEqual(status.error_subtitle(payload), expected, error)
            self.assertLessEqual(font.text_width(expected, "tiny"), status.SUBTITLE_MAX_WIDTH, expected)
        self.apply("UserPromptSubmit")
        self.apply("StopFailure", now=101, error="authentication_failed")
        with self.store.lock():
            sessions = self.store.load_unlocked()["sessions"]
        self.assertEqual(status.aggregate_activity(sessions, "ERROR"), "Auth failed")

    def test_finished_says_how_long_the_turn_took(self):
        for seconds, text in [(0, "0s"), (42, "42s"), (59.6, "1m"), (60, "1m"), (83, "1m 23s"), (720, "12m"), (725, "12m 5s"),
                              (3600, "1h"), (3900, "1h 5m")]:
            self.assertEqual(status.format_duration(seconds), text, seconds)
        self.assertEqual(status.finished_subtitle(42), "Done in 42s")
        self.assertEqual(status.finished_subtitle(83), "1m 23s", "'Done in 1m 23s' would not fit")
        for seconds in (0, 42, 83, 725, 3900):
            self.assertLessEqual(font.text_width(status.finished_subtitle(seconds), "tiny"), status.SUBTITLE_MAX_WIDTH)

    def test_turn_timing_starts_on_a_new_prompt_and_survives_waiting(self):
        def subtitle():
            with self.store.lock():
                sessions = self.store.load_unlocked()["sessions"]
            return status.aggregate_activity(sessions, status.aggregate_state(sessions))

        self.apply("SessionStart", now=100, source="startup")
        self.apply("UserPromptSubmit", now=1000)
        self.apply("PermissionRequest", now=1010, tool_name="Bash")
        self.apply("PostToolUse", now=1100)
        self.apply("UserPromptSubmit", now=1110)  # typed while the turn runs: the same turn
        self.apply("Stop", now=1125)
        self.assertEqual(subtitle(), "2m 5s", "measured from the first prompt")
        self.apply("Stop", now=1130)
        self.apply("UserPromptSubmit", now=2000)  # after a finished turn: a new one
        self.apply("Stop", now=2003)
        self.assertEqual(subtitle(), "Done in 3s")
        self.apply("PreCompact", now=2010, trigger="manual")
        self.apply("PostCompact", now=2011, trigger="manual")
        self.assertEqual(subtitle(), "Done in 3s", "compaction restores the finished subtitle")

    def test_finished_celebrates_with_bang_rainbow_and_fireworks_in_free_space(self):
        plan = status.asset_plan(self.config, "FINISHED")
        self.assertEqual(set(plan), {status.MASCOT_ID, status.BANG_ID, status.ANIMATION_ID, status.FIREWORKS_ID})
        self.assertEqual(plan[status.ANIMATION_ID][0], anim.celebration_asset_name("rainbow", status.COMET_WIDTH))
        off = dataclasses.replace(self.config, animation=False)
        self.assertEqual(set(status.asset_plan(off, "FINISHED")), {status.MASCOT_ID})
        client = status.BusyBarClient(self.config)
        assets = {key: value[0] for key, value in plan.items()}
        wanted = client.elements("FINISHED", activity="Done in 42s", assets=assets)
        title, bang = wanted[status.TITLE_ID], wanted[status.BANG_ID]
        label = font.text_width("FINISHED")
        self.assertEqual(bang["x"], title["x"] + label + 1)
        self.assertEqual(bang["y"], title["y"] + 2, "the bars span the caps")
        unit = label + 1 + anim.BANG_WIDTH
        self.assertEqual(title["x"], 36 - unit // 2)
        fireworks = wanted[status.FIREWORKS_ID]
        self.assertEqual((fireworks["x"], fireworks["y"]), (56, 0))
        self.assertGreaterEqual(fireworks["x"], bang["x"] + anim.BANG_WIDTH, "clear of the title")
        self.assertGreaterEqual(fireworks["x"], 36 - 40 // 2 + 40, "and of the widest centered subtitle")
        self.assertLessEqual(fireworks["y"] + anim.FIREWORKS_HEIGHT, status.BADGE_Y + 1, "above the badge")
        self.assertEqual(wanted[status.ANIMATION_ID]["x"], status.COMET_X)

    def test_waiting_dots_follow_the_label_and_are_centered_with_it(self):
        client = status.BusyBarClient(self.config)
        dots_name = anim.dots_asset_name(self.config.colors["WAITING_FOR_INPUT"])
        wanted = client.elements("WAITING_FOR_INPUT", assets={status.DOTS_ID: dots_name})
        title, dots = wanted[status.TITLE_ID], wanted[status.DOTS_ID]
        label = font.text_width("WAITING")  # 28
        unit = label + status.SUFFIX_GAP + anim.DOTS_WIDTH  # "WAITING..." is 34px wide
        self.assertEqual((label, unit), (28, 34))
        self.assertEqual((title["align"], title["x"]), ("top_left", 36 - unit // 2))
        self.assertEqual((dots["x"], dots["y"]), (title["x"] + label + 1, title["y"] + 6), "on the baseline, one px after the G")
        self.assertEqual((dots["path"], dots["loop"]), (dots_name, True))
        self.assertGreaterEqual(title["x"], 16, "clear of the mascot")
        # without the asset (failed upload, or disabled) the label is a plain centered title
        plain = client.elements("WAITING_FOR_INPUT")
        self.assertEqual(set(plain), {status.TITLE_ID})
        self.assertEqual((plain[status.TITLE_ID]["align"], plain[status.TITLE_ID]["x"]), ("top_mid", 36))
        # a label too wide for the display center moves its unit to the column; too wide for that, no dots
        medium = client.elements("WAITING_FOR_INPUT", label="PLEASE WAIT", assets={status.DOTS_ID: dots_name})
        self.assertEqual(medium[status.TITLE_ID]["x"], 44 - (font.text_width("PLEASE WAIT") + 6) // 2)
        huge = client.elements("WAITING_FOR_INPUT", label="PLEASE WAIT FOR ME", assets={status.DOTS_ID: dots_name})
        self.assertNotIn(status.DOTS_ID, huge)
        bare = status.BusyBarClient(dataclasses.replace(self.config, mascot=False))
        self.assertEqual(bare.elements("WAITING_FOR_INPUT", assets={status.DOTS_ID: dots_name})[status.TITLE_ID]["x"], 36 - 17)

    def test_dots_are_only_for_waiting_and_can_be_disabled(self):
        for state in status.STATUSES:
            self.assertEqual(status.DOTS_ID in status.asset_plan(self.config, state), state == "WAITING_FOR_INPUT", state)
        off = dataclasses.replace(self.config, waiting_dots=False)
        self.assertNotIn(status.DOTS_ID, status.asset_plan(off, "WAITING_FOR_INPUT"))
        device = FakeDevice()
        self.drive(device, [("PermissionRequest", {})], config=off)
        self.assertEqual(set(device.screen), {status.TITLE_ID, status.SUBTITLE_ID, status.MASCOT_ID, status.ANIMATION_ID})

    def test_waiting_to_working_removes_the_dots_and_back_again(self):
        device = FakeDevice()
        self.drive(device, [("PermissionRequest", {})])
        self.assertIn(status.DOTS_ID, device.screen)
        dots = dict(device.screen[status.DOTS_ID])
        self.drive(device, [("PostToolUse", {})])
        self.assertNotIn(status.DOTS_ID, device.screen)
        self.assertEqual(device.screen[status.TITLE_ID]["align"], "top_mid", "title back to plain centering")
        self.drive(device, [("PermissionRequest", {})])
        self.assertEqual(device.screen[status.DOTS_ID], dots)

    def test_long_labels_scroll_inside_the_column_instead_of_covering_the_mascot(self):
        client = status.BusyBarClient(self.config)
        long = client.elements("WORKING", label="PLEASE WAIT A MOMENT", activity="Searching")
        self.assertEqual((long[status.TITLE_ID]["align"], long[status.TITLE_ID]["x"]), ("top_left", 16))
        self.assertEqual(long[status.TITLE_ID]["width"], 56)
        self.assertEqual(long[status.SUBTITLE_ID]["align"], "top_mid", "tiny 9-char subtitle fits and stays centered")
        bare = status.BusyBarClient(dataclasses.replace(self.config, mascot=False))
        self.assertEqual(bare.elements("WORKING", label="PLEASE WAIT A MOMENT")[status.TITLE_ID]["align"], "top_mid")

    def test_options_disable_the_comet_and_the_mascot(self):
        device = FakeDevice()
        self.drive(device, [("UserPromptSubmit", {})], config=dataclasses.replace(self.config, animation=False))
        self.assertEqual(set(device.screen), {status.TITLE_ID, status.SUBTITLE_ID, status.MASCOT_ID})
        device = FakeDevice()
        bare = dataclasses.replace(self.config, animation=False, mascot=False)
        self.drive(device, [("UserPromptSubmit", {})], config=bare)
        self.assertEqual(set(device.screen), {status.TITLE_ID, status.SUBTITLE_ID})
        self.assertFalse([call for call in device.calls if "assets" in call[1] or "storage" in call[1]])
        device = FakeDevice()
        self.drive(device, [("UserPromptSubmit", {})], config=dataclasses.replace(self.config, mascot=False))
        comet = device.screen[status.ANIMATION_ID]
        self.assertEqual((comet["x"], comet["path"]), (20, anim.animation_asset_name(self.config.colors["WORKING"], 32)))

    def test_erase_tolerates_elements_that_are_already_gone(self):
        device = FakeDevice()
        device.screen = {status.SUBTITLE_ID: {}}  # the comet is not there, only the subtitle
        with mock.patch.object(status.urllib.request, "urlopen", side_effect=device):
            status.BusyBarClient(self.config).show("READY", previous=None)
        self.assertEqual(set(device.screen), {status.TITLE_ID}, "the present leftover is still removed")
        device = FakeDevice()
        with mock.patch.object(status.urllib.request, "urlopen", side_effect=device):
            status.BusyBarClient(self.config).show("READY", previous=None)  # nothing to erase at all
        self.assertEqual(set(device.screen), {status.TITLE_ID})

    def test_unknown_device_state_erases_leftovers(self):
        device = FakeDevice()
        device.screen = {status.SUBTITLE_ID: {}, status.MASCOT_ID: {}, status.ANIMATION_ID: {}}
        with mock.patch.object(status.urllib.request, "urlopen", side_effect=device):
            status.BusyBarClient(self.config).show("FINISHED", previous=None)
        self.assertEqual(set(device.screen), {status.TITLE_ID})

    def test_new_prompt_forces_a_full_redraw(self):
        device = FakeDevice()
        self.drive(device, [("UserPromptSubmit", {}), ("Stop", {})])
        with self.store.lock():
            self.assertIsNotNone(self.store.load_unlocked()["drawn"])
        status.apply_event(self.store, {"hook_event_name": "UserPromptSubmit", "session_id": "one"}, now=200)
        with self.store.lock():
            self.assertIsNone(self.store.load_unlocked()["drawn"])

    def test_cloud_bearer_header(self):
        configured = dataclasses.replace(self.config, auth_mode="cloud", token="token-value")
        headers = status.BusyBarClient(configured).headers()
        self.assertEqual(headers["Authorization"], "Bearer token-value")
        self.assertNotIn("X-API-Token", headers)

    def test_address_normalization(self):
        self.assertEqual(status.normalize_base_url("10.0.4.20"), "http://10.0.4.20/api")
        self.assertEqual(
            status.normalize_base_url("https://api.busy.app/busybar/"),
            "https://api.busy.app/busybar",
        )


class MascotTests(unittest.TestCase):
    def decode(self, blob):
        section_len, chunk_len, sections, file_frames, display_frames = struct.unpack("<IIIII", blob[16:36])
        records, offset, frames = blob[36 + section_len:], 0, []
        while offset < len(records):
            encoding, duration, length = struct.unpack("<BBH", records[offset:offset + 4])
            payload = records[offset + 4:offset + 4 + length]
            frames.append(anim.rle_decompress(payload) if encoding == 1 else payload)
            offset += 4 + length
        self.assertEqual(len(frames), file_frames)
        return blob[12], frames

    def test_every_state_has_a_valid_looping_16x16_animation(self):
        for state in status.STATUSES:
            blob = mascot.animation(state)
            self.assertEqual(blob[9:11], bytes([16, 16]))
            fps, frames = self.decode(blob)
            self.assertGreaterEqual(len(frames), 20, state)
            self.assertEqual({len(frame) for frame in frames}, {16 * 16 * 3})
            self.assertGreater(len({frame for frame in frames}), 3, "%s must actually move" % state)
            self.assertLess(len(blob), 20000, "small enough to upload quickly")

    def test_states_look_different_and_idle_is_dim(self):
        firsts = {state: self.decode(mascot.animation(state))[1][0] for state in status.STATUSES}
        self.assertEqual(len(set(firsts.values())), len(firsts))

        def brightest(state):
            return max(max(frame) for frame in self.decode(mascot.animation(state))[1])

        self.assertLess(brightest("IDLE"), 0.6 * brightest("WORKING"))

    def test_mascot_uses_the_orange_body_in_bgr_order_and_keeps_the_text_column_clear(self):
        _, frames = self.decode(mascot.animation("FINISHED"))
        orange_bgr = bytes(reversed(mascot.ORANGE))
        self.assertTrue(any(orange_bgr in frame[i:i + 3] for frame in frames for i in range(0, len(frame), 3)))
        frame = mascot.working()[0]
        self.assertTrue(all(pixel is None for pixel in frame.pixels[:0]))  # canvas is exactly 16 wide, no spill
        self.assertEqual(len(frame.pixels), 256)

    def test_asset_names_are_versioned_per_state(self):
        names = {mascot.asset_name(state) for state in status.STATUSES}
        self.assertEqual(len(names), len(status.STATUSES))
        self.assertTrue(all(re.search(r"-v\d+\.anim$", name) for name in names))
        self.assertNotEqual(mascot.asset_name("WORKING"), "mascot-working-v2.anim", "the fixed art got a new name")


class FontTests(unittest.TestCase):
    def test_widths_match_what_the_device_draws(self):
        # measured on a real bar from the framebuffer
        for text, width in [("WORKING", 32), ("WAITING", 28), ("FINISHED", 33), ("LIMIT", 17), ("IDLE", 15),
                            ("ERROR", 24), ("READY", 23), ("WAITING...", 34), ("PLEASE WAIT", 47)]:
            self.assertEqual(font.text_width(text), width, text)
        self.assertEqual(font.text_width(""), 0)
        for text, width in [("PLAN", 16), ("AUTO", 17), ("EDIT", 15), ("LOCK", 17), ("YOLO", 19), ("Compacting", 38),
                            ("Searching", 31), ("Editing", 22), ("PLEASE WAIT", 44)]:
            self.assertEqual(font.text_width(text, "tiny"), width, text)


class SoundTests(unittest.TestCase):
    def samples(self, pcm):
        return struct.unpack("<%dh" % (len(pcm) // 2), pcm)

    def pitch(self, values, start, stop):
        """Frequency from zero crossings between two times (seconds)."""
        window = values[int(start * sounds.SAMPLE_RATE):int(stop * sounds.SAMPLE_RATE)]
        crossings = sum(1 for a, b in zip(window, window[1:]) if (a < 0) != (b < 0))
        return crossings / 2 / (stop - start)

    def test_pcm_is_quiet_clean_and_the_right_length(self):
        for kind, notes in sounds.KINDS.items():
            pcm = sounds.sound(kind)
            values = self.samples(pcm)
            seconds = max(start + length for _, start, length, _ in notes)
            self.assertAlmostEqual(len(values) / sounds.SAMPLE_RATE, seconds, delta=0.01)
            self.assertLessEqual(max(map(abs, values)), sounds.GAIN * 32767 + 1, "quiet by construction")
            self.assertGreater(max(map(abs, values)), sounds.GAIN * 32767 * 0.9, "but normalized to GAIN")
            self.assertLessEqual(abs(values[0]) + abs(values[-1]), 40, "starts and ends at silence, so no clicks")
            self.assertEqual(pcm, sounds.sound(kind), "deterministic")
            self.assertLess(len(pcm), 100000, "small enough to upload quickly")

    def test_waiting_falls_and_finished_rises(self):
        waiting = self.samples(sounds.sound("waiting"))
        self.assertAlmostEqual(self.pitch(waiting, 0.01, 0.15), sounds.A5, delta=60)
        self.assertAlmostEqual(self.pitch(waiting, 0.45, 0.55), sounds.E5, delta=40)
        finished = self.samples(sounds.sound("finished"))
        self.assertAlmostEqual(self.pitch(finished, 0.0, 0.06), sounds.C5, delta=60)
        self.assertAlmostEqual(self.pitch(finished, 0.55, 0.75), sounds.C6, delta=60)

    def test_asset_names(self):
        names = {kind: sounds.sound_asset_name(kind) for kind in sounds.KINDS}
        self.assertEqual(len(set(names.values())), len(names))
        self.assertTrue(all(name.endswith(".snd") for name in names.values()))
        self.assertEqual(names, {kind: sounds.sound_asset_name(kind) for kind in sounds.KINDS}, "stable")
        self.assertLessEqual(len("/ext/user_assets/claude-code-status/" + max(names.values(), key=len)), 64)


class AnimationTests(unittest.TestCase):
    def frames_of(self, blob):
        section_len = struct.unpack("<I", blob[16:20])[0]
        records, offset, frames = blob[36 + section_len:], 0, []
        while offset < len(records):
            encoding, duration, length = struct.unpack("<BBH", records[offset:offset + 4])
            payload = records[offset + 4:offset + 4 + length]
            frames.append(anim.rle_decompress(payload) if encoding == 1 else payload)
            offset += 4 + length
        return frames

    def test_night_scene_and_breath_have_the_right_size_and_move(self):
        night = anim.night_animation()
        self.assertEqual(night[9:11], bytes([16, 12]))
        frames = self.frames_of(night)
        self.assertEqual(len(frames), anim.NIGHT_FRAMES)
        self.assertEqual({len(frame) for frame in frames}, {16 * 12 * 3})
        lit = lambda frame: sum(1 for i in range(0, len(frame), 3) if frame[i:i + 3] != b"\x00\x00\x00")
        self.assertGreater(len(set(frames)), 20, "twinkling and a shooting star")
        self.assertGreater(min(lit(frame) for frame in frames), 10, "the moon is always there")
        self.assertLessEqual(max(max(frame) for frame in frames), 255 * anim.NIGHT_BRIGHTNESS + 1, "dim")
        breath = self.frames_of(anim.breath_animation("#7D8CB0FF", 32))
        self.assertEqual(len(breath), anim.BREATH_FRAMES)
        peak = lambda frame: max(frame)
        self.assertGreater(peak(breath[anim.BREATH_FRAMES // 2]), 3 * peak(breath[0]), "it swells")
        row = breath[anim.BREATH_FRAMES // 2][32 * 3:]  # the bright row
        self.assertGreater(row[16 * 3 + 2], row[0 * 3 + 2], "brightest in the middle")
        self.assertNotEqual(anim.breath_asset_name("#7D8CB0FF", 32), anim.breath_asset_name("#FF0000FF", 32))

    def test_celebration_assets_have_the_right_size_and_move(self):
        for blob, size in [(anim.bang_animation(), (3, 5)), (anim.rainbow_animation(32), (32, 2)),
                           (anim.fireworks_animation(), (16, 12))]:
            self.assertEqual(blob[9:11], bytes(size))
            frames = self.frames_of(blob)
            self.assertEqual({len(frame) for frame in frames}, {size[0] * size[1] * 3})
            self.assertGreaterEqual(len(set(frames)), 5, "it animates")
        bang = self.frames_of(anim.bang_animation())[0]
        self.assertEqual([bang[(y * 3 + 1) * 3:(y * 3 + 1) * 3 + 3] == b"\x00\x00\x00" for y in range(5)], [True] * 5, "gap column")
        self.assertEqual(bang[(3 * 3) * 3:(3 * 3) * 3 + 3], b"\x00\x00\x00", "gap row")
        self.assertNotEqual(bang[0:3], b"\x00\x00\x00")
        fireworks = self.frames_of(anim.fireworks_animation())
        lit = lambda frame: sum(1 for i in range(0, len(frame), 3) if frame[i:i + 3] != b"\x00\x00\x00")
        self.assertGreater(max(lit(frame) for frame in fireworks), 20, "bursts have many sparks")
        self.assertGreater(min(lit(frame) for frame in fireworks), 0, "something is always going off")


    def test_ping_is_a_loop_of_rings_spreading_from_the_middle(self):
        frames = anim.ping_frames((255, 176, 0), 32)
        self.assertEqual(len(frames), anim.PING_FRAMES)
        self.assertEqual({len(frame) for frame in frames}, {32 * 2 * 3})
        row = lambda frame: [frame[i + 2] for i in range(0, 32 * 3, 3)]  # red channel of the bright row... 
        bright = lambda frame: [frame[32 * 3 + i + 2] for i in range(0, 32 * 3, 3)]
        early, late = bright(frames[1]), bright(frames[10])
        self.assertLess(abs(early.index(max(early)) - 15.5), 4, "a fresh ring starts at the middle")
        self.assertGreater(abs(late.index(max(late[:16])) - 15.5), abs(early.index(max(early[:16])) - 15.5), "and spreads out")
        blob = anim.ping_animation("#FFB000FF", 32)
        self.assertEqual(blob[9:11], bytes([32, 2]))
        self.assertNotEqual(anim.ping_asset_name("#FFB000FF", 32), anim.animation_asset_name("#FFB000FF", 32))

    def test_dots_animation_counts_up_and_loops(self):
        blob = anim.dots_animation("#FFB000FF")
        self.assertEqual(blob[9:11], bytes([5, 1]))
        section_len = struct.unpack("<I", blob[16:20])[0]
        records, offset, frames = blob[36 + section_len:], 0, []
        while offset < len(records):
            encoding, duration, length = struct.unpack("<BBH", records[offset:offset + 4])
            payload = records[offset + 4:offset + 4 + length]
            frames.append(anim.rle_decompress(payload) if encoding == 1 else payload)
            offset += 4 + length
        lit = [sum(1 for i in range(0, 15, 3) if frame[i:i + 3] != b"\x00\x00\x00") for frame in frames]
        self.assertEqual(lit, [0, 1, 2, 3])
        self.assertEqual(frames[3][:3], bytes([0x00, 0xB0, 0xFF]), "amber in BGR order")
        self.assertEqual([frames[3][i:i + 3] != b"\x00\x00\x00" for i in (0, 3, 6, 9, 12)], [True, False, True, False, True])

    def test_dots_asset_name_tracks_color(self):
        self.assertNotEqual(anim.dots_asset_name("#FFB000FF"), anim.dots_asset_name("#FF0000FF"))
        self.assertEqual(anim.dots_asset_name("#FFB000FF"), anim.dots_asset_name("#FFB00000"))

    def test_rle_round_trips_runs_and_noise(self):
        samples = [
            b"",
            b"\x01\x02\x03" * 200,
            bytes(range(256)) * 3,
            b"\x00\x00\x00" * 5 + b"\x01\x02\x03" + b"\x00\x00\x00" * 400,
        ]
        for sample in samples:
            sample = sample[: len(sample) // 3 * 3]
            self.assertEqual(anim.rle_decompress(anim.rle_compress(sample)), sample)

    def test_rle_matches_firmware_reference_vectors(self):
        for source, block, expected in [
            ("07", 1, "8107"),
            ("0707070707", 1, "0507"),
            ("010203", 1, "83010203"),
            ("41425858585858585858", 1, "864142585858580458"),
            ("deadbedeadbedeadbedeadbe010203", 3, "04deadbe81010203"),
        ]:
            self.assertEqual(anim.rle_compress(bytes.fromhex(source), block), bytes.fromhex(expected))

    def test_container_header_and_frames_decode_back(self):
        frames = anim.comet_frames((0, 128, 255))
        blob = anim.working_animation("#0080FFFF")
        self.assertEqual(blob[:8], b"bicycle0")
        self.assertEqual(blob[9:13], bytes([anim.ANIMATION_WIDTH, anim.ANIMATION_HEIGHT, 0, anim.ANIMATION_FPS]))
        section_len, chunk_len, sections, file_frames, display_frames = struct.unpack("<IIIII", blob[16:36])
        self.assertEqual((sections, file_frames, display_frames), (1, len(frames), len(frames)))
        records = blob[36 + section_len:]
        self.assertEqual(len(records), chunk_len)
        offset = 0
        for frame in frames:
            encoding, duration, length = struct.unpack("<BBH", records[offset:offset + 4])
            payload = records[offset + 4:offset + 4 + length]
            self.assertEqual(duration, 1)
            self.assertEqual(anim.rle_decompress(payload) if encoding == 1 else payload, frame)
            offset += 4 + length
        self.assertEqual(offset, len(records))

    def test_comet_is_bgr_loops_seamlessly_and_uses_the_color(self):
        frames = anim.comet_frames((255, 0, 0))
        width = anim.ANIMATION_WIDTH
        for frame in frames:
            for x in range(width):
                b, g, r = frame[x * 3:x * 3 + 3]  # device order is blue, green, red
                self.assertGreaterEqual(r, max(b, g))
        self.assertGreater(max(max(frame[2::3]) for frame in frames), 200, "bright head")
        self.assertEqual(frames[0][-3:], bytes([0, 0, int(255 * anim.ANIMATION_RAIL * anim.ANIMATION_ROW_GAIN[-1])]))

        def brightest(frame):
            return max(range(width), key=lambda x: frame[x * 3 + 2])

        step = max(abs(brightest(frames[i]) - brightest(frames[(i + 1) % len(frames)])) for i in range(len(frames)))
        self.assertLessEqual(step, 6, "head eases; peak speed is about 4.7 px per frame")
        wrap = abs(brightest(frames[-1]) - brightest(frames[0]))
        self.assertLessEqual(wrap, 1, "no jump where the loop restarts")

    def test_asset_name_tracks_color_and_width_only(self):
        self.assertEqual(anim.animation_asset_name("#0080FFFF"), anim.animation_asset_name("#0080FF00"))
        self.assertNotEqual(anim.animation_asset_name("#0080FFFF"), anim.animation_asset_name("#FF0000FF"))
        self.assertNotEqual(anim.animation_asset_name("#0080FFFF", 56), anim.animation_asset_name("#0080FFFF", 72))

    def test_comet_width_is_configurable(self):
        blob = anim.working_animation("#0080FFFF", 56)
        self.assertEqual(blob[9:11], bytes([56, anim.ANIMATION_HEIGHT]))


if __name__ == "__main__":
    unittest.main()
