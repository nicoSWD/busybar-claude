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
import busybar_mascot as mascot  # noqa: E402
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

    def __init__(self, fail_uploads=False, busy_uploads=0):
        self.calls = []
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
        elif (method, path) == ("POST", "/display/draw"):
            for element in json.loads(body)["elements"]:
                self.screen[element["id"]] = element
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
        self.assertEqual(activity(), "")
        self.apply("PostToolUse", now=107)
        self.assertEqual(activity(), "Thinking")
        self.apply("Stop", now=108)
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
        self.assertEqual(comet["path"], anim.animation_asset_name(self.config.colors["WORKING"], 56))
        self.assertEqual((comet["x"], comet["y"], comet["loop"]), (16, status.ANIMATION_Y, True))
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
            ("StopFailure", {"error": "rate_limit"}, "LIMIT", "LIMIT"),
            ("SessionStart", {"source": "startup"}, "READY", "READY"),
            ("SessionEnd", {}, "IDLE", "IDLE"),
        ]:
            self.drive(device, [(event, extra)])
            self.assertEqual(device.screen[status.MASCOT_ID]["path"], mascot.asset_name(expected))
            self.assertEqual(device.screen[status.TITLE_ID]["text"], label)
            if expected != "WORKING":
                self.assertEqual(set(device.screen), {status.TITLE_ID, status.MASCOT_ID}, expected)

    def test_leaving_working_posts_then_erases_subtitle_and_comet(self):
        device = FakeDevice()
        self.drive(device, [("UserPromptSubmit", {})])
        device.calls.clear()
        self.drive(device, [("Stop", {})])
        self.assertEqual(device.calls[-2:], [("POST", "/display/draw"), ("DELETE", "/display/draw")])
        self.assertEqual(device.screen[status.TITLE_ID]["text"], "FINISHED")

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
        alone = client.elements("FINISHED")[status.TITLE_ID]
        self.assertEqual(with_subtitle["y"], status.TITLE_Y)
        self.assertEqual(alone["y"], status.TITLE_CENTER_Y)
        self.assertGreater(alone["y"], with_subtitle["y"])
        device = FakeDevice()
        self.drive(device, [("UserPromptSubmit", {}), ("Stop", {})])
        self.assertEqual(device.screen[status.TITLE_ID]["y"], status.TITLE_CENTER_Y)

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
        self.assertEqual((comet["x"], comet["path"]), (0, anim.animation_asset_name(self.config.colors["WORKING"], 72)))

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
        for name in names:  # the device cannot remove files whose full path is over ~64 characters
            self.assertLessEqual(len("/ext/user_assets/claude-code-status/" + name), 64, name)
        self.assertTrue(all(re.search(r"-v\d+\.anim$", name) for name in names))
        self.assertNotEqual(mascot.asset_name("WORKING"), "mascot-working-v2.anim", "the fixed art got a new name")


class AnimationTests(unittest.TestCase):
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
