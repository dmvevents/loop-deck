#!/usr/bin/env python3
"""Loop Deck harness mode (docs/CONTRACT.md) against local fixtures only.

Run with python3 -B tests/test_loop_deck_harness_mode.py.
LOOP_VIZ_GENERATOR selects a parent or neuter copy of generate-loop-viz.py; the copy must sit at
<root>/ with the contract validator at <root>/tools/check-contract.py (the generator loads it by path).
"""
import builtins
import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get("LOOP_VIZ_GENERATOR", ROOT / "generate-loop-viz.py")).resolve()
EXAMPLES = ROOT / "examples"
TMP = ROOT / ".tmp" / "harness-test-tmp"      # a working dir beside the tree; delete it after a run
TMP.mkdir(parents=True, exist_ok=True)
GOLDEN_NOW = datetime(2026, 10, 13, 17, 30, tzinfo=timezone.utc)


def row(slug, kind="loop", cadence=300, enabled=True, depends_on=(), runbook=None):
    r = {"slug": slug, "name": slug.replace("-", " ").title(), "kind": kind, "owner": f"/fixture/{slug}.sh",
         "cadence_s": None if kind == "seed" else cadence, "tags": [f"t-{slug}"], "enabled": enabled,
         "depends_on": list(depends_on)}
    if runbook is not None:
        r["runbook_md"] = runbook
    return r


def run(slug, ts, rc=0, dur=1, note=None):
    rec = {"ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "tag": f"t-{slug}", "rc": rc, "dur_s": dur}
    if note is not None:
        rec["note"] = note
    return json.dumps(rec)


class HarnessMode(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="harness-", dir=TMP)
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name) / "data"
        self.data.mkdir()
        self.out = self.data / "out"

    # ---------- fixtures ----------
    def write_registry(self, rows):
        (self.data / "registry.json").write_text(json.dumps({
            "schema": "loop-deck-registry/1", "generated_at": "2026-10-13T20:00:00Z",
            "source": "fixture", "loops": rows}))

    def write_runs(self, *lines):
        (self.data / "runs.jsonl").write_text("".join(line + "\n" for line in lines))

    def copy_examples(self):
        for p in EXAMPLES.iterdir():
            shutil.copy(p, self.data / p.name)

    def status(self):
        return json.loads((self.out / "status.json").read_text())

    def by_slug(self, status):
        return {r["slug"]: r for r in status["loops"]}

    # ---------- runners ----------
    def subprocess_run(self, env_extra=None, unset=False, path_prefix=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("LOOP_DECK_")}
        env["LOOP_DECK_MODE"] = "harness"
        if not unset:
            env["LOOP_DECK_DATA_DIR"] = str(self.data)
        if path_prefix:
            env["PATH"] = str(path_prefix) + os.pathsep + env.get("PATH", "")
        env.update(env_extra or {})
        return subprocess.run([sys.executable, "-I", str(SOURCE)], env=env, capture_output=True,
                              text=True, timeout=30)

    def inprocess_run(self, now=GOLDEN_NOW):
        """Fixed clock, no subprocess/socket/shell, every open() recorded."""
        spec = importlib.util.spec_from_file_location("harness_under_test", SOURCE)
        viz = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(viz)
        viz.datetime = mock.Mock(wraps=datetime)
        viz.datetime.now.side_effect = lambda tz=None: now
        viz.datetime.strptime = datetime.strptime
        opened, real_open = [], builtins.open

        def recording_open(path, *a, **kw):
            opened.append(os.path.realpath(path) if isinstance(path, (str, os.PathLike)) else path)
            return real_open(path, *a, **kw)

        env = {"LOOP_DECK_MODE": "harness", "LOOP_DECK_DATA_DIR": str(self.data)}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(builtins, "open", side_effect=recording_open), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("probe: subprocess.run")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("probe: Popen")), \
                mock.patch.object(os, "system", side_effect=AssertionError("probe: os.system")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as exited:
                viz.main()
        self.opened, self.stderr = opened, err.getvalue()
        return exited.exception.code

    # ---------- (1) empty data dir ----------
    def test_empty_data_dir_renders_zero_run_markers(self):
        """§5 + §4 runs.jsonl missing: registry only -> exit 0, NO-DATA rows, no health number, no history."""
        self.write_registry([row("alpha"), row("beta", depends_on=["alpha"]), row("gamma", kind="seed")])
        r = self.subprocess_run()
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.status()
        self.assertEqual(s["mode"], "harness")
        self.assertIsNone(s["health_pct"], "zero runs must not render a health number")
        self.assertEqual((s["healthy"], s["denom"], s["runs_total"]), (0, 0, 0))
        self.assertIsNone(s["missed_fires_total"])
        self.assertEqual(s["attention"], [])
        self.assertEqual([l["slug"] for l in s["loops"]], ["alpha", "beta", "gamma"], "no fabricated rows")
        self.assertEqual({l["status"] for l in s["loops"]}, {"NO-DATA"})
        self.assertEqual(s["counts"], {"NO-DATA": 3})
        for l in s["loops"]:
            self.assertNotIn("run_success", l)
            self.assertNotIn("last_run", l)
            self.assertIsNone(l["missed"])
        self.assertEqual((s["breakers"], s["max_attempts"], s["paused"]), ([], None, False))
        self.assertFalse(s["registry_drift"])
        self.assertTrue(all(l["expected_sessions"] == [] and l["missing_sessions"] == [] for l in s["loops"]))
        self.assertNotIn("HUB", s["topology_mmd"])
        self.assertIn("n0 --> n1", s["topology_mmd"])
        self.assertEqual(re.findall(r"class (\S+) (\S+);", s["topology_mmd"]), [("n0,n1,n2", "cNODATA")])
        self.assertFalse((self.out / "history.jsonl").exists(), "zero runs must leave history untouched")
        self.assertEqual(oct((self.out / "status.json").stat().st_mode & 0o777), "0o600")

    # ---------- (2) worked examples golden ----------
    def test_worked_examples_golden(self):
        """§9 golden: examples -> 3 rows, rc 97 RED, seed never missed, dur_s null, breaker from ledger."""
        self.copy_examples()
        self.assertEqual(self.inprocess_run(), 0, self.stderr)
        s = self.status()
        rows = self.by_slug(s)
        self.assertEqual(len(s["loops"]), 3)
        auto, watch, seed = rows["autonomy-loop"], rows["slack-watch"], rows["hindsight-seed"]
        self.assertEqual((auto["status"], auto["run_success"]["last_rc"]), ("RED", 97))
        self.assertEqual(auto["run_success"]["runs"], 2)
        self.assertEqual(auto["run_success"]["success_pct"], 50)
        self.assertTrue(auto["breaker"])
        self.assertEqual(auto["ledger"], {"item": "autonomy-loop:fire-12", "attempts": 3, "max": 3,
                                          "resolved": False})
        self.assertEqual(s["breakers"][0]["state"], "CIRCUIT-BROKEN")
        self.assertEqual((watch["status"], watch["last_run"]["dur_s"]), ("GREEN", None))
        self.assertEqual((seed["status"], seed["missed"]), ("GREEN", False))
        self.assertEqual((s["health_pct"], s["healthy"], s["denom"]), (66.7, 2, 3))
        self.assertEqual((s["max_attempts"], s["paused"], s["pause_reason"]), (3, False, ""))
        self.assertEqual(s["attention"], ["autonomy-loop"])
        hist = [json.loads(x) for x in (self.out / "history.jsonl").read_text().splitlines()]
        self.assertEqual(hist[-1]["s"], {"autonomy-loop": "R", "slack-watch": "G", "hindsight-seed": "G"})

    def test_seed_is_never_missed_at_any_age(self):
        """§3.2 seed rows: GREEN on rc 0 at any age, never STALE/missed."""
        self.copy_examples()
        self.assertEqual(self.inprocess_run(now=GOLDEN_NOW + timedelta(days=60)), 0, self.stderr)
        seed = self.by_slug(self.status())["hindsight-seed"]
        self.assertEqual((seed["status"], seed["missed"]), ("GREEN", False))

    # ---------- (3) drift ----------
    def test_unclaimed_tag_is_registry_drift_exit_3(self):
        """§4 unclaimed tag: exit 3, status.json written with registry_drift true and the tag listed."""
        self.copy_examples()
        with open(self.data / "runs.jsonl", "a") as f:
            f.write('{"ts": "2026-10-13T17:29:00Z", "tag": "nobody-owns-me", "rc": 0, "dur_s": 1}\n')
        self.assertEqual(self.inprocess_run(), 3, self.stderr)
        s = self.status()
        self.assertEqual((s["registry_drift"], s["drift"], s["unclaimed_tags"]), (True, True, ["nobody-owns-me"]))
        self.assertEqual(len(s["loops"]), 3, "an unclaimed tag never becomes a row")

    # ---------- (4) malformed input ----------
    def test_malformed_runs_line_exit_2_writes_nothing(self):
        """§4 malformed runs.jsonl line: exit 2, one-line reason, no traceback, no status.json."""
        good = run("alpha", GOLDEN_NOW)
        bad_lines = {
            "not JSON": "FIRE 3 END rc=0 duration=12s",
            "missing rc": '{"ts": "2026-10-13T17:00:00Z", "tag": "t-alpha", "dur_s": 1}',
            "ts null": '{"ts": null, "tag": "t-alpha", "rc": 0, "dur_s": 1}',
            "ts number": '{"ts": 1791912476, "tag": "t-alpha", "rc": 0, "dur_s": 1}',
            "impossible date": '{"ts": "2026-13-45T99:99:99Z", "tag": "t-alpha", "rc": 0, "dur_s": 1}',
            "rc string": '{"ts": "2026-10-13T17:00:00Z", "tag": "t-alpha", "rc": "0", "dur_s": 1}',
            "blank line": "",
        }
        for label, bad in bad_lines.items():
            with self.subTest(label):
                self.write_registry([row("alpha")])
                self.write_runs(good, bad)
                r = self.subprocess_run()
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertFalse((self.out / "status.json").exists())
                self.assertNotIn("Traceback", r.stderr)
                self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr)
                self.assertTrue(r.stderr.startswith("loop-deck harness: "), r.stderr)

    def test_malformed_ledger_or_state_exit_2(self):
        """§4 loop-ledger.json / state.json present but malformed: exit 2 (UNKNOWN is not running)."""
        cases = {"loop-ledger.json": '{"schema": "loop-deck-ledger/1", "max_attempts": 3, "items": []}',
                 "state.json": '{"schema": "loop-deck-state/1", "paused": "false", "ts": "2026-10-13T20:00:00Z"}',
                 "registry.json": "{truncated"}
        for name, body in cases.items():
            with self.subTest(name):
                self.copy_examples()
                (self.data / name).write_text(body)
                r = self.subprocess_run()
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertFalse((self.out / "status.json").exists())
                self.assertNotIn("Traceback", r.stderr)

    # ---------- (5) data dir ----------
    def test_data_dir_unset_relative_or_missing_exit_2(self):
        """§1/§4 LOOP_DECK_DATA_DIR unset / relative / missing: exit 2 and nothing created."""
        self.write_registry([row("alpha")])
        for label, kw in (("unset", {"unset": True}),
                          ("relative", {"env_extra": {"LOOP_DECK_DATA_DIR": "data"}}),
                          ("missing", {"env_extra": {"LOOP_DECK_DATA_DIR": str(self.data / "absent")}})):
            with self.subTest(label):
                r = self.subprocess_run(**kw)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                self.assertFalse(self.out.exists())
                self.assertFalse((self.data / "absent").exists())

    # ---------- (6) probes forbidden ----------
    def test_no_probe_reaches_the_path_shim(self):
        """§1/§8 harness mode never runs crontab, tmux, git, journalctl, pgrep, ss (PATH shim log stays empty)."""
        shim, log = self.data.parent / "shim", self.data.parent / "probes.log"
        shim.mkdir()
        for name in ("crontab", "tmux", "git", "journalctl", "pgrep", "ss"):
            (shim / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 1\n')
            (shim / name).chmod(0o755)
        control = subprocess.run(["sh", "-c", "crontab -l; tmux ls"], capture_output=True, text=True,
                                 env={"PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}, timeout=10)
        self.assertEqual(control.returncode, 1)
        self.assertEqual(log.read_text().splitlines(), ["crontab -l", "tmux ls"], "positive control: shim records")
        log.write_text("")
        self.copy_examples()
        r = self.subprocess_run(path_prefix=shim)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(log.read_text(), "", "a fleet probe leaked out of harness mode")

    def test_reads_only_the_data_dir(self):
        """§1 reads ONLY the four inputs (no LOOP.md, STATE.md, gate.yaml, syslog, fleet-members.json)."""
        self.copy_examples()
        self.assertEqual(self.inprocess_run(), 0, self.stderr)
        data = os.path.realpath(self.data)
        spa = {os.path.realpath(SOURCE.parent / "app" / n) for n in ("index.html", "app.css", "app.js")}
        outside = [p for p in self.opened if not str(p).startswith(data + os.sep) and p not in spa]
        self.assertEqual(outside, [], "harness mode opened a file outside LOOP_DECK_DATA_DIR")
        self.assertEqual({p for p in self.opened if p in spa}, spa, "the page is deployed from app/")
        for n in ("index.html", "app.css", "app.js"):
            self.assertEqual((self.out / n).read_bytes(), (SOURCE.parent / "app" / n).read_bytes())
        names = {os.path.basename(str(p)) for p in self.opened}
        self.assertTrue({"registry.json", "runs.jsonl", "loop-ledger.json", "state.json"} <= names)

    # ---------- §3.2 run-derived state: one test per state ----------
    def state_of(self, r, *lines, now=GOLDEN_NOW):
        self.write_registry([r])
        self.write_runs(*lines)
        self.assertEqual(self.inprocess_run(now=now), 0, self.stderr)
        l = self.status()["loops"][0]
        return l["status"], l["missed"]

    def test_state_green_within_two_cadences(self):
        """§3.2 boundary: latest rc 0 at age exactly 2 x cadence_s is GREEN."""
        self.assertEqual(self.state_of(row("a"), run("a", GOLDEN_NOW - timedelta(seconds=600))), ("GREEN", False))

    def test_state_stale_beyond_two_cadences(self):
        """§3.2 boundary: 2 x cadence_s + 1 s with rc 0 is STALE."""
        self.assertEqual(self.state_of(row("a"), run("a", GOLDEN_NOW - timedelta(seconds=601))), ("STALE", False))
        self.assertEqual(self.status()["missed_fires_total"], 0, "STALE is not missed")

    def test_state_stale_at_exactly_six_cadences(self):
        """§3.2 boundary: age exactly 6 x cadence_s is still STALE (RED only beyond it)."""
        self.assertEqual(self.state_of(row("a"), run("a", GOLDEN_NOW - timedelta(seconds=1800))), ("STALE", False))
        self.assertEqual(self.status()["missed_fires_total"], 0, "STALE is not missed")

    def test_state_red_missed_beyond_six_cadences(self):
        """§3.2 boundary: 6 x cadence_s + 1 s is RED "missed" (periodic kinds only)."""
        self.assertEqual(self.state_of(row("a"), run("a", GOLDEN_NOW - timedelta(seconds=1801))), ("RED", True))
        self.assertEqual(self.status()["missed_fires_total"], 1)

    def test_state_red_on_nonzero_rc(self):
        """§3.2 RED: latest rc != 0 even when fresh."""
        self.assertEqual(self.state_of(row("a"), run("a", GOLDEN_NOW, rc=124)), ("RED", False))

    def test_state_no_data_without_records(self):
        """§3.2 NO-DATA: zero records for the row (another row has some); excluded from health."""
        self.write_registry([row("a"), row("b")])
        self.write_runs(run("b", GOLDEN_NOW))
        self.assertEqual(self.inprocess_run(), 0, self.stderr)
        s = self.status()
        a = self.by_slug(s)["a"]
        self.assertEqual((a["status"], a["missed"]), ("NO-DATA", None))
        self.assertEqual((s["health_pct"], s["healthy"], s["denom"]), (100.0, 1, 1))

    def test_state_disabled_ignores_records(self):
        """§3.2 DISABLED: enabled false -> grey, run records ignored, excluded from health."""
        self.assertEqual(self.state_of(row("a", enabled=False), run("a", GOLDEN_NOW, rc=1)), ("DISABLED", None))
        s = self.status()
        self.assertEqual((s["health_pct"], s["denom"], s["attention"]), (None, 0, []))

    def test_state_seed_red_on_nonzero_rc(self):
        """§3.2 seed rows: RED on rc != 0, never STALE."""
        self.assertEqual(self.state_of(row("s", kind="seed"), run("s", GOLDEN_NOW - timedelta(days=9), rc=2)),
                         ("RED", False))

    def test_latest_record_is_chosen_by_ts_not_file_order(self):
        """§3.2 ts is the fire END; an older record appended after a newer one never wins."""
        st = self.state_of(row("a"), run("a", GOLDEN_NOW - timedelta(seconds=60), rc=0),
                           run("a", GOLDEN_NOW - timedelta(seconds=3000), rc=1))
        self.assertEqual(st, ("GREEN", False))
        rs = self.status()["loops"][0]["run_success"]
        self.assertEqual((rs["last_rc"], rs["runs"], rs["ok"]), (0, 2, 1))

    def test_paused_flag_and_reason_from_state_json(self):
        """§3.4 paused: status.json carries paused + pause_reason from state.json."""
        self.copy_examples()
        (self.data / "state.json").write_text(json.dumps({"schema": "loop-deck-state/1", "paused": True,
                                                         "reason": "harness STOP", "ts": "2026-10-13T20:00:00Z"}))
        self.assertEqual(self.inprocess_run(), 0, self.stderr)
        s = self.status()
        self.assertEqual((s["paused"], s["pause_reason"]), (True, "harness STOP"))

    def test_breaker_threshold_comes_from_the_ledger_file(self):
        """§3.3 breaker = an item '<slug>:*' with attempts >= the file's max_attempts (not a hard-coded 3)."""
        self.write_registry([row("alpha"), row("alpha-two")])
        self.write_runs(run("alpha", GOLDEN_NOW), run("alpha-two", GOLDEN_NOW))
        for mx, expect in ((5, False), (3, True), (2, True)):
            with self.subTest(max_attempts=mx):
                (self.data / "loop-ledger.json").write_text(json.dumps({
                    "schema": "loop-deck-ledger/1", "max_attempts": mx,
                    "items": {"alpha:fire-1": {"attempts": 3}, "alpha-two:fire-9": {"attempts": 9}}}))
                self.assertEqual(self.inprocess_run(), 0, self.stderr)
                s = self.status()
                rows = self.by_slug(s)
                self.assertEqual((rows["alpha"]["breaker"], s["max_attempts"]), (expect, mx),
                                 "prefix is '<slug>:', so alpha-two:* never trips alpha")
                self.assertTrue(rows["alpha-two"]["breaker"])
        (self.data / "loop-ledger.json").write_text(json.dumps({
            "schema": "loop-deck-ledger/1", "max_attempts": 3, "items": {"ghost:x": {"attempts": 3}}}))
        r = self.subprocess_run()
        self.assertEqual(r.returncode, 2, "a ledger key for no registry row is a contract violation")

    # ---------- (7) fleet mode unchanged ----------
    def test_fleet_mode_suites_still_pass(self):
        """Fleet mode is not supported from the release tree, so its suites are not shipped with it."""
        self.skipTest("fleet-only suites test_loop_viz_session_history.py and test_membership_refresh.py are not part of this tree")
        env = {k: v for k, v in os.environ.items() if not k.startswith("LOOP_DECK_")}
        env["LOOP_VIZ_GENERATOR"] = str(SOURCE)
        for suite, floor in (("tests/test_loop_viz_session_history.py", 22),
                             ("tools/test_membership_refresh.py", 6)):
            with self.subTest(suite):
                r = subprocess.run([sys.executable, "-B", str(ROOT / suite)], cwd=ROOT, env=env,
                                   capture_output=True, text=True, timeout=300)
                self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
                counts = [int(n) for n in re.findall(r"^Ran (\d+) tests? in ", r.stderr, re.M)]
                self.assertTrue(counts, "no unittest summary line: the suite proved nothing")
                self.assertGreaterEqual(sum(counts), floor, f"{suite} shrank below its floor")

if __name__ == "__main__":
    unittest.main(verbosity=2)
