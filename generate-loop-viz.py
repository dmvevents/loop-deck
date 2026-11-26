#!/usr/bin/env python3
"""generate-loop-viz.py — loop-registry data emitter + loop-sync drift detector (v2, "Loop Deck").

Reads (all local, read-only):
  LOOP.md            — the registry (Active Loops group tables A–D)
  crontab -l         — live schedule ground truth
  /var/log/syslog(.1)— last-fire + fire-history evidence (CRON "CMD (...)" lines, verbatim match)
  log mtimes         — fallback evidence (quiet scripts append nothing — mtime alone is NOT death)
  loop-ledger.json   — circuit-breaker state
  tmux ls            — liveness for hub-driven session loops
  out/status.json — previous session expectations for the same registry rows
  origin/master STATE.md — "Last run:" header for the remote agentic-fixer loop (fetched, never
                       the possibly-lagging local checkout; local file is the offline fallback)

Atomically writes out/status.json (schema 3) and APPENDS out/history.jsonl (one line per run,
rotated at ~5000 lines). Also reads the instrumentation and A2 action ledgers next to it — runs.jsonl (exit
codes via tools/run-logged.sh), acks.jsonl + actions.jsonl (deck-server /act), escalations.jsonl
(tools/escalate-log.sh) — plus loop-budget.json; absent ledgers surface as honest empty states.
The SPA in app/ renders these; a refresh script copies the app into out/ and owns the
127.0.0.1:8091 deck-server.py (static + gated POST /act). Exit 3 if REGISTRY-DRIFT, else 0.

LOOP_DECK_MODE=harness (docs/CONTRACT.md) replaces every input above with
$LOOP_DECK_DATA_DIR/{registry.json,runs.jsonl,loop-ledger.json,state.json}, writes only under
$LOOP_DECK_DATA_DIR/out/, and runs no probe at all (no crontab, syslog, tmux, pgrep, git).
Exit 2 = input missing/unreadable/malformed (nothing written), 3 = REGISTRY-DRIFT, else 0.
"""
import io, json, os, re, subprocess, sys, tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(REPO, "out")
NOW = datetime.now(timezone.utc)
WINDOW_MIN = 24 * 60          # metrics window (capped by syslog coverage below)
HISTORY_KEEP, HISTORY_CAP = 5000, 6000

SEV = {"RED": 5, "STALE": 4, "UNKNOWN": 4, "GREEN": 3, "LIVE": 3, "SCHED": 2, "NA": 1,
       "NO-DATA": 1, "DISABLED": 0}  # last two: harness mode only (never emitted in fleet mode)
STATE_SRC = ["STATE.md"]  # set in main(); read by status_for_row for the fixer evidence label
LETTER = {"GREEN": "G", "LIVE": "L", "STALE": "S", "RED": "R", "SCHED": "C", "NA": "N",
          "UNKNOWN": "U", "NO-DATA": "E", "DISABLED": "D"}  # E, D: harness mode only
# status palette (validated: dataviz validate_palette.py, dark surface — see the Deck design study)
CHIP = {"GREEN": "#2ea043", "LIVE": "#2ea043", "STALE": "#e3b341", "RED": "#f85149",
        "SCHED": "#4493f8", "NA": "#8b949e", "UNKNOWN": "#8b949e"}

def sh(argv):
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, timeout=20).stdout  # argv list, no shell
    except Exception:
        return ""

def pgrep_first(pattern):
    """First PID whose full command line matches the ERE PATTERN; "" when none; None when the probe itself is
    unavailable (no pgrep binary, timeout), so the caller reports UNKNOWN rather than a dead daemon.

    No shell on purpose: sh(f"pgrep -f {p} | head -1") always found the pipeline's own
    `/bin/sh -c "pgrep -f <p> | head -1"` parent, so every @reboot row read "daemon alive"
    while no such daemon ran (measured 2026-10-13; the test that pins it stays with the source repository)."""
    try:
        out = subprocess.run(["pgrep", "-f", "--", pattern], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    pids = out.split()
    return pids[0] if pids else ""

_ERE_SPECIAL = re.compile(r"([.^$*+?()\[\]{}|\\])")
_SHELL_OPS = ("|", "&", ";", ">", "<", "2>")

def reboot_pattern(tok, cmd):
    """ERE for an @reboot daemon's argv: the script's basename plus its first argument from the crontab command,
    when that argument is not a shell operator or redirection. `daemon.py view` must not stand in
    for the `daemon.py monitor` daemon. A bare basename still matches any argv that
    carries it, so the row detail names the pattern it matched."""
    base = tok.split("/")[-1]
    words = cmd.split()
    nxt = ""
    for i, w in enumerate(words):
        if w == tok or w == base or w.endswith("/" + base):
            if i + 1 < len(words) and not words[i + 1].startswith(_SHELL_OPS):
                nxt = words[i + 1]
            break
    pat = _ERE_SPECIAL.sub(r"\\\1", base)
    return pat + "[[:space:]]+" + _ERE_SPECIAL.sub(r"\\\1", nxt) if nxt else pat

def observe_tmux_sessions():
    """None means observation unavailable; a successful empty set means no sessions."""
    try:
        result = subprocess.run(["tmux", "ls", "-F", "#{session_name}"],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return set(result.stdout.split()) if result.returncode == 0 else None

def strip_md(s):
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    return s.strip()

def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

# ---------- parse LOOP.md active-loop group tables ----------
def parse_registry():
    text = open(os.path.join(REPO, "LOOP.md")).read()
    active = text.split("## Archived Loops")[0]
    groups, cur = [], None
    for line in active.splitlines():
        m = re.match(r"^### ([A-D])\. (.+)$", line)
        if m:
            cur = {"key": m.group(1), "title": strip_md(m.group(2)), "rows": []}
            groups.append(cur)
            continue
        if cur is None or not line.startswith("|"):
            continue
        cells = [strip_md(c) for c in line.strip().strip("|").split("|")]
        if len(cells) < 7 or cells[0] in ("Loop",) or set(cells[0]) <= {"-", " "}:
            continue
        cur["rows"].append(dict(zip(
            ["name", "driver", "cadence", "pattern", "aut", "verify", "handoff"], cells[:7])))
    return groups

# ---------- syslog: full fire history per cron payload (not just the last fire) ----------
def syslog_fires():
    fires, coverage_start = {}, None
    for path in ("/var/log/syslog.1", "/var/log/syslog"):
        try:
            with open(path, errors="replace") as f:
                for line in f:
                    if " CRON[" not in line or " CMD (" not in line:
                        continue
                    ts = line.split()[0]
                    payload = line.split(" CMD (", 1)[1].rstrip()
                    if payload.endswith(")"):
                        payload = payload[:-1]
                    try:
                        t = datetime.fromisoformat(ts).astimezone(timezone.utc)
                    except ValueError:
                        continue
                    if coverage_start is None or t < coverage_start:
                        coverage_start = t
                    fires.setdefault(payload.strip(), []).append(int(t.timestamp()))
        except OSError:
            continue
    for k in fires:
        fires[k].sort()
    return fires, coverage_start

def parse_crontab(fires):
    entries = []
    for line in sh(["crontab", "-l"]).splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("@"):
            fields, cmd = [s.split()[0]], " ".join(s.split()[1:])
        else:
            parts = s.split(None, 5)
            if len(parts) < 6:
                continue
            fields, cmd = parts[:5], parts[5]
        # real redirect targets only: skip fd-dups like 2>&1 (checker: '2>&1 > /path'
        # ordering or a bare '2>&1' must not capture '&1' as a "log path")
        log = None
        for lm in re.finditer(r"(?<![\d>])>>?\s*(\S+)", cmd):
            tgt = lm.group(1)
            if tgt.startswith("&") or tgt == "/dev/null":
                continue
            log = tgt if tgt.startswith("/") else \
                os.path.join(os.path.expanduser("~"), tgt)  # cron runs from $HOME
            break
        ep = fires.get(cmd.strip(), [])
        fired_min = (NOW.timestamp() - ep[-1]) / 60.0 if ep else None
        entries.append({"fields": fields, "cmd": cmd, "log": log, "raw": s,
                        "epochs": ep, "fired_min": fired_min, "registered": False})
    return entries

def period_minutes(fields):
    if fields[0].startswith("@"):
        return None  # @reboot daemons have no period
    minute, hour = fields[0], fields[1]
    dow = fields[4] if len(fields) > 4 else "*"
    m = re.match(r".*\*/(\d+)$", minute) or re.match(r".*-\d+/(\d+)$", minute)
    if minute == "*":
        return 1
    if m:
        return int(m.group(1))
    n_min = len(minute.split(","))
    if hour == "*":
        return max(1, 60 // n_min)
    mh = re.match(r"\*/(\d+)$", hour)
    if mh:
        return int(mh.group(1)) * 60
    if dow != "*":
        return 10080
    return 1440

def age_minutes(path):
    try:
        return (NOW.timestamp() - os.stat(path).st_mtime) / 60.0
    except OSError:
        return None

def fmt_age(mins):
    if mins is None:
        return "no log"
    if mins < 90:
        return f"{int(mins)}m"
    if mins < 48 * 60:
        return f"{mins/60:.1f}h"
    return f"{mins/1440:.1f}d"

def read_jsonl(path, cap=None):
    """Tolerant reader for the runtime ledgers — one bad line never drops the file."""
    rows = []
    try:
        with open(path) as f:
            for ln in f:
                try:
                    rows.append(json.loads(ln))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows[-cap:] if cap else rows

# ---------- matching registry rows to cron lines ----------
SCRIPT_RE = re.compile(r"((?:[\w.~-]+/)*)([\w-]+\.(?:sh|py))")

def row_tokens(driver):
    toks = set()
    for m in SCRIPT_RE.finditer(driver):
        pre, base = m.group(1), m.group(2)
        parts = [p for p in pre.split("/") if p and p not in ("~", ".")]
        toks.add(f"{parts[-1]}/{base}" if parts else base)
    for kw in ("logrotate", "s3 sync"):
        if kw in driver.lower():
            toks.add(kw)
    return toks

def tok_matches(tok, cmd):
    if tok in ("logrotate", "s3 sync"):
        return tok in cmd
    return re.search(r"(?<![\w.-])" + re.escape(tok), cmd) is not None

def cron_metrics(c, window_min):
    """Punctuality within the effective evidence window (launch evidence only — syslog CMD).
    The expectation window is clamped to the cron's FIRST fire ever seen in syslog: a cron
    installed an hour ago must not be charged 'missed fires' for the 23 hours before it existed
    (full syslog coverage is ~2 days, so a first-fire inside the window means genuinely new —
    or dead long enough that the staleness status already says RED)."""
    per = period_minutes(c["fields"])
    cutoff = NOW.timestamp() - window_min * 60
    win_start = max(cutoff, c["epochs"][0]) if c["epochs"] else cutoff
    eff_min = max(1.0, (NOW.timestamp() - win_start) / 60.0)
    in_win = [e for e in c["epochs"] if e >= cutoff]
    m = {"sched": " ".join(c["fields"]), "cmd": c["cmd"], "log": c["log"],
         "period_min": per, "last_fire_epoch": c["epochs"][-1] if c["epochs"] else None,
         "age_min": round(c["fired_min"], 1) if c["fired_min"] is not None else None,
         "fires_window": len(in_win), "fire_epochs": in_win[-400:],
         "observed_since": int(win_start) if c["epochs"] else None,
         "expected": None, "missed": None, "punctuality_pct": None}
    # judge punctuality only after >=2 full periods of evidence — a brand-new cron would
    # otherwise pin at 100% (floor + clamp) before it has any track record (checker finding).
    # Zero fires ever => NO expectation either: the window can't clamp to a first fire, so a
    # line installed 20 minutes ago would be charged a full day of "missed" (found when
    # two freshly installed crons showed 60 phantom misses).
    if per and c["epochs"] and eff_min >= 2 * per:
        exp = int(eff_min // per)
        m["expected"] = exp
        m["missed"] = max(0, exp - len(in_win))
        m["punctuality_pct"] = min(100, round(100.0 * len(in_win) / exp)) if exp else None
    return m

def cron_status(tok, c):
    per = period_minutes(c["fields"]) or 1440
    if c["fields"][0] == "@reboot":
        pat = reboot_pattern(tok, c["cmd"])          # shell-free probe; argv = basename + first argument
        pid = pgrep_first(pat)
        if pid is None:
            return ("UNKNOWN", f"{tok}: @reboot liveness not observable (pgrep unavailable)")
        return ("GREEN", f"{tok}: daemon alive (pid {pid}; argv matches /{pat}/)") if pid else \
               ("RED", f"{tok}: @reboot daemon NOT running (no argv matches /{pat}/)")
    age, src = c["fired_min"], "syslog"
    if age is None:
        age, src = (age_minutes(c["log"]) if c["log"] else None), "log mtime"
    if age is None:
        return ("SCHED", f"{tok}: scheduled; no fire evidence readable (no syslog hit, no log)")
    if age <= max(2 * per, 10):
        return ("GREEN", f"{tok}: fired {fmt_age(age)} ago [{src}] (period {per}m)")
    if age <= max(6 * per, 30):
        return ("STALE", f"{tok}: last fire {fmt_age(age)} ago [{src}] (period {per}m)")
    return ("RED", f"{tok}: last fire {fmt_age(age)} ago [{src}] (period {per}m)")

def named_sessions(driver):
    sess = re.findall(r"tmux `?(\w[\w-]*)`?", driver) or re.findall(r"session `?(\w[\w-]*)`?", driver)
    return set(re.findall(r"`([\w-]+)`", driver)) | set(sess)

def previous_loops():
    """Expectations belong to the same registry identity in the last complete snapshot."""
    try:
        with open(os.path.join(OUT, "status.json")) as f:
            previous = json.load(f)
    except (OSError, ValueError):
        return {}
    rows = previous.get("loops") if isinstance(previous, dict) else None
    if not isinstance(rows, list):
        return {}
    return {(r["slug"], r["group"], r["driver"]): r for r in rows
            if isinstance(r, dict) and
            all(isinstance(r.get(k), str) for k in ("slug", "group", "driver"))}

def session_evidence(row, tmux_sessions, previous):
    named = named_sessions(row["driver"])
    def stored(key):
        values = previous.get(key)
        return {s for s in values if isinstance(s, str)} if isinstance(values, list) else set()
    expected = stored("expected_sessions") & named
    if previous.get("status") == "LIVE" and "expected_sessions" not in previous:
        # Legacy LIVE meant at least one named session was observed. With several
        # names, only the observation detail identifies which ones were actually up.
        if len(named) == 1:
            expected |= named
        elif isinstance(previous.get("detail"), list):
            prefix = "tmux session(s) up: "
            for detail in previous["detail"]:
                if isinstance(detail, str) and detail.startswith(prefix):
                    expected |= set(detail[len(prefix):].split(", ")) & named
    if tmux_sessions is None:
        missing = stored("missing_sessions") & expected
    else:
        expected |= named & tmux_sessions
        missing = expected - tmux_sessions
    return {"expected_sessions": sorted(expected), "missing_sessions": sorted(missing)}

def status_for_row(row, crons, tmux_sessions, state_last_run, window_min, sessions=None):
    name, driver = row["name"], row["driver"]
    matched, statuses, details = [], [], []
    for tok in sorted(row_tokens(driver)):
        for c in crons:
            if tok_matches(tok, c["cmd"]):
                c["registered"] = True
                st, d = cron_status(tok, c)
                statuses.append(st); details.append(d)
                matched.append(cron_metrics(c, window_min) | {"token": tok, "status": st})
    if not matched:
        if "agentic-fixer" in name and state_last_run:
            age = (NOW - state_last_run).total_seconds() / 60
            st = "GREEN" if age <= 120 else ("STALE" if age <= 360 else "RED")
            return st, [f"Last run {fmt_age(age)} ago [{STATE_SRC[0]}] (hourly loop)"], False, []
        named = named_sessions(driver)
        sessions = sessions if sessions is not None else session_evidence(row, tmux_sessions, {})
        if sessions["missing_sessions"]:
            detail = "DRIFT: expected tmux session(s) missing: " + ", ".join(sessions["missing_sessions"])
            if tmux_sessions is None:
                detail += " (last confirmed missing; current tmux observation UNKNOWN)"
            return "RED", [detail], True, []
        if named and tmux_sessions is None:
            return "UNKNOWN", ["tmux observation UNKNOWN; session liveness unavailable"], False, []
        live = sorted(named & (tmux_sessions or set()))
        if live:
            return "LIVE", [f"tmux session(s) up: {', '.join(live)}"], False, []
        if any(k in driver.lower() for k in ("tmux", "hub", "claude session", "per-invocation")):
            return "NA", ["hub/session-driven — no cron to check"], False, []
        cronish = bool(re.search(r"(\*/|per-min|hourly|:\d\d|Mon|@reboot|\d+,\d+)", row["cadence"]))
        if cronish:
            return "RED", ["DRIFT: registry says cron-backed but no crontab entry matches"], True, []
        return "NA", ["no mechanical signal defined"], False, []
    worst = max(statuses, key=lambda s: SEV[s])
    fresh = sum(1 for s in statuses if s in ("GREEN", "SCHED"))
    if len(statuses) > 1:
        details.insert(0, f"{fresh}/{len(statuses)} drivers fresh")
    return worst, details, False, matched

# ---------- ledger ----------
def ledger_for(toks, ledger):
    """Exact-ish only: a ledger key must contain a driver script's base name. No fuzzy matches."""
    bases = {t.split("/")[-1].rsplit(".", 1)[0] for t in toks if "/" in t or "." in t}
    for key, e in ledger.items():
        if any(b and b in key for b in bases):
            return {"item": key, "attempts": e.get("attempts", 0), "max": 3,
                    "resolved": bool(e.get("resolved"))}
    return None

# ---------- mermaid topology ----------
def mermaid_overview(groups):
    lines = ["flowchart TB", '  HUB["orchestrator hub"]']
    cls = {k: [] for k in CHIP}
    for g in groups:
        lines.append(f'  subgraph G{g["key"]}["{g["key"]}. {g["title"][:46]}"]')
        for i, r in enumerate(g["rows"]):
            nid = f'{g["key"]}{i}'
            label = r["name"][:30].replace('"', "'")
            lines.append(f'    {nid}["{label}<br/><small>{r["cadence"][:18]}</small>"]')
            cls[r["_status"]].append(nid)
        lines.append("  end")
        lines.append(f'  HUB --> G{g["key"]}')
    for st, fg in CHIP.items():
        lines.append(f"  classDef c{st} fill:{fg}22,stroke:{fg},color:#e6edf3;")
        if cls[st]:
            lines.append(f'  class {",".join(cls[st])} c{st};')
    return "\n".join(lines)

ANATOMY = """flowchart LR
  S["Schedule / cron"] --> T["Triage (skill)"] --> ST["STATE.md / memory"] --> W["Worktree"]
  W --> M["Maker sub-agent"] --> V["Verifier sub-agent<br/>(tests + loop-gate)"] --> G{"Human gate?"}
  G -->|"safe / allowlisted"| C["commit / PR / act"]
  G -->|"risky / gate.yaml"| E["escalate w/ context"]
  C --> P["Persist + prune STATE"] --> S"""

# ---------- STATE.md prune ledger ----------
def track_state_prunes(state_txt):
    """STATE.md is pruned aggressively (State-Rot discipline) — pruning must leave EVIDENCE
    beyond git archaeology (an operator requirement). Each run snapshots origin STATE.md;
    any line present last run but absent now (pruned OR edited — the old version) is appended
    to state-prunes.jsonl. Append-only; snapshot replaced atomically; first run only seeds."""
    snap = os.path.join(OUT, "state-head.snapshot")
    ledger = os.path.join(OUT, "state-prunes.jsonl")
    cur = [l for l in state_txt.splitlines() if l.strip()]
    removed = []
    try:
        old = open(snap).read().splitlines()
    except OSError:
        old = None
    if old is not None:
        curset = set(cur)
        removed = [l for l in old if l.strip() and l not in curset]
        if removed:
            import fcntl
            with open(ledger, "a") as f:
                fcntl.flock(f, fcntl.LOCK_EX)  # review finding: match actions.jsonl discipline
                for l in removed:
                    f.write(json.dumps({"ts": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                        "e": int(NOW.timestamp()), "line": l[:1500]},
                                       ensure_ascii=False) + "\n")
    tmp = snap + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(cur) + "\n")
    os.replace(tmp, snap)
    return removed

# ---------- history ----------
@contextmanager
def atomic_output(path):
    """Publish a complete file using a unique temporary file on the same filesystem."""
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".",
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            yield f
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass

def append_history(line_obj):
    """Kill-safe by construction (review finding): the normal path is a PURE
    APPEND (never truncates — a crash mid-write loses at most the newest line); rotation
    (once per ~52 days) writes a tempfile then os.replace (atomic — old or new, never a
    truncated file). A separate, never-replaced lock covers open/read/append/rotation:
    waiting writers must open the data pathname only AFTER acquiring that stable lock."""
    import fcntl
    path = os.path.join(OUT, "history.jsonl")
    line = json.dumps(line_obj, separators=(",", ":"))
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open(path, "a+") as f:
            f.seek(0)
            lines = f.read().splitlines()
            lines.append(line)
            if len(lines) > HISTORY_CAP:
                lines = lines[-HISTORY_KEEP:]
                with atomic_output(path) as t:
                    t.write("\n".join(lines) + "\n")
            else:
                f.write(line + "\n")   # "a+" mode: writes always append
    return len(lines)

# ---------- harness mode (LOOP_DECK_MODE=harness; docs/CONTRACT.md) ----------
HARNESS_INPUTS = ("registry.json", "runs.jsonl", "loop-ledger.json", "state.json")
HARNESS_SPA = ("index.html", "app.css", "app.js")   # copied into out/ like the fleet's refresh script (not shipped) does
# contract §3.2 (owner ruling 2026-10-13): periodic row with latest rc 0 is GREEN at age <= 2x cadence_s,
# STALE up to 6x, RED "missed" beyond; no minute floors (cadence_s is explicit)
HARNESS_FRESH_FACTOR = 2
HARNESS_MISSED_FACTOR = 6
VALIDATOR = os.path.join(REPO, "tools", "check-contract.py")

class HarnessError(Exception):
    """A contract violation or unreadable input: one line on stderr, exit 2, nothing written."""

def harness_data_dir():
    d = os.environ.get("LOOP_DECK_DATA_DIR", "")
    if not d or not os.path.isabs(d):
        raise HarnessError("LOOP_DECK_DATA_DIR must be set to an absolute path in harness mode")
    if not os.path.isdir(d) or not os.access(d, os.R_OK | os.X_OK):
        raise HarnessError(f"LOOP_DECK_DATA_DIR {d!r} is missing or unreadable")
    return d

def load_validator():
    """The contract's own validator: the generator reuses its rules so the two cannot drift."""
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location("loop_deck_contract", VALIDATOR)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except (OSError, ImportError, AttributeError, SyntaxError) as e:
        raise HarnessError(f"contract validator unavailable at {VALIDATOR}: {e}")
    return mod

def harness_snapshot(data_dir, snap):
    """Copy each present input once, so the bytes validated are the bytes rendered."""
    for name in HARNESS_INPUTS:
        try:
            with open(os.path.join(data_dir, name), "rb") as f:
                body = f.read()
        except FileNotFoundError:
            continue
        except OSError as e:
            raise HarnessError(f"{name} is unreadable: {e.strerror or e}")
        with open(os.path.join(snap, name), "wb") as f:
            f.write(body)

def harness_epoch(ts, where):
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        raise HarnessError(f"{where}: {ts!r} is not a real UTC time")

def harness_inputs(data_dir):
    """Validate a one-time snapshot with the contract validator; return the parsed inputs.
    Shape errors raise HarnessError; an unclaimed run tag is returned (REGISTRY-DRIFT, exit 3)."""
    V = load_validator()
    with tempfile.TemporaryDirectory(prefix=".inputs-", dir=OUT) as snap:   # writes stay in out/
        harness_snapshot(data_dir, snap)
        p = {n: os.path.join(snap, n) for n in HARNESS_INPUTS}
        try:
            registry = V._load_json(p["registry.json"], "registry.json")
            tags = V.check_registry(registry)
            _, unclaimed = V.check_runs(p["runs.jsonl"], tags)
            slugs = set(tags.values())   # the validator's own derivation (validate())
            has_ledger = V.check_ledger(p["loop-ledger.json"], slugs) is not None
            has_state = V.check_state(p["state.json"]) is not None
            with open(p["runs.jsonl"]) if os.path.exists(p["runs.jsonl"]) else io.StringIO() as f:
                runs = [json.loads(ln) for ln in f.read().splitlines()]
            ledger = json.load(open(p["loop-ledger.json"])) if has_ledger else None
            state = json.load(open(p["state.json"])) if has_state else None
        except V.Violation as e:
            raise HarnessError(f"contract violation: {e}")
        except (OSError, UnicodeDecodeError) as e:
            raise HarnessError(f"input unreadable: {e}")
    for i, r in enumerate(runs, 1):
        r["_e"] = harness_epoch(r["ts"], f"runs.jsonl:{i} ts")
    for key, it in ((ledger or {}).get("items") or {}).items():
        if "last_ts" in it:
            harness_epoch(it["last_ts"], f"loop-ledger.json items[{key!r}] last_ts")
    if state is not None:
        harness_epoch(state["ts"], "state.json ts")
    return registry, tags, runs, sorted(unclaimed), ledger, state

def harness_row_state(row, recs, now_e):
    """Contract §3.2 run-derived state -> (status, detail, missed). recs: this row's records
    ordered by ts (stable sort: equal ts keep file order), never by file position alone."""
    if not row["enabled"]:
        return "DISABLED", ["disabled in registry.json (enabled: false); run records ignored"], None
    if not recs:
        return "NO-DATA", ["no runs recorded yet"], None
    last = recs[-1]
    if row["kind"] == "seed":   # one-shot: any age counts, never STALE or missed
        if last["rc"] != 0:
            return "RED", [f"run-FAILURE: last rc={last['rc']} at {last['ts']}"], False
        return "GREEN", [f"seeded {last['ts']} (rc 0; one-shot, never missed)"], False
    age, cad = now_e - last["_e"], row["cadence_s"]
    missed = age > HARNESS_MISSED_FACTOR * cad   # "missed" = the RED band only (owner ruling P1)
    if last["rc"] != 0:
        return "RED", [f"run-FAILURE: last rc={last['rc']} at {last['ts']}"], missed
    if age <= HARNESS_FRESH_FACTOR * cad:
        return "GREEN", [f"last run rc 0 {fmt_age(age / 60)} ago (cadence {cad}s)"], False
    if not missed:
        return "STALE", [f"last run rc 0 {fmt_age(age / 60)} ago, beyond {HARNESS_FRESH_FACTOR}x "
                         f"cadence {cad}s"], False
    return "RED", [f"missed: no run for {fmt_age(age / 60)}, beyond {HARNESS_MISSED_FACTOR}x "
                   f"cadence {cad}s (last rc 0 at {last['ts']})"], True

def harness_mermaid(rows):
    """Registry nodes + depends_on edges only: no hub node, no invented edge."""
    ids = {r["slug"]: f"n{i}" for i, r in enumerate(rows)}
    lines, cls = ["flowchart LR"], {}
    for r in rows:
        label = re.sub(r'[<>"\[\]{}|`]', "", r["name"])[:30]
        lines.append(f'  {ids[r["slug"]]}["{label}<br/><small>{r["status"]}</small>"]')
        cls.setdefault(r["status"], []).append(ids[r["slug"]])
    for r in rows:
        for d in r.get("depends_on") or []:
            lines.append(f"  {ids[d]} --> {ids[r['slug']]}")
    for st, nodes in cls.items():
        name, fg = "c" + re.sub(r"[^A-Z]", "", st), CHIP.get(st, "#8b949e")
        lines.append(f"  classDef {name} fill:{fg}22,stroke:{fg},color:#e6edf3;")
        lines.append(f'  class {",".join(nodes)} {name};')
    return "\n".join(lines)

def _generate_harness(data_dir):
    """One harness cycle under main()'s generation lock: the four inputs only, zero probes."""
    registry, tags, runs, unclaimed, ledger, state = harness_inputs(data_dir)
    now_e, day_ago = NOW.timestamp(), NOW.timestamp() - 24 * 3600
    by_slug = {}
    for r in sorted(runs, key=lambda r: r["_e"]):          # order by ts (contract: fire END)
        if r["tag"] in tags:
            by_slug.setdefault(tags[r["tag"]], []).append(r)
    max_att = ledger["max_attempts"] if ledger else None
    items = (ledger or {}).get("items", {})
    titles = {"loop": "Loops", "watch": "Watches", "sync": "Syncs", "seed": "Seeds (one-shot)"}
    loops, missed_total = [], 0
    for row in registry["loops"]:
        recs = by_slug.get(row["slug"], [])
        st, det, missed = harness_row_state(row, recs, now_e)
        l = {"slug": row["slug"], "name": row["name"], "group": row["kind"],
             "group_title": titles[row["kind"]], "driver": row["owner"],
             "cadence": f'{row["cadence_s"]}s' if row["cadence_s"] else "one-shot",
             "pattern": row["kind"], "aut": "", "verify": "", "handoff": "",
             "status": st, "detail": det, "crons": [], "expected_sessions": [],
             "missing_sessions": [], "kind": row["kind"], "cadence_s": row["cadence_s"],
             "enabled": row["enabled"], "tags": row["tags"],
             "depends_on": row.get("depends_on", []), "runbook_md": row.get("runbook_md", ""),
             "missed": missed, "ledger": None, "breaker": False}
        if recs and row["enabled"]:
            win = [r for r in recs if r["_e"] >= day_ago]
            ok = sum(1 for r in win if r["rc"] == 0)
            l["run_success"] = {"runs": len(win), "ok": ok,
                                "last_rc": win[-1]["rc"] if win else None,
                                "last_ts": win[-1]["ts"] if win else None,
                                "success_pct": round(100.0 * ok / len(win)) if win else None}
            last = recs[-1]
            l["last_run"] = {"ts": last["ts"], "rc": last["rc"], "dur_s": last["dur_s"],
                             "note": last.get("note")}
            missed_total += 1 if missed else 0
        own = sorted((k for k in items if k.startswith(row["slug"] + ":")),
                     key=lambda k: (-items[k]["attempts"], k))
        if own:
            e = items[own[0]]
            # contract §3.3: breaker = attempts >= the file's max_attempts (no "resolved" field)
            l["ledger"] = {"item": own[0], "attempts": e["attempts"], "max": max_att,
                           "resolved": False}
            l["breaker"] = e["attempts"] >= max_att
        loops.append(l)
    return _publish_harness(registry, runs, unclaimed, ledger, state, loops, missed_total)

def _publish_harness(registry, runs, unclaimed, ledger, state, loops, missed_total):
    counts = {}
    for l in loops:
        counts[l["status"]] = counts.get(l["status"], 0) + 1
    healthy = counts.get("GREEN", 0)
    denom = sum(1 for l in loops if l["status"] not in ("NO-DATA", "DISABLED"))
    health_pct = round(100.0 * healthy / denom, 1) if denom else None  # no evidence = no number
    drift = bool(unclaimed)
    max_att = ledger["max_attempts"] if ledger else None
    breakers = [{"item": k, "attempts": e["attempts"], "resolved": False,
                 "state": "CIRCUIT-BROKEN" if e["attempts"] >= max_att else "open",
                 "note": str(e.get("note", ""))[:220]}
                for k, e in (ledger or {}).get("items", {}).items()]
    breakers.sort(key=lambda b: (-b["attempts"], b["item"]))
    kinds = [k for k in ("loop", "watch", "sync", "seed") if any(l["group"] == k for l in loops)]
    status = {
        "schema": 3, "marker": "LOOP_DECK_STATUS", "mode": "harness",
        "generated": NOW.isoformat(), "generated_epoch": int(NOW.timestamp()),
        "tmux_observation": "NOT-OBSERVED",   # harness mode runs no tmux probe
        "drift": drift, "registry_drift": drift, "unclaimed_tags": unclaimed,
        "drift_missing": [], "drift_unregistered": [],
        "counts": counts, "health_pct": health_pct, "healthy": healthy, "denom": denom,
        "runs_total": len(runs), "cron_entries": 0, "window_min": WINDOW_MIN,
        "missed_fires_total": missed_total if runs else None,
        "attention": [l["slug"] for l in sorted(loops, key=lambda l: -SEV[l["status"]])
                      if l["status"] in ("RED", "STALE", "UNKNOWN")],
        "groups": [{"key": k, "title": loops[[l["group"] for l in loops].index(k)]["group_title"],
                    "slugs": [l["slug"] for l in loops if l["group"] == k]} for k in kinds],
        "loops": loops, "breakers": breakers, "max_attempts": max_att,
        "paused": bool(state and state["paused"]),
        "pause_reason": str(state.get("reason", "")) if state else "",
        "registry_source": str(registry.get("source", "")), "registry_generated_at": registry["generated_at"],
        "run_success": {"tags": {l["slug"]: l["run_success"] for l in loops if "run_success" in l},
                        "instrumented": sum(1 for l in loops if "run_success" in l),
                        "note": "harness mode: runs.jsonl records joined to rows by registry tags"},
        "escalations": None, "budget": None, "recent_actions": [], "state_prunes": None,
        "topology_mmd": harness_mermaid(loops), "anatomy_mmd": ANATOMY,
        "evidence_note": "harness mode: state derived from runs.jsonl (latest record by ts) against "
                         "cadence_s; no host probes",
    }
    for name in HARNESS_SPA:   # the Deck's own page (code, not data): same version as this generator
        with open(os.path.join(REPO, "app", name)) as src, atomic_output(os.path.join(OUT, name)) as f:
            f.write(src.read())
    with atomic_output(os.path.join(OUT, "status.json")) as f:
        json.dump(status, f, indent=1)
    hist = "untouched (zero runs)"
    if runs:   # zero runs: never fabricate a first history entry
        hist = append_history({"ts": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "e": int(NOW.timestamp()),
                               "health": health_pct, "drift": drift, "c": counts,
                               "s": {l["slug"]: LETTER[l["status"]] for l in loops}})
    print(f"harness: emitted {len(loops)} loops, {len(runs)} runs, health="
          f"{'—' if health_pct is None else f'{health_pct}%'}, history={hist}, "
          f"drift={'YES ' + ','.join(unclaimed) if drift else 'no'} -> {OUT}/status.json")
    sys.exit(3 if drift else 0)

def main():
    global NOW, OUT
    import fcntl
    if os.environ.get("LOOP_DECK_MODE") == "harness":
        return harness_main()
    os.makedirs(OUT, exist_ok=True)
    # Stable across status replacement, and independent of an inherited
    # .refresh.lock. Every entry through main(), including direct CLI runs,
    # serializes observations, prior-state reconciliation, and both publications.
    with open(os.path.join(OUT, ".generation.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        NOW = datetime.now(timezone.utc)
        _generate()

def harness_main():
    """Harness entry: OUT moves under the data dir before any write; same generation lock."""
    global NOW, OUT
    import fcntl
    try:
        OUT = os.path.join(harness_data_dir(), "out")
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, ".generation.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            NOW = datetime.now(timezone.utc)
            _generate_harness(os.path.dirname(OUT))
    except HarnessError as e:
        print(f"loop-deck harness: {e}", file=sys.stderr)
        sys.exit(2)
    except OSError as e:
        print(f"loop-deck harness: output not writable: {e}", file=sys.stderr)
        sys.exit(2)

def _generate():
    """Run one cycle with main() holding the generation lock."""
    groups = parse_registry()
    fires, coverage_start = syslog_fires()
    window_min = WINDOW_MIN
    if coverage_start:
        window_min = min(WINDOW_MIN, max(60, int((NOW - coverage_start).total_seconds() // 60)))
    crons = parse_crontab(fires)
    tmux_sessions = observe_tmux_sessions()
    previous = previous_loops()
    # Fixer evidence MUST come from ORIGIN, not the local checkout: the fixer commits from a remote host
    # and pushes; the local STATE.md only moves when this host pulls. Found 2026-08-25: local
    # header was 4.3h old (false STALE) while origin had a fresh 21:05Z cycle the whole time.
    # git fetch never touches the working tree; on any failure fall back to the local file.
    state_txt, state_src = "", "origin/master STATE.md"
    try:
        subprocess.run(["git", "-C", REPO, "fetch", "-q", "origin", "master"],
                       capture_output=True, timeout=25)
        state_txt = sh(["git", "-C", REPO, "show", "origin/master:STATE.md"])
    except Exception:
        pass
    if "Last run:" not in state_txt:
        state_src = "LOCAL STATE.md (origin fetch failed — evidence may lag the fixer)"
        try:
            state_txt = open(os.path.join(REPO, "STATE.md")).read()
        except OSError:
            state_txt = ""
    m = re.search(r"Last run:\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})Z", state_txt)
    state_last = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc) if m else None
    STATE_SRC[0] = state_src
    try:
        ledger = json.load(open(os.path.join(REPO, "loop-ledger.json")))
    except Exception:
        ledger = {}

    # ---- instrumentation + A2 action ledgers (append-only; absent file = honest empty state) ----
    day_ago = NOW.timestamp() - 24 * 3600
    run_tags = {}
    for r in read_jsonl(os.path.join(OUT, "runs.jsonl")):
        try:
            e = datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError, TypeError):
            continue
        if e < day_ago:
            continue
        t = run_tags.setdefault(r.get("tag", "?"), {"runs": 0, "ok": 0, "last_rc": None, "last_ts": None})
        t["runs"] += 1
        t["ok"] += 1 if r.get("rc") == 0 else 0
        t["last_rc"], t["last_ts"] = r.get("rc"), r.get("ts")
    for t in run_tags.values():
        t["success_pct"] = round(100.0 * t["ok"] / t["runs"]) if t["runs"] else None
    acks = {}
    for a in read_jsonl(os.path.join(OUT, "acks.jsonl")):
        if a.get("e", 0) >= day_ago and a.get("slug"):
            acks[a["slug"]] = a.get("ts")
    esc = read_jsonl(os.path.join(OUT, "escalations.jsonl"))
    esc7 = [x for x in esc if x.get("e", 0) >= NOW.timestamp() - 7 * 86400]
    try:
        budget = json.load(open(os.path.join(REPO, "loop-budget.json")))
    except (OSError, ValueError):
        budget = None

    drift_missing, loops, seen_slugs = [], [], set()  # counts recomputed after degradation
    for g in groups:
        for r in g["rows"]:
            slug, n = slugify(r["name"]), 2
            while slug in seen_slugs:
                slug, n = f"{slugify(r['name'])}-{n}", n + 1
            seen_slugs.add(slug)
            r["_slug"] = slug
            sessions = session_evidence(r, tmux_sessions,
                                        previous.get((slug, g["key"], r["driver"]), {}))
            st, det, drifted, matched = status_for_row(
                r, crons, tmux_sessions, state_last, window_min, sessions)
            r["_status"] = st
            if drifted:
                drift_missing.append(f'{g["key"]}: {r["name"]}')
            loops.append({
                "slug": slug, "name": r["name"], "group": g["key"],
                "group_title": g["title"], "driver": r["driver"], "cadence": r["cadence"],
                "pattern": r["pattern"], "aut": r["aut"], "verify": r["verify"],
                "handoff": r["handoff"], "status": st, "detail": det, "crons": matched,
                "expected_sessions": sessions["expected_sessions"] if not matched else [],
                "missing_sessions": sessions["missing_sessions"] if not matched else [],
                "ledger": ledger_for(row_tokens(r["driver"]), ledger),
            })
    drift_unreg = [c["raw"] for c in crons if not c["registered"]]
    drift = bool(drift_missing or drift_unreg)
    rowmap = {r["_slug"]: r for g in groups for r in g["rows"]}
    for l in loops:  # join instrumentation by slug (run-logged tags use the loop slug)
        if l["slug"] == "deck-viz-regenerate":
            # This is a child check of the existing refresh, not a separate cron.
            # Missing/stale evidence is also a failed guard, never a vacuous pass.
            check = run_tags.get("deck-fleet-members-lint")
            try:
                stamp = datetime.fromisoformat(check["last_ts"].replace("Z", "+00:00"))
                age = (NOW - stamp).total_seconds()
                current = 0 <= age <= 30 * 60
            except (TypeError, KeyError, ValueError):
                current = False
            healthy_check = current and check.get("last_rc") == 0
            l["membership_check"] = check
            reason = (f"rc={check.get('last_rc')}" if current else "evidence unavailable or older than 30m")
            # The refresh passes the observed rc independently of log persistence.
            # Preserve historical instrumentation, but never mask a current failure.
            observed_rc = os.environ.get("LOOP_MEMBERS_LINT_RC")
            if observed_rc is not None:
                l["membership_current_rc"] = observed_rc
                healthy_check = healthy_check and observed_rc == "0"
                reason = f"current rc={observed_rc}; last logged {reason}"
            l["detail"].insert(0, f"fleet-members-lint: {reason}")
            if not healthy_check:
                l["status"] = rowmap[l["slug"]]["_status"] = "RED"
        if l["slug"] in run_tags:
            l["run_success"] = run_tags[l["slug"]]
            rc = l["run_success"].get("last_rc")
            # launch evidence alone must not keep a FAILING loop green: a cron can fire on
            # schedule while every run exits non-zero (self-analysis gap, 2026-08-25)
            if rc not in (0, None) and l["status"] not in ("RED",):
                l["status"] = "RED"
                l["detail"] = [f"run-FAILURE: last rc={rc} (run-logged exit-code evidence)"] + l["detail"]
                rowmap[l["slug"]]["_status"] = "RED"
        if l["slug"] in acks:
            l["ack_ts"] = acks[l["slug"]]
    counts = {}
    for l in loops:  # recount AFTER degradation so tiles/health/topology agree
        counts[l["status"]] = counts.get(l["status"], 0) + 1
    try:
        pruned_now = track_state_prunes(state_txt)
    except Exception as e:  # review finding: disk-full etc. must not freeze status.json rendering
        print(f"state-prunes: WARN {e}", file=sys.stderr)
        pruned_now = []
    prune_rows = read_jsonl(os.path.join(OUT, "state-prunes.jsonl"))

    healthy = counts.get("GREEN", 0) + counts.get("LIVE", 0)
    denom = max(1, len(loops) - counts.get("NA", 0))
    health_pct = round(100.0 * healthy / denom, 1)
    missed_total = sum(c["missed"] or 0 for l in loops for c in l["crons"])
    attention = [l["slug"] for l in sorted(loops, key=lambda l: -SEV[l["status"]])
                 if l["status"] in ("RED", "STALE", "UNKNOWN")]
    breakers = [{"item": k, "attempts": e.get("attempts", 0), "resolved": bool(e.get("resolved")),
                 "state": ("CIRCUIT-BROKEN" if (e.get("attempts", 0) >= 3 and not e.get("resolved"))
                           else ("resolved" if e.get("resolved") else "open")),
                 "note": (e.get("fix") or e.get("note") or e.get("class") or "")[:220]}
                for k, e in ledger.items()]
    breakers.sort(key=lambda b: (-b["attempts"], b["item"]))

    status = {
        "schema": 3, "marker": "LOOP_DECK_STATUS",  # whitespace-independent serve check
        "generated": NOW.isoformat(), "generated_epoch": int(NOW.timestamp()),
        "tmux_observation": "UNKNOWN" if tmux_sessions is None else "OK",
        "drift": drift, "drift_missing": drift_missing, "drift_unregistered": drift_unreg,
        "counts": counts, "health_pct": health_pct, "healthy": healthy, "denom": denom,
        "cron_entries": len(crons), "window_min": window_min,
        "missed_fires_total": missed_total, "attention": attention,
        "groups": [{"key": g["key"], "title": g["title"],
                    "slugs": [r["_slug"] for r in g["rows"]]} for g in groups],
        "loops": loops, "breakers": breakers,
        "run_success": {"tags": run_tags, "instrumented": len(run_tags),
                        "note": "exit-code capture live for the Deck owner's crons only "
                                "(scope set by the operator); fleet-wide adoption carded"},
        "escalations": {"total_logged": len(esc), "last_7d": len(esc7),
                        "last": esc[-1] if esc else None,
                        "note": "append-only ledger via tools/escalate-log.sh — adoption is "
                                "per loop"},
        "budget": ({**budget,
                    "attempts_open": sum(1 for b in breakers if b["state"] == "open"),
                    "attempts_at_cap": sum(1 for b in breakers if b["state"] == "CIRCUIT-BROKEN")}
                   if budget else None),
        "recent_actions": read_jsonl(os.path.join(OUT, "actions.jsonl"), cap=20),
        "state_prunes": {"total": len(prune_rows), "pruned_this_run": len(pruned_now),
                         "last_7d": sum(1 for r in prune_rows if r.get("e", 0) >= NOW.timestamp() - 7*86400),
                         "note": "lines removed/edited-away from origin STATE.md, snapshot-diffed "
                                 "every run — prune evidence beyond git archaeology; full ledger "
                                 "state-prunes.jsonl (git history remains the immutable source)"},
        "topology_mmd": mermaid_overview(groups), "anatomy_mmd": ANATOMY,
        "evidence_note": "cron fires = LAUNCH evidence (syslog CRON CMD lines); run SUCCESS = exit codes via run-logged.sh where adopted (last rc!=0 => RED) — fleet-wide adoption carded",
    }
    with atomic_output(os.path.join(OUT, "status.json")) as f:
        json.dump(status, f, indent=1)

    hist_n = append_history({
        "ts": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "e": int(NOW.timestamp()),
        "health": health_pct, "drift": drift, "c": counts,
        "s": {l["slug"]: LETTER[l["status"]] for l in loops},
    })

    print(f"emitted {len(loops)} loops, {len(crons)} crons, window={window_min}m, "
          f"health={health_pct}%, missed={missed_total}, history={hist_n} lines, "
          f"drift={'YES' if drift else 'no'} -> {OUT}/status.json")
    sys.exit(3 if drift else 0)

if __name__ == "__main__":
    main()
