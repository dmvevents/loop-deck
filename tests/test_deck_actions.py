#!/usr/bin/env python3
"""Socketless /act regressions; --against accepts a saved deck-server.py blob."""
import argparse
import builtins
from collections import deque
from datetime import datetime, timezone
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "deck-server.py"
TOKEN = "fixture-act-token-0123456789"  # the fixture deployment enables actions (act token)


def forbid_external_effects(event, args):
    if event in {"subprocess.Popen", "os.system", "os.fork",
                 "socket.__new__", "socket.connect", "socket.bind"}:
        raise AssertionError(f"forbidden test effect: {event}")


class Child:
    """Keep duplicated descriptors as a child would, without creating a process."""

    def __init__(self, pass_fds):
        self.pid = 123456
        self.returncode = None
        self.fds = [os.dup(fd) for fd in pass_fds]

    def poll(self):
        return self.returncode

    def wait(self, *args, **kwargs):
        raise AssertionError("a request must never wait for the generator")

    def finish(self):
        self.returncode = 0
        for fd in self.fds:
            os.close(fd)
        self.fds.clear()


class AuditStream:
    def __init__(self, stream, case):
        self.stream, self.case = stream, case
        self.pending = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def write(self, text):
        if self.case.audit_fault == "write":
            raise OSError("planted audit write failure")
        if self.case.audit_fault == "partial-flush":
            self.pending = text
            return len(text)
        return self.stream.write(text)

    def flush(self):
        if self.case.audit_fault == "flush":
            raise OSError("planted audit flush failure")
        if self.case.audit_fault == "partial-flush":
            self.stream.write(self.pending[:8])
            self.stream.flush()
            raise OSError("planted partial audit flush failure")
        self.stream.flush()
        self.case.audit_events.append("flush")


class DeckActions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="deck-actions-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.out = self.repo / "out"
        self.out.mkdir(parents=True)
        self.pause = self.root / "state/LOOP_PAUSE"
        self.pause.parent.mkdir()
        self.audit = self.out / "actions.jsonl"
        self.acks = self.out / "acks.jsonl"
        self.clock = 1000.0
        self.state_paused = False
        self.action_rc = 0
        self.pause_results = deque()
        self.audit_fault = None
        self.audit_events = []
        self.effects = []
        self.replies = []
        self.gate_calls = []
        self.children = []
        self.spawn_error = False
        self.expected_spawn = None    # None = fleet: [python, REPO/generate-loop-viz.py], cwd REPO
        self.addCleanup(self.finish_children)
        env = mock.patch.dict(os.environ, {"LOOP_DECK_ACT_TOKEN": TOKEN})
        env.start()
        self.addCleanup(env.stop)
        for name in ("LOOP_DECK_MODE", "LOOP_DECK_DATA_DIR"):
            os.environ.pop(name, None)   # restored by the patch on cleanup
        self.registry({"loops": [{"slug": "current-loop"}]})
        self.load_server()

    def finish_children(self):
        for child in self.children:
            child.finish()

    def load_server(self):
        module = types.ModuleType("deck_server_fixture")
        module.__file__ = str(SOURCE)
        exec(compile(SOURCE.read_text(), str(SOURCE), "exec"), module.__dict__)
        module.REPO = str(self.repo)
        module.PAUSE_FILE = str(self.pause)
        module.GATE = str(self.root / "gate-never-executed")
        module.time = types.SimpleNamespace(time=lambda: self.clock,
                                            monotonic=lambda: self.clock)

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 13, 12, 0, tzinfo=timezone.utc)

        module.datetime = FixedDatetime
        module.subprocess = types.SimpleNamespace(
            run=self.gate, Popen=self.spawn, STDOUT=subprocess.STDOUT)
        module.open = self.open_file
        module.os = types.SimpleNamespace(**vars(os))
        module.os.fsync = self.fsync
        module.os.link = self.link
        module.os.replace = self.replace
        module.os.rename = self.replace
        module.os.unlink = self.unlink
        self.module = module

    def registry(self, value):
        (self.out / "status.json").write_text(json.dumps(value))

    def gate(self, args, **kwargs):
        self.assertEqual(args[:2], ["bash", self.module.GATE])
        self.assertLessEqual(kwargs["timeout"], 15)
        self.gate_calls.append(args[2:])
        if args[2] == "check-action":
            self.assertIn(args[3], ["deck-" + v for v in self.module.VERBS])
            rc = self.action_rc
        elif args[2:] == ["paused"]:
            rc = (self.pause_results.popleft() if self.pause_results else
                  int(not (self.state_paused or self.pause.exists())))
        else:
            raise AssertionError(f"unexpected gate command: {args[2:]}")
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="planted gate result")

    def observe_effect(self, name):
        self.effects.append((name, list(self.audit_events), self.rows(self.audit)))

    def open_file(self, path, mode="r", *args, **kwargs):
        path = Path(path)
        if any(flag in mode for flag in ("a", "w", "x", "+")):
            self.assertTrue(path.is_relative_to(self.root), path)
            if path in {self.pause, self.acks, self.out / "rerun.log"}:
                self.observe_effect(path.name)
        if path == self.audit and "a" in mode:
            if self.audit_fault == "open":
                raise OSError("planted audit open failure")
            return AuditStream(builtins.open(path, mode, *args, **kwargs), self)
        return builtins.open(path, mode, *args, **kwargs)

    def fsync(self, fd):
        st = os.fstat(fd)
        audit_st = self.audit.stat() if self.audit.exists() else None
        is_audit = audit_st and (st.st_dev, st.st_ino) == (audit_st.st_dev, audit_st.st_ino)
        is_directory = (st.st_dev, st.st_ino) == (
            self.out.stat().st_dev, self.out.stat().st_ino)
        if is_audit:
            if self.audit_fault == "fsync":
                raise OSError("planted audit fsync failure")
            self.audit_events.append("file-fsync")
        if is_directory and self.audit.exists():
            if self.audit_fault == "directory-fsync":
                raise OSError("planted audit directory fsync failure")
            self.audit_events.append("directory-fsync")
        os.fsync(fd)

    def link(self, src, dst, *args, **kwargs):
        self.observe_effect("archive")
        self.assertTrue(Path(dst).is_relative_to(self.root))
        return os.link(src, dst, *args, **kwargs)

    def replace(self, src, dst, *args, **kwargs):
        self.observe_effect("archive")
        self.assertTrue(Path(dst).is_relative_to(self.root))
        return os.replace(src, dst, *args, **kwargs)

    def unlink(self, path, *args, **kwargs):
        self.assertTrue(Path(path).is_relative_to(self.root))
        self.observe_effect("unlink")
        return os.unlink(path, *args, **kwargs)

    def spawn(self, args, **kwargs):
        expected = self.expected_spawn or (
            [sys.executable, str(self.repo / "generate-loop-viz.py")], str(self.repo))
        self.assertEqual(args, expected[0])
        self.assertEqual(kwargs["cwd"], expected[1])
        self.spawn_env = kwargs.get("env")
        self.observe_effect("spawn")
        if self.spawn_error:
            raise OSError("planted spawn failure")
        child = Child(kwargs.get("pass_fds", ()))
        self.children.append(child)
        return child

    @staticmethod
    def rows(path):
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def archives(self):
        return sorted(self.pause.parent.glob("LOOP_PAUSE.released.*"))

    def request(self, verb, *, origin="http://127.0.0.1:8091", slug="current-loop", token=TOKEN):
        h = object.__new__(self.module.Handler)
        payload = json.dumps({"verb": verb, "slug": slug, "reason": "planted reason"}).encode()
        h.directory = str(self.out)
        h.path = "/act"
        h.headers = {"Origin": origin, "Content-Length": str(len(payload))}
        if token is not None:
            h.headers["X-Loop-Deck-Token"] = token
        h.server = types.SimpleNamespace(server_port=8091)
        h.client_address = ("127.0.0.1", 12345)
        h.rfile, h.wfile = io.BytesIO(payload), io.BytesIO()
        statuses = []
        h.send_response = statuses.append
        h.send_header = lambda *args: None
        h.end_headers = lambda: None
        h.do_POST()
        self.assertEqual(len(statuses), 1)
        response = json.loads(h.wfile.getvalue())
        self.replies.append((statuses[0], response, list(self.audit_events)))
        return statuses[0], response

    def prepare(self, verb):
        if verb == "resume":
            self.pause.write_text("original pause reason")

    def assert_durable_action(self, verb):
        self.prepare(verb)
        code, body = self.request(verb)
        self.assertEqual(code, 202 if verb == "rerun-viz" else 200)
        self.assertTrue(body["ok"])
        self.assertTrue(self.effects, "positive control must exercise an effect")
        for effect, events, rows in self.effects:
            with self.subTest(effect=effect):
                self.assertIn("flush", events)
                self.assertIn("file-fsync", events)
                self.assertIn("directory-fsync", events)
                self.assertLess(events.index("flush"), events.index("file-fsync"))
                self.assertTrue(rows, "durable intent must be readable before the effect")
                self.assertNotEqual(rows[-1]["result"], "OK", "an intent must not claim completion")
        self.assertIn("file-fsync", self.replies[-1][2])

    def assert_audit_failure(self, verb, fault):
        self.prepare(verb)
        before_pause = self.pause.read_bytes() if self.pause.exists() else None
        self.audit_fault = fault
        code, body = self.request(verb)
        self.assertEqual(code, 500)
        self.assertFalse(body["ok"])
        self.assertEqual(self.pause.read_bytes() if self.pause.exists() else None, before_pause)
        self.assertFalse(self.acks.exists())
        self.assertFalse(self.archives())
        self.assertFalse(self.children)
        self.assertFalse(self.effects, "no action begins when the audit cannot be persisted")
        self.assertFalse((self.repo / ".refresh.lock").exists(),
                         "audit failure must not create even a generation lock file")

    def assert_partial_audit_retry(self, verb):
        self.prepare(verb)
        before_pause = self.pause.read_bytes() if self.pause.exists() else None
        # The normal observer parses JSONL. A malformed tail must not let the
        # test observer's decoder, rather than production code, block an effect.
        self.observe_effect = lambda name: self.effects.append(name)
        self.audit_fault = "partial-flush"
        self.assertEqual(self.request(verb)[0], 500)
        fragment = self.audit.read_bytes()
        self.assertEqual(len(fragment), 8, "the failed flush must retain actual bytes")
        self.assertFalse(fragment.endswith(b"\n"))
        self.assertFalse(self.effects)

        self.audit_fault = None
        code, body = self.request(verb)
        self.assertEqual(code, 500, "a retry must refuse an unterminated audit tail")
        self.assertFalse(body["ok"])
        self.assertEqual(self.audit.read_bytes(), fragment, "retain damaged evidence unchanged")
        self.assertFalse(self.effects)
        self.assertFalse(self.children)
        self.assertFalse(self.acks.exists())
        self.assertFalse(self.archives())
        self.assertEqual(self.pause.read_bytes() if self.pause.exists() else None, before_pause)
        self.assertFalse((self.repo / ".refresh.lock").exists())

    def test_untrusted_or_missing_origin_denies_without_gate_or_effect(self):
        for origin in ("", "null", "http://untrusted.invalid", "http://localhost:9999"):
            for verb in self.module.VERBS:
                with self.subTest(origin=origin, verb=verb):
                    self.assertEqual(self.request(verb, origin=origin)[0], 403)
        self.assertFalse(self.gate_calls)
        self.assertFalse(self.effects)

    def test_localhost_origin_and_known_current_slug_are_allowed(self):
        self.assertEqual(self.request("ack", origin="http://localhost:8091")[0], 200)
        self.assertEqual(self.rows(self.acks)[0]["slug"], "current-loop")

    def test_ack_rechecks_current_registry_after_slug_is_removed(self):
        self.assertEqual(self.request("ack")[0], 200)
        self.registry({"loops": [{"slug": "replacement-loop"}]})
        self.assertEqual(self.request("ack")[0], 400)
        self.assertEqual(len(self.rows(self.acks)), 1)

    def test_gate_denial_preserved_for_every_verb(self):
        self.action_rc = 2
        for verb in self.module.VERBS:
            with self.subTest(verb=verb):
                self.assertEqual(self.request(verb)[0], 403)
        self.assertFalse(self.effects)
        self.assertTrue(all(call[0] == "check-action" for call in self.gate_calls))

    def test_unknown_pause_state_denies_every_verb(self):
        for rc in (2, 64, -9):
            for verb in self.module.VERBS:
                with self.subTest(rc=rc, verb=verb):
                    self.pause_results.append(rc)
                    self.assertEqual(self.request(verb)[0], 503)
        self.assertFalse(self.effects)

    def test_paused_state_denies_every_verb_except_resume(self):
        self.state_paused = True
        for verb in ("ack", "pause", "rerun-viz"):
            with self.subTest(verb=verb):
                self.assertEqual(self.request(verb)[0], 409)
        self.assertFalse(self.effects)

    def test_ack_rejects_absent_registry(self):
        (self.out / "status.json").unlink()
        self.assertEqual(self.request("ack")[0], 400)
        self.assertFalse(self.acks.exists())

    def test_ack_rejects_unreadable_registry(self):
        (self.out / "status.json").unlink()
        (self.out / "status.json").mkdir()
        self.assertEqual(self.request("ack")[0], 400)
        self.assertFalse(self.acks.exists())

    def test_ack_rejects_malformed_json(self):
        (self.out / "status.json").write_text("{truncated")
        self.assertEqual(self.request("ack")[0], 400)
        self.assertFalse(self.acks.exists())

    def test_ack_rejects_invalid_registry_shapes(self):
        for value in (None, [], {}, {"loops": None}, {"loops": {}},
                      {"loops": []}, {"loops": [None]}, {"loops": [{}]},
                      {"loops": [{"slug": []}]}, {"loops": [{"slug": "bad slug"}]},
                      {"loops": [{"slug": "current-loop"}, {}]}):
            with self.subTest(registry=value):
                self.registry(value)
                self.assertEqual(self.request("ack")[0], 400)
                self.assertFalse(self.acks.exists())

    def test_ack_rejects_unknown_or_invalid_slug(self):
        for slug in ("unknown-loop", "", "current-loop\n", "../current-loop", None, 7):
            with self.subTest(slug=slug):
                self.assertEqual(self.request("ack", slug=slug)[0], 400)
        self.assertFalse(self.acks.exists())

    def test_resume_without_pause_file_reports_state_pause(self):
        self.state_paused = True
        code, body = self.request("resume")
        self.assertEqual(code, 409)
        self.assertFalse(body["ok"])
        self.assertIn("still paused via STATE.md", body["msg"])

    def test_resume_archives_file_but_reports_remaining_state_pause(self):
        self.pause.write_text("reason retained")
        self.state_paused = True
        code, body = self.request("resume")
        self.assertEqual(code, 409)
        self.assertFalse(body["ok"])
        self.assertIn("still paused via STATE.md", body["msg"])
        self.assertFalse(self.pause.exists())
        self.assertEqual([p.read_text() for p in self.archives()], ["reason retained"])

    def test_resume_rechecks_pause_query_after_archiving(self):
        self.pause.write_text("reason retained")
        self.pause_results.extend((0, 2))
        code, body = self.request("resume")
        self.assertEqual(code, 503)
        self.assertFalse(body["ok"])
        self.assertIn("UNKNOWN", body["msg"])
        self.assertEqual([p.read_text() for p in self.archives()], ["reason retained"])

    def test_two_resumes_in_one_timestamp_preserve_both_reasons(self):
        for reason in ("first reason", "second reason"):
            self.pause.write_text(reason)
            self.assertEqual(self.request("resume")[0], 200)
        self.assertEqual(sorted(p.read_text() for p in self.archives()),
                         ["first reason", "second reason"])

    def test_archive_collision_never_overwrites_existing_archive(self):
        self.pause.write_text("new reason")
        prefix = str(self.pause) + ".released.20261013T120000Z."
        Path(prefix + str(os.getpid())).write_text("legacy archive")
        Path(prefix + "collision").write_text("preexisting archive")
        suffixes = iter(("collision", "fresh"))
        self.module.uuid = types.SimpleNamespace(
            uuid4=lambda: types.SimpleNamespace(hex=next(suffixes)))
        self.assertEqual(self.request("resume")[0], 200)
        self.assertEqual(sorted(p.read_text() for p in self.archives()),
                         ["legacy archive", "new reason", "preexisting archive"])

    def resume_with_external_pause(self, when):
        self.pause.write_text("original pause")
        replacement = self.pause.parent / "new-emergency-pause"
        replacement.write_text("new emergency pause")
        injected = []

        def wrap(operation):
            def archive(src, dst, *args, **kwargs):
                is_pause = Path(src) == self.pause
                if is_pause and when == "before":
                    os.replace(replacement, self.pause)
                    injected.append(when)
                result = operation(src, dst, *args, **kwargs)
                if is_pause and when == "after":
                    os.replace(replacement, self.pause)
                    injected.append(when)
                return result
            return archive

        with mock.patch.object(self.module.os, "link", wrap(self.module.os.link)), \
                mock.patch.object(self.module.os, "replace", wrap(self.module.os.replace)):
            response = self.request("resume")
        self.assertEqual(injected, [when], "the concurrent replacement must actually occur")
        return response

    def test_new_pause_after_archive_move_is_preserved_and_reported(self):
        code, body = self.resume_with_external_pause("after")
        contents = [p.read_text() for p in self.archives()]
        if self.pause.exists():
            contents.append(self.pause.read_text())
        self.assertIn("new emergency pause", contents, "resume discarded the concurrent pause")
        self.assertEqual(self.pause.read_text(), "new emergency pause")
        self.assertEqual([p.read_text() for p in self.archives()], ["original pause"])
        self.assertEqual(code, 409)
        self.assertFalse(body["ok"])
        self.assertIn("still paused via LOOP_PAUSE", body["msg"])

    def test_new_pause_before_archive_move_is_archived_without_loss(self):
        code, body = self.resume_with_external_pause("before")
        self.assertEqual([p.read_text() for p in self.archives()], ["new emergency pause"])
        self.assertFalse(self.pause.exists())
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])

    def test_concurrent_audit_appends_wait_for_flock_and_preserve_every_row(self):
        rows = [{"writer": i, "msg": "line one\nline two \u03a9" * 128} for i in range(8)]
        ready = threading.Barrier(len(rows) + 1)
        finished = threading.Event()
        results = {}
        handler = object.__new__(self.module.Handler)

        def append(index, row):
            try:
                ready.wait(timeout=5)
                handler._append_jsonl(str(self.audit), row)
                results[index] = "ok"
            except BaseException as exc:
                results[index] = exc
            finally:
                finished.set()

        threads = [threading.Thread(target=append, args=(i, row), daemon=True)
                   for i, row in enumerate(rows)]
        # Each append opens a separate descriptor, so this is a real conflicting
        # flock even though the writers are threads in the same process.
        with self.audit.open("a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            try:
                for thread in threads:
                    thread.start()
                ready.wait(timeout=5)
                self.assertFalse(
                    finished.wait(1),
                    f"audit appends returned before the held flock was released: {results}")
                self.assertEqual(self.audit.read_bytes(), b"")
            finally:
                fcntl.flock(held, fcntl.LOCK_UN)
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(results, dict.fromkeys(range(len(rows)), "ok"))
        self.assertTrue(self.audit.read_bytes().endswith(b"\n"))
        self.assertCountEqual(self.rows(self.audit), rows)

    def test_concurrent_actions_do_not_interleave_or_wait_for_slow_audit(self):
        entered, release = threading.Event(), threading.Event()
        results = {}
        append = self.module.Handler._append_jsonl

        def delayed(handler, path, obj):
            if threading.current_thread().name == "first-action" and Path(path) == self.audit:
                entered.set()
                if not release.wait(2):
                    raise AssertionError("fixture audit was not released")
            return append(handler, path, obj)

        def run(name, verb):
            try:
                results[name] = self.request(verb)
            except BaseException as exc:
                results[name] = exc

        with mock.patch.object(self.module.Handler, "_append_jsonl", delayed):
            first = threading.Thread(target=run, args=("first", "pause"), name="first-action")
            second = threading.Thread(target=run, args=("second", "resume"))
            first.start()
            try:
                self.assertTrue(entered.wait(1))
                second.start()
                second.join(1)
                self.assertFalse(second.is_alive(), "contenders must fail promptly, not queue")
                self.assertIsInstance(results["second"], tuple)
                self.assertEqual(results["second"][0], 429)
                self.assertFalse(self.pause.exists())
            finally:
                release.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)
        self.assertEqual(results["first"][0], 200)
        self.assertEqual(self.request("resume")[0], 200)

    def test_slow_denial_reply_does_not_hold_action_lock(self):
        entered, release = threading.Event(), threading.Event()
        self.state_paused = True
        reply = self.module.Handler._reply
        results = []

        def delayed(handler, code, ok, msg):
            if threading.current_thread().name == "slow-reply":
                entered.set()
                if not release.wait(2):
                    raise AssertionError("fixture reply was not released")
            return reply(handler, code, ok, msg)

        with mock.patch.object(self.module.Handler, "_reply", delayed):
            first = threading.Thread(target=lambda: results.append(self.request("ack")),
                                     name="slow-reply")
            first.start()
            try:
                self.assertTrue(entered.wait(1))
                code, body = self.request("resume")
                self.assertEqual(code, 409)
                self.assertIn("STATE.md", body["msg"])
            finally:
                release.set()
                first.join(3)
        self.assertEqual(results[0][0], 409)

    def test_rerun_stays_busy_after_gap_until_child_exits(self):
        self.assertEqual(self.request("rerun-viz")[0], 202)
        self.clock += 2 * self.module.RERUN_MIN_GAP_S
        self.assertEqual(self.request("rerun-viz")[0], 429)
        self.assertEqual(len(self.children), 1)
        self.children[0].finish()
        self.assertEqual(self.request("rerun-viz")[0], 202)
        self.assertEqual(len(self.children), 2)

    def test_rerun_cooldown_still_applies_after_quick_child_exit(self):
        self.assertEqual(self.request("rerun-viz")[0], 202)
        self.children[0].finish()
        self.assertEqual(self.request("rerun-viz")[0], 429)
        self.clock += self.module.RERUN_MIN_GAP_S
        self.assertEqual(self.request("rerun-viz")[0], 202)

    def test_failed_spawn_releases_lock_and_does_not_consume_cooldown(self):
        self.spawn_error = True
        self.assertEqual(self.request("rerun-viz")[0], 500)
        self.spawn_error = False
        self.assertEqual(self.request("rerun-viz")[0], 202)

    def test_refresh_lock_contention_denies_spawn_then_allows_retry(self):
        lock_path = self.repo / ".refresh.lock"
        with lock_path.open("a") as held:
            inode = os.fstat(held.fileno()).st_ino
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.request("rerun-viz")[0], 429)
            self.assertFalse(self.children)
        self.assertEqual(self.request("rerun-viz")[0], 202)
        self.assertEqual(lock_path.stat().st_ino, inode)

    def test_child_holds_lock_across_server_reload_without_blocking_other_actions(self):
        self.assertEqual(self.request("rerun-viz")[0], 202)
        child = self.children[0]
        inherited_count = len(child.fds)
        self.assertEqual(self.request("ack")[0], 200)
        self.clock += 2 * self.module.RERUN_MIN_GAP_S
        self.load_server()
        self.assertEqual(self.request("rerun-viz")[0], 429)
        self.assertEqual(len(self.children), 1)
        self.assertEqual(inherited_count, 1)
        child.finish()
        self.assertEqual(self.request("rerun-viz")[0], 202)

    def test_denial_audit_failure_is_500(self):
        self.audit_fault = "fsync"
        code, body = self.request("ack", origin="http://untrusted.invalid")
        self.assertEqual(code, 500)
        self.assertFalse(body["ok"])
        self.assertFalse(self.effects)

    # ---------- action token (contract §7) ----------
    def reload_without_token(self, value=None):
        if value is None:
            os.environ.pop("LOOP_DECK_ACT_TOKEN", None)
        else:
            os.environ["LOOP_DECK_ACT_TOKEN"] = value
        self.load_server()

    def assert_refused_without_effect(self, code, result, n):
        self.assertEqual(code, 403)
        rows = self.rows(self.audit)
        self.assertEqual(len(rows), n)
        self.assertEqual({r["result"] for r in rows}, {result})
        self.assertNotIn("INTENT", {r["result"] for r in rows})
        self.assertFalse(self.gate_calls, "no gate call before the token check")
        self.assertFalse(self.effects)
        self.assertFalse(self.pause.exists())
        self.assertFalse(self.acks.exists())
        self.assertFalse(self.children, "no generator spawn")
        self.assertFalse((self.repo / ".refresh.lock").exists())

    def test_env_unset_refuses_every_verb_audited_act_disabled(self):
        """§7: LOOP_DECK_ACT_TOKEN unset -> 403 DENIED-ACT-DISABLED, audited, before any gate or effect."""
        self.reload_without_token()
        n = 0
        for verb in self.module.VERBS:
            for token in (TOKEN, None, ""):
                with self.subTest(verb=verb, token=token):
                    code, body = self.request(verb, token=token)
                    n += 1
                    self.assertFalse(body["ok"])
                    self.assert_refused_without_effect(code, "DENIED-ACT-DISABLED", n)

    def test_empty_token_env_means_disabled(self):
        """§7: an empty LOOP_DECK_ACT_TOKEN never enables actions (an empty header must not match)."""
        self.reload_without_token("")
        code, _ = self.request("ack", token="")
        self.assert_refused_without_effect(code, "DENIED-ACT-DISABLED", 1)

    def test_wrong_or_missing_token_refused(self):
        """§7: token set -> a POST without the exact X-Loop-Deck-Token is 403 DENIED-TOKEN."""
        n = 0
        for verb in self.module.VERBS:
            for token in (None, "", "wrong", TOKEN + "x", TOKEN[:-1], TOKEN.upper(), " " + TOKEN, "ü" + TOKEN):
                with self.subTest(verb=verb, token=token):
                    code, body = self.request(verb, token=token)
                    n += 1
                    self.assertFalse(body["ok"])
                    self.assert_refused_without_effect(code, "DENIED-TOKEN", n)

    def test_token_is_compared_in_constant_time(self):
        """§7: the comparison goes through hmac.compare_digest on bytes."""
        calls = []
        real = self.module.hmac.compare_digest
        self.module.hmac = types.SimpleNamespace(
            compare_digest=lambda a, b: calls.append((a, b)) or real(a, b))
        self.assertEqual(self.request("ack")[0], 200)
        self.assertEqual(calls, [(TOKEN.encode(), TOKEN.encode())])

    def assert_verb_passes_unchanged_gates(self, verb, expected_code):
        self.prepare(verb)
        code, body = self.request(verb)
        self.assertEqual(code, expected_code, body)
        self.assertTrue(body["ok"])
        self.assertEqual(self.gate_calls[0], ["check-action", "deck-" + verb])
        self.assertIn(["paused"], self.gate_calls)
        self.assertEqual(self.rows(self.audit)[0]["result"], "INTENT")

    def test_right_token_ack_runs_the_unchanged_gates(self):
        """§7: with the right token, ack passes Origin, whitelist, gate and pause checks as before."""
        self.assert_verb_passes_unchanged_gates("ack", 200)
        self.assertEqual(self.rows(self.acks)[0]["slug"], "current-loop")

    def test_right_token_pause_runs_the_unchanged_gates(self):
        """§7: with the right token, pause passes the unchanged checks and writes the pause file."""
        self.assert_verb_passes_unchanged_gates("pause", 200)
        self.assertTrue(self.pause.exists())

    def test_right_token_resume_runs_the_unchanged_gates(self):
        """§7: with the right token, resume passes the unchanged checks and archives the pause."""
        self.assert_verb_passes_unchanged_gates("resume", 200)
        self.assertEqual(len(self.archives()), 1)

    def test_right_token_rerun_runs_the_unchanged_gates(self):
        """§7: with the right token, rerun-viz passes the unchanged checks and spawns one generator."""
        self.assert_verb_passes_unchanged_gates("rerun-viz", 202)
        self.assertEqual(len(self.children), 1)

    def test_rerun_child_env_never_carries_the_token(self):
        """§7: the generator child gets an explicit env without LOOP_DECK_ACT_TOKEN."""
        self.assertEqual(self.request("rerun-viz")[0], 202)
        self.assertIsNotNone(self.spawn_env, "env must be explicit, not inherited")
        self.assertNotIn("LOOP_DECK_ACT_TOKEN", self.spawn_env)
        self.assertNotIn(TOKEN, self.spawn_env.values())

    def test_token_does_not_bypass_origin_or_paused(self):
        """§7: the token comes first; Origin and paused checks still deny after it."""
        self.assertEqual(self.request("ack", origin="http://untrusted.invalid")[0], 403)
        self.assertEqual(self.rows(self.audit)[-1]["result"], "DENIED-ORIGIN")
        self.state_paused = True
        self.assertEqual(self.request("ack")[0], 409)
        self.assertFalse(self.acks.exists())

    # ---------- 1.7.0 GET surface: /__act and the static allowlist ----------
    def get(self, path):
        h = object.__new__(self.module.Handler)
        h.directory = str(self.out)
        h.path, h.command, h.request_version = path, "GET", "HTTP/1.0"
        h.requestline = f"GET {path} HTTP/1.0"
        h.headers = {}
        h.server = types.SimpleNamespace(server_port=8091)
        h.client_address = ("127.0.0.1", 12345)
        h.rfile, h.wfile = io.BytesIO(), io.BytesIO()
        statuses = []
        h.send_response = lambda code, message=None: statuses.append(code)
        h.send_header = lambda *args: None
        h.end_headers = lambda: None
        h.do_GET()
        self.assertEqual(len(statuses), 1)
        return statuses[0], h.wfile.getvalue()

    def test_act_endpoint_reflects_the_env_and_never_the_token(self):
        """§7: GET /__act -> {"enabled": bool} from LOOP_DECK_ACT_TOKEN, never revealing it."""
        code, body = self.get("/__act")
        self.assertEqual((code, json.loads(body)), (200, {"enabled": True}))
        self.assertNotIn(TOKEN.encode(), body)
        self.reload_without_token()
        code, body = self.get("/__act")
        self.assertEqual((code, json.loads(body)), (200, {"enabled": False}))
        self.assertFalse(self.gate_calls)
        self.assertFalse(self.audit.exists(), "a GET never writes the audit")

    def test_static_allowlist_serves_only_the_deck_files(self):
        """§7: only the allowlisted files are served; planted files and subdirectories are 404."""
        (self.out / "index.html").write_text("<!doctype html>")
        (self.out / "history.jsonl").write_text("")
        for name in ("runs.jsonl", "state-head.snapshot", "refresh.log", "actions.jsonl", "server.log"):
            (self.out / name).write_text("planted secret\n")
        (self.out / "driver").mkdir()
        (self.out / "driver/board.json").write_text("{}")
        for path in ("/status.json", "/history.jsonl", "/index.html", "/", "/status.json?x=1"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 200)
        for path in ("/runs.jsonl", "/state-head.snapshot", "/refresh.log", "/actions.jsonl",
                     "/server.log", "/driver/", "/driver", "/driver/board.json", "/../repo/out/runs.jsonl",
                     "//status.json", "/status.json/", "/STATUS.JSON"):
            with self.subTest(path=path):
                code, body = self.get(path)
                self.assertEqual(code, 404)
                self.assertNotIn(b"planted secret", body)

    def test_root_without_index_is_not_listed(self):
        """§7: "/" with no index.html is 404, never a directory listing."""
        (self.out / "runs.jsonl").write_text("planted secret\n")
        code, body = self.get("/")
        self.assertEqual(code, 404)
        self.assertNotIn(b"runs.jsonl", body)

    # ---------- harness-mode rerun-viz (contract §1) ----------
    def test_harness_rerun_regenerates_only_the_data_dir(self):
        """§1: harness rerun-viz spawns -I with the mode and data dir, locks under out/, never REPO/viz."""
        data = self.root / "data"
        self.out = data / "out"
        self.out.mkdir(parents=True)
        self.audit, self.acks = self.out / "actions.jsonl", self.out / "acks.jsonl"
        fleet_out = self.repo / "out"
        before = sorted(p.name for p in fleet_out.iterdir())
        os.environ.update(LOOP_DECK_MODE="harness", LOOP_DECK_DATA_DIR=str(data))
        self.load_server()
        self.expected_spawn = ([sys.executable, "-I", str(self.repo / "generate-loop-viz.py")], str(data))
        self.assertEqual(self.request("rerun-viz")[0], 202)
        self.assertEqual((self.spawn_env["LOOP_DECK_MODE"], self.spawn_env["LOOP_DECK_DATA_DIR"]),
                         ("harness", str(data)))
        self.assertTrue((self.out / ".rerun.lock").exists())
        self.assertTrue((self.out / "rerun.log").exists())
        self.assertFalse((self.repo / ".refresh.lock").exists())
        self.assertEqual(sorted(p.name for p in fleet_out.iterdir()), before, "fleet out/ untouched")


for _verb in ("pause", "resume", "ack", "rerun-viz"):
    def durable(self, verb=_verb):
        self.assert_durable_action(verb)
    setattr(DeckActions, "test_durable_audit_precedes_" + _verb.replace("-", "_"), durable)
    def partial_retry(self, verb=_verb):
        self.assert_partial_audit_retry(verb)
    setattr(DeckActions, "test_partial_audit_flush_retry_blocks_" + _verb.replace("-", "_"),
            partial_retry)
    for _fault in ("open", "write", "flush", "fsync", "directory-fsync"):
        def audit_failure(self, verb=_verb, fault=_fault):
            self.assert_audit_failure(verb, fault)
        setattr(DeckActions, "test_audit_" + _fault.replace("-", "_") +
                "_failure_blocks_" + _verb.replace("-", "_"), audit_failure)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--against", type=Path, default=SOURCE)
    args, rest = parser.parse_known_args()
    SOURCE = args.against.resolve()
    sys.addaudithook(forbid_external_effects)
    unittest.main(argv=[sys.argv[0], *rest])
