#!/usr/bin/env python3
"""deck-server.py — Loop Deck same-origin server: static SPA + gated POST /act (A2 tier).

Action tier A2. Replaces `python3 -m http.server` so
the action endpoint shares the SPA's origin — CSP connect-src 'self' stays intact and no
second loopback port is consumed (port registry: LOOP.md "Safety").

Safety invariants (binding — the Deck design study and the operator's loop constraints):
  * binds 127.0.0.1 ONLY (asserted at startup; refresh script re-verifies with ss)
  * STATIC verb whitelist — request fields never reach a shell; no user-supplied commands
  * every verb runs `loop-gate.sh check-action deck-<verb>` first (mechanical, not model-trusted)
  * fleet paused => every verb except `resume` answers 409
  * durable audit BEFORE effects or replies; accepted actions record INTENT, not completion
  * `resume` ARCHIVES the pause file without overwriting evidence, then rechecks pause state
  * Origin allowlist: only our own loopback origins may POST (blocks cross-origin browser JS)
  * 1.7.0: /act is OFF unless LOOP_DECK_ACT_TOKEN is set (403 DENIED-ACT-DISABLED);
    with it set, a POST must carry X-Loop-Deck-Token (constant-time compare, else 403
    DENIED-TOKEN) before any check above runs. GET /__act -> {"enabled": bool}, never the token.
  * 1.7.0: static serving is an allowlist (STATIC_ALLOW); everything else 404, no listings.
  * LOOP_DECK_MODE=harness: rerun-viz regenerates $LOOP_DECK_DATA_DIR/out only (contract §1).
"""
import argparse
import fcntl
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

VERSION = "1.7.1"
REPO = os.path.dirname(os.path.abspath(__file__))
PAUSE_FILE = os.environ.get("DECK_PAUSE_FILE", os.path.expanduser("~/.loop-deck/LOOP_PAUSE"))
GATE = os.environ.get("DECK_GATE", os.path.join(REPO, "loop-gate.sh"))
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")  # \Z not $: $ would accept a trailing newline
VERBS = ("rerun-viz", "pause", "resume", "ack")
RERUN_MIN_GAP_S = 30
ACT_TOKEN = os.environ.get("LOOP_DECK_ACT_TOKEN", "")   # empty or unset = actions OFF
HARNESS = os.environ.get("LOOP_DECK_MODE") == "harness"
DATA_DIR = os.environ.get("LOOP_DECK_DATA_DIR", "")
STATIC_ALLOW = frozenset(("/", "/index.html", "/app.js", "/app.css", "/mermaid.min.js",
                          "/status.json", "/history.jsonl", "/acks.jsonl"))
_last_rerun = [None]
_rerun_process = None
_action_lock = threading.Lock()


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AuditError(OSError):
    pass


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Handler(SimpleHTTPRequestHandler):
    server_version = "LoopDeck/" + VERSION

    def log_message(self, *a):  # audit jsonl is the record; stdout is detached under nohup
        pass

    def end_headers(self):
        self.send_header("X-Deck-Server", VERSION)
        if self.path.endswith((".json", ".jsonl")):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        if self.path == "/__version":  # refresh script compares this to the source VERSION
            body = VERSION.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/__act":  # the SPA hides its buttons from this; never reveals the token
            body = json.dumps({"enabled": bool(ACT_TOKEN)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if urlsplit(self.path).path not in STATIC_ALLOW:
            return self.send_error(404)
        super().do_GET()

    def do_HEAD(self):
        if urlsplit(self.path).path not in STATIC_ALLOW:
            return self.send_error(404)
        super().do_HEAD()

    def list_directory(self, path):  # "/" without index.html must not become a listing
        self.send_error(404)
        return None

    # ---------- A2 endpoint ----------
    def do_POST(self):
        if self.path != "/act":
            return self._reply(404, False, "unknown endpoint")
        verb, slug = "?", None
        self._has_intent = False
        try:
            # The act-token gate precedes every other check, gate call and effect.
            if not ACT_TOKEN:
                return self._audited(403, "DENIED-ACT-DISABLED", verb, slug,
                                     "actions are disabled in this deployment (LOOP_DECK_ACT_TOKEN unset)")
            given = self.headers.get("X-Loop-Deck-Token", "")
            if not hmac.compare_digest(given.encode("utf-8", "surrogateescape"),
                                       ACT_TOKEN.encode("utf-8", "surrogateescape")):
                return self._audited(403, "DENIED-TOKEN", verb, slug,
                                     "missing or wrong X-Loop-Deck-Token header")
            origin = self.headers.get("Origin", "")
            allowed = {f"http://127.0.0.1:{self.server.server_port}",
                       f"http://localhost:{self.server.server_port}"}
            if origin not in allowed:
                return self._audited(403, "DENIED-ORIGIN", verb, slug,
                                     f"origin {origin!r} not in loopback allowlist")
            n = int(self.headers.get("Content-Length", 0))
            if not 0 < n <= 4096:
                return self._audited(413, "DENIED-SIZE", verb, slug, "body missing or too large")
            try:
                body = json.loads(self.rfile.read(n).decode())
            except (ValueError, UnicodeDecodeError):
                return self._audited(400, "DENIED-BODY", verb, slug, "body is not JSON")
            verb = str(body.get("verb", ""))
            raw_slug = body.get("slug")
            slug = raw_slug if isinstance(raw_slug, str) else None
            if verb not in VERBS:
                return self._audited(400, "DENIED-VERB", verb, slug,
                                     f"verb not in static whitelist {VERBS}")
            # mechanical gate HOOK (review finding): none of the deck-* action ids are in
            # gate.yaml humanGateActions today, so this allows everything — it is the audit
            # label + the toggle point where a future gate.yaml stanza can block verbs,
            # NOT an active block. The real enforcement here: whitelist/origin/paused/audit.
            g = subprocess.run(["bash", GATE, "check-action", f"deck-{verb}"],
                               capture_output=True, text=True, timeout=15)
            if g.returncode != 0:
                return self._audited(403, "DENIED-GATE", verb, slug,
                                     (g.stdout or g.stderr).strip()[:200])
            # Serialize pause checks with actions. Contenders never queue behind a slow
            # gate or disk, and the lock is never held for the generator's lifetime.
            if not _action_lock.acquire(blocking=False):
                return self._audited(429, "DENIED-BUSY", verb, slug, "another action is in progress")
            try:
                # 0 = paused, 1 = running; every other rc denies EVERY verb (fail-closed).
                pq = subprocess.run(["bash", GATE, "paused"], capture_output=True, timeout=15).returncode
                if pq not in (0, 1):
                    code, result, msg = (503, "DENIED-PAUSE-UNKNOWN",
                                         f"pause state UNKNOWN (gate rc={pq}) — refusing every verb")
                elif pq == 0 and verb != "resume":
                    code, result, msg = (409, "DENIED-PAUSED",
                                         "fleet paused — only 'resume' is allowed")
                else:
                    code, result, msg = self._execute(verb, slug, body)
            finally:
                _action_lock.release()
            if self._has_intent:
                return self._reply(code, result == "OK", msg)
            return self._audited(code, result, verb, slug, msg)
        except AuditError:
            return self._reply(500, False, "audit persistence failed — action not started")
        except Exception as e:  # daemon must not die on a bad request
            if self._has_intent:
                # The durable intent remains an attempt, including on partial effects.
                # A second audit write could fail after an irreversible spawn.
                return self._reply(500, False, str(e)[:200])
            return self._audited(500, "ERROR", verb, slug, str(e)[:200])

    def _execute(self, verb, slug, body):
        global _rerun_process
        root = self.directory
        if verb == "rerun-viz":
            if _rerun_process is not None and _rerun_process.poll() is None:
                return 429, "DENIED-BUSY", "generator is still running"
            now = time.monotonic()
            if _last_rerun[0] is not None:
                gap = now - _last_rerun[0]
                if gap < RERUN_MIN_GAP_S:
                    return 429, "DENIED-RATE", f"rerun ran {int(gap)}s ago — min gap {RERUN_MIN_GAP_S}s"
            # Same stable inode as the scheduled `flock -n -o` wrapper. Pass it only
            # to the generator: closing our copy (without LOCK_UN) preserves its lock
            # across server restarts, and child exit releases it without a watcher.
            self._intent(verb, slug, "regeneration requested")
            generator = os.path.join(REPO, "generate-loop-viz.py")
            # The child never needs the action token. Harness mode regenerates the data dir
            # only: its own lock under the served root, -I, cwd outside REPO (contract §1).
            env = {k: v for k, v in os.environ.items() if k != "LOOP_DECK_ACT_TOKEN"}
            if HARNESS:
                lock_path, cmd, cwd = os.path.join(root, ".rerun.lock"), [sys.executable, "-I", generator], DATA_DIR
                env.update(LOOP_DECK_MODE="harness", LOOP_DECK_DATA_DIR=DATA_DIR)
            else:
                lock_path, cmd, cwd = os.path.join(REPO, ".refresh.lock"), [sys.executable, generator], REPO
            with open(lock_path, "a") as generation_lock:
                try:
                    fcntl.flock(generation_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return 429, "DENIED-BUSY", "generation or refresh is already running"
                with open(os.path.join(root, "rerun.log"), "ab") as lg:
                    _rerun_process = subprocess.Popen(
                        cmd, stdout=lg, stderr=subprocess.STDOUT, cwd=cwd, env=env,
                        close_fds=True, pass_fds=(generation_lock.fileno(),))
                _last_rerun[0] = now
            return 202, "OK", "regeneration started — data refreshes in a few seconds"
        if verb == "pause":
            reason = re.sub(r"[^\w .,:/#-]", "", str(body.get("reason", "")))[:200]
            self._intent(verb, slug, "pause requested: " + reason)
            os.makedirs(os.path.dirname(PAUSE_FILE), exist_ok=True)
            # Preserve a pause written externally after the gate query.
            with open(PAUSE_FILE, "x") as f:
                f.write(json.dumps({"ts": utcnow(), "via": "deck", "reason": reason}) + "\n")
                f.flush()
                os.fsync(f.fileno())
            sync_directory(os.path.dirname(PAUSE_FILE))
            return 200, "OK", "LOOP_PAUSE set — every loop exits at its next cycle"
        if verb == "resume":
            self._intent(verb, slug, "resume requested; pause state will be rechecked")
            arch = None
            if os.path.exists(PAUSE_FILE):
                prefix = PAUSE_FILE + ".released." + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                for _ in range(10):
                    arch = prefix + "." + uuid.uuid4().hex
                    try:
                        # Reserve only an unused name (O_EXCL); existing archives
                        # cannot become replace targets, even on a UUID collision.
                        with open(arch, "x"):
                            pass
                        break
                    except FileExistsError:
                        continue
                else:
                    raise OSError("could not allocate a unique pause archive")
                # Replace our own placeholder atomically. Never unlink PAUSE_FILE:
                # an external writer may have replaced it with a new emergency pause.
                os.replace(PAUSE_FILE, arch)
                sync_directory(os.path.dirname(PAUSE_FILE))
            pq = subprocess.run(["bash", GATE, "paused"], capture_output=True, timeout=15).returncode
            if pq not in (0, 1):
                return 503, "DENIED-PAUSE-UNKNOWN", f"pause state UNKNOWN after resume (gate rc={pq})"
            if pq == 0:
                source = "LOOP_PAUSE" if os.path.exists(PAUSE_FILE) else "STATE.md"
                if source == "STATE.md":
                    return (409, "DENIED-STATE-PAUSED",
                            "still paused via STATE.md — resume cannot clear a STATE pause; "
                            "an operator edits STATE.md")
                return 409, "DENIED-PAUSED", f"still paused via {source}"
            if arch is None:
                return 200, "OK", "fleet already running (no pause file, STATE.md not paused)"
            return 200, "OK", f"resumed — pause file archived as {os.path.basename(arch)}"
        if verb == "ack":
            if not (slug and SLUG_RE.match(slug)):
                return 400, "DENIED-SLUG", "slug must match [a-z0-9-]{1,64}"
            try:
                with open(os.path.join(root, "status.json")) as f:
                    registry = json.load(f)
                if not isinstance(registry, dict) or not isinstance(registry.get("loops"), list):
                    raise ValueError("invalid registry")
                known = set()
                for row in registry["loops"]:
                    entry = row.get("slug") if isinstance(row, dict) else None
                    if not isinstance(entry, str) or not SLUG_RE.fullmatch(entry) or entry in known:
                        raise ValueError("invalid registry slug")
                    known.add(entry)
            except (OSError, ValueError):
                return 400, "DENIED-REGISTRY", "current status.json is unavailable or invalid"
            if slug not in known:
                return 400, "DENIED-SLUG", f"unknown loop slug {slug!r}"
            self._intent(verb, slug, "acknowledgement requested")
            self._append_jsonl(os.path.join(root, "acks.jsonl"),
                               {"ts": utcnow(), "e": int(time.time()), "slug": slug, "via": "deck"})
            return 200, "OK", f"acknowledged {slug} — chip appears at the next data refresh (evidence-graded, not optimistic), stays 24h"
        return 400, "DENIED-VERB", "unreachable"

    def _append_jsonl(self, path, obj):
        with open(path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            size = os.fstat(f.fileno()).st_size
            if size and os.pread(f.fileno(), 1, size - 1) != b"\n":
                # A failed append may have left a prefix. Preserve it rather
                # than joining a new intent to an unparseable JSONL record.
                raise OSError("unterminated JSONL tail — refusing to append")
            f.write(json.dumps(obj, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
            sync_directory(os.path.dirname(path))

    def _audit(self, code, result, verb, slug, msg):
        try:
            self._append_jsonl(os.path.join(self.directory, "actions.jsonl"),
                               {"ts": utcnow(), "verb": verb, "slug": slug, "result": result,
                                 "http": code, "client": self.client_address[0], "msg": msg[:200]})
        except OSError as e:
            raise AuditError("audit persistence failed") from e

    def _intent(self, verb, slug, msg):
        self._audit(None, "INTENT", verb, slug, msg)
        self._has_intent = True

    def _audited(self, code, result, verb, slug, msg):
        try:
            self._audit(code, result, verb, slug, msg)
        except AuditError:
            return self._reply(500, False, "audit persistence failed — action not started")
        return self._reply(code, result == "OK", msg)

    def _reply(self, code, ok, msg):
        body = json.dumps({"ok": ok, "msg": msg}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass


def main():
    ap = argparse.ArgumentParser(description="Loop Deck same-origin server (static + gated /act)")
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--root", default=os.path.join(REPO, "out"))
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    if HARNESS and not (os.path.isabs(DATA_DIR) and
                        os.path.realpath(root) == os.path.realpath(os.path.join(DATA_DIR, "out"))):
        sys.exit("deck-server: harness mode needs --root \"$LOOP_DECK_DATA_DIR/out\" with an absolute "
                 "LOOP_DECK_DATA_DIR (rerun-viz must never regenerate another tree)")
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), partial(Handler, directory=root))
    assert srv.server_address[0] == "127.0.0.1", "loopback bind is a hard rule"
    print(f"deck-server {VERSION} on 127.0.0.1:{a.port} root={root} "
          f"mode={'harness' if HARNESS else 'fleet'} actions={'ON' if ACT_TOKEN else 'OFF'}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
