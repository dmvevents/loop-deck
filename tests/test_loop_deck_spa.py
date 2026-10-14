#!/usr/bin/env python3
"""SPA checks: relative request URLs (static) and honest empty states (rendered).

Run with python3 -B tests/test_loop_deck_spa.py. LOOP_DECK_APP_JS selects a parent or neuter copy of
app/app.js. The render tests run the real app.js under node in a fake DOM (tests/spa_render.js)
against a status.json that the real generator emits in harness mode; they need node on PATH.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = Path(os.environ.get("LOOP_DECK_APP_JS", ROOT / "app/app.js")).resolve()
INDEX = ROOT / "app/index.html"
EXAMPLES = ROOT / "examples"
TMP = ROOT / ".tmp" / "spa-test-tmp"      # a working dir beside the tree; delete it after a run
TMP.mkdir(parents=True, exist_ok=True)
NODE = shutil.which("node")

# an absolute or scheme URL as the first argument of a request API
ABSOLUTE_REQUEST = re.compile(
    r"""(?:\bfetch|\.open\(\s*['"][A-Za-z]+['"]\s*,|\bnew\s+(?:EventSource|WebSocket))\s*\(?\s*['"`](?:/|[a-z]+:)""")
REQUEST_CALL = re.compile(r"""\bfetch\(\s*['"`]([^'"`]*)['"`]""")


class StaticUrls(unittest.TestCase):
    def test_no_absolute_request_url_in_app_js(self):
        """Contract §7: every request is relative (path-prefix proxy); fails on fetch('/..."""
        src = APP.read_text()
        self.assertGreaterEqual(len(REQUEST_CALL.findall(src)), 4, "scan found no fetch calls (vacuous)")
        self.assertEqual(ABSOLUTE_REQUEST.findall(src), [])
        self.assertEqual(sorted(set(REQUEST_CALL.findall(src))), ["__act", "act", "history.jsonl", "status.json"])

    def test_pattern_catches_planted_absolute_urls(self):
        """Positive control: the scan flags each absolute form it is meant to catch."""
        for planted in ("fetch('/act', {})", 'fetch("/status.json")', "fetch(`/__act`)",
                        "fetch('http://127.0.0.1:8091/act')", "xhr.open('POST', '/act')",
                        "new EventSource('/events')"):
            with self.subTest(planted=planted):
                self.assertTrue(ABSOLUTE_REQUEST.search(planted), planted)

    def test_index_html_has_no_absolute_src_or_href(self):
        """Contract §7: index.html loads app.js/app.css/mermaid relatively."""
        html = INDEX.read_text()
        self.assertGreaterEqual(len(re.findall(r'\b(?:src|href)="', html)), 4)
        self.assertEqual(re.findall(r'\b(?:src|href)="(?:/|[a-z]+:)', html), [])


@unittest.skipIf(NODE is None, "node is not on PATH: the rendered empty-state tests cannot run here")
class RenderedEmptyStates(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="spa-", dir=TMP)
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)

    def generate(self, *names):
        for name in names:
            shutil.copy(EXAMPLES / name, self.data / name)
        env = {k: v for k, v in os.environ.items() if not k.startswith("LOOP_DECK_")}
        env.update(LOOP_DECK_MODE="harness", LOOP_DECK_DATA_DIR=str(self.data))
        r = subprocess.run([sys.executable, "-I", str(ROOT / "generate-loop-viz.py")], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads((self.data / "out/status.json").read_text())

    def render(self, act_enabled=False):
        out = self.data / "out"
        r = subprocess.run([NODE, str(ROOT / "tests/spa_render.js"), str(APP), str(out / "status.json"),
                            str(out / "history.jsonl"), "true" if act_enabled else "false"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_zero_runs_render_the_documented_empty_states(self):
        """Contract §5: zero runs -> '—', 'no runs recorded yet', NO-DATA rows, 'no breakers', 'no history yet'."""
        status = self.generate("registry.json")
        o = self.render()
        v = o["views"]
        for route, view in v.items():
            with self.subTest(route=route):
                for fake in ("100%", "0/1", "✔ all clear", "that is the goal", "null", "undefined", "NaN"):
                    self.assertNotIn(fake, view["text"] + view["chrome"])
                if route != "runbook":   # the Runbook's status legend shows every chip by design
                    self.assertNotIn("● GREEN", view["text"])
                    self.assertNotIn("✕ RED", view["text"])
                self.assertIn("no runs recorded yet", view["chrome"])
        self.assertIn("health — no runs recorded yet", v[""]["text"])
        self.assertIn("missed — no runs recorded yet", v[""]["text"])
        self.assertIn("3 rows registered — no runs recorded yet", v[""]["text"])
        self.assertEqual(v["loops"]["text"].count("○ NO-DATA"), len(status["loops"]))
        self.assertIn("n0 --> n1", v["topology"]["text"])
        self.assertNotIn("HUB", v["topology"]["text"])
        self.assertIn("no breakers", v["breakers"]["text"])
        self.assertIn("circuit-broken (no ledger)", v["breakers"]["text"])
        self.assertEqual(v["history"]["text"].count("no history yet"), 3)
        for l in status["loops"]:
            self.assertIn(l["runbook_md"] or "no runbook", v["runbook"]["text"])

    def test_actions_hidden_when_disabled_and_never_probed_by_post(self):
        """Contract §7: GET __act enabled:false -> no live button, an honest note, and no POST at all."""
        status = self.generate("registry.json", "runs.jsonl", "loop-ledger.json", "state.json")
        o = self.render(act_enabled=False)
        self.assertEqual(o["posts"], [])
        self.assertIn("__act", o["gets"])
        self.assertEqual(sum(view["liveButtons"] for view in o["views"].values()), 0)
        for route in ["runbook"] + ["loop/" + l["slug"] for l in status["loops"]]:
            self.assertIn("actions disabled in this deployment", o["views"][route]["text"])
        enabled = self.render(act_enabled=True)
        self.assertEqual(enabled["posts"], [])
        self.assertGreater(enabled["views"]["loop/autonomy-loop"]["liveButtons"], 0, "positive control")
        self.assertNotIn("actions disabled in this deployment", enabled["views"]["loop/autonomy-loop"]["text"])

    def test_paused_banner_on_every_view(self):
        """Contract §3.4: state.json paused -> a PAUSED banner in the chrome of every view."""
        (self.data / "state.json").write_text(json.dumps({"schema": "loop-deck-state/1", "paused": True,
                                                          "reason": "harness STOP", "ts": "2026-10-13T20:00:00Z"}))
        self.generate("registry.json", "runs.jsonl")
        for route, view in self.render()["views"].items():
            with self.subTest(route=route):
                self.assertIn("⏸ PAUSED — harness STOP", view["chrome"])

    def test_every_served_link_is_in_the_server_allowlist(self):
        """Contract §7: page links point only at files deck-server serves; Runbook lists exactly three."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("deck_server_allow", ROOT / "deck-server.py")
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)          # module level only defines constants and classes
        self.generate("registry.json", "runs.jsonl", "loop-ledger.json", "state.json")
        views = self.render()["views"]
        links = {href for view in views.values() for href in view["links"]}
        self.assertTrue(links, "no links rendered (vacuous)")
        self.assertEqual(sorted("/" + h for h in links if "/" + h not in server.STATIC_ALLOW), [])
        self.assertEqual(views["runbook"]["links"], ["status.json", "history.jsonl", "acks.jsonl"])

    def test_breaker_threshold_and_drill_down_route(self):
        """Contract §3.3 + #/loop/<slug>: the ledger's max_attempts is rendered; every row has a drill-down."""
        status = self.generate("registry.json", "runs.jsonl", "loop-ledger.json", "state.json")
        v = self.render()["views"]
        self.assertIn("3/3", v["breakers"]["text"])
        self.assertIn("autonomy-loop:fire-12", v["breakers"]["text"])
        for l in status["loops"]:
            self.assertIn(l["name"], v["loop/" + l["slug"]]["text"])
            self.assertNotIn("no loop", v["loop/" + l["slug"]]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
