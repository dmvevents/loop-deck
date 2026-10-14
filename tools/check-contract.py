#!/usr/bin/env python3
"""check-loop-deck-contract.py — validate a Loop Deck harness-mode data directory against the DATA CONTRACT
(docs/CONTRACT.md §3-§4). Used two ways:

  python3 tools/check-contract.py [DATA_DIR]      # default: examples
      exit 0 = every file present and valid (missing runs.jsonl / loop-ledger.json / state.json are allowed and
               reported as "absent" — the generator treats them as empty), 2 = ANY contract violation
               (shape, encoding, unreadable file, unclaimed run tag) — the validator never exits 1 or 3; the
               GENERATOR alone distinguishes exit 3 (REGISTRY-DRIFT) from exit 2 (fail-closed: a malformed
               adapter output must never render)
  python3 tools/check-contract.py --self-test     # plants one defect per contract rule into
      copies of the examples and asserts each copy is REJECTED for THAT rule's reason while the pristine
      examples PASS; exit 0 = the validator discriminates, 1 = a planted defect was accepted or was rejected
      only by a collateral rule (the check would be decorative for the rule under test)

Why a separate checker: the adapter author (another team) needs a failing-first oracle before the release
tree exists, and the release tree's generator reuses these rules so the two cannot drift apart.
"""
import argparse, copy, datetime, json, math, os, re, sys, tempfile

SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")  # ASCII digits only (\d admits other scripts)
KINDS = ("loop", "watch", "sync", "seed")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(REPO, "examples")


class Violation(Exception):
    pass


class Missing(Violation):
    """A file that is allowed to be absent is absent (never raised for a present-but-broken file)."""


def _ts(v, where):
    if not isinstance(v, str) or not TS_RE.match(v):
        raise Violation(f"{where}: timestamp must be YYYY-MM-DDTHH:MM:SSZ, got {v!r}")
    try:
        datetime.datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")  # range check: month 13 / second 61 are not timestamps
    except ValueError as exc:
        raise Violation(f"{where}: timestamp out of range ({exc})")


def _load_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise Missing(f"{what} absent: {path}")
    except json.JSONDecodeError as exc:
        raise Violation(f"{what} is not valid JSON ({path}): {exc}")
    except UnicodeDecodeError as exc:
        raise Violation(f"{what} is not UTF-8 ({path}): {exc}")
    except OSError as exc:  # unreadable (permissions, a directory, I/O) is a violation, never a traceback
        raise Violation(f"{what} unreadable ({path}): {exc}")


def check_registry(doc):
    if not isinstance(doc, dict) or doc.get("schema") != "loop-deck-registry/1":
        raise Violation("registry.json: schema must be 'loop-deck-registry/1'")
    _ts(doc.get("generated_at"), "registry.json generated_at")
    loops = doc.get("loops")
    if not isinstance(loops, list) or not loops:
        raise Violation("registry.json: 'loops' must be a non-empty list (an empty registry is 'nothing to render', not a pass)")
    slugs, tags = set(), {}
    for i, row in enumerate(loops):
        where = f"registry.json loops[{i}]"
        if not isinstance(row, dict):
            raise Violation(f"{where}: row must be an object")
        slug = row.get("slug")
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            raise Violation(f"{where}: slug must match [a-z0-9][a-z0-9-]{{0,63}}, got {slug!r}")
        if slug in slugs:
            raise Violation(f"{where}: duplicate slug {slug!r}")
        slugs.add(slug)
        for key in ("name", "owner"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise Violation(f"{where} ({slug}): {key} must be a non-empty string")
        kind = row.get("kind")
        if kind not in KINDS:
            raise Violation(f"{where} ({slug}): kind must be one of {KINDS}, got {kind!r}")
        cad = row.get("cadence_s", "<absent>")
        if cad == "<absent>":
            raise Violation(f"{where} ({slug}): cadence_s is required (int seconds, or null for kind 'seed')")
        if cad is None and kind != "seed":
            raise Violation(f"{where} ({slug}): cadence_s may be null only for kind 'seed'")
        if cad is not None and (not isinstance(cad, int) or isinstance(cad, bool) or cad <= 0):
            raise Violation(f"{where} ({slug}): cadence_s must be a positive int, got {cad!r}")
        rt = row.get("tags")
        if not isinstance(rt, list) or not rt or any(not isinstance(t, str) or not t for t in rt):
            raise Violation(f"{where} ({slug}): tags must be a non-empty list of non-empty strings")
        for t in rt:
            if t in tags and tags[t] != slug:
                raise Violation(f"{where} ({slug}): tag {t!r} is already claimed by {tags[t]!r}")
            tags[t] = slug
        if not isinstance(row.get("enabled"), bool):
            raise Violation(f"{where} ({slug}): enabled must be true or false")
        deps = row.get("depends_on", [])
        if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
            raise Violation(f"{where} ({slug}): depends_on must be a list of slugs")
        if "runbook_md" in row and not isinstance(row["runbook_md"], str):
            raise Violation(f"{where} ({slug}): runbook_md must be a string")
    for row in loops:
        for d in row.get("depends_on", []):
            if d not in slugs:
                raise Violation(f"registry.json ({row['slug']}): depends_on names unknown slug {d!r}")
    return tags


def check_runs(path, tags):
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return 0, set()
    except UnicodeDecodeError as exc:
        raise Violation(f"runs.jsonl is not UTF-8: {exc}")
    except OSError as exc:
        raise Violation(f"runs.jsonl unreadable: {exc}")
    unclaimed, n = set(), 0
    for ln, line in enumerate(lines, 1):
        if not line.strip():
            raise Violation(f"runs.jsonl:{ln}: blank line (append-only file must not contain empty records)")
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise Violation(f"runs.jsonl:{ln}: not JSON ({exc})")
        if not isinstance(rec, dict):
            raise Violation(f"runs.jsonl:{ln}: record must be an object")
        for key in ("ts", "tag", "rc", "dur_s"):
            if key not in rec:
                raise Violation(f"runs.jsonl:{ln}: missing required key {key!r}")
        _ts(rec["ts"], f"runs.jsonl:{ln} ts")
        if not isinstance(rec["tag"], str) or not rec["tag"]:
            raise Violation(f"runs.jsonl:{ln}: tag must be a non-empty string")
        if not isinstance(rec["rc"], int) or isinstance(rec["rc"], bool):
            raise Violation(f"runs.jsonl:{ln}: rc must be an int, got {rec['rc']!r}")
        d = rec["dur_s"]
        if d is not None and (isinstance(d, bool) or not isinstance(d, (int, float)) or not math.isfinite(d) or d < 0):
            raise Violation(f"runs.jsonl:{ln}: dur_s must be a finite non-negative number or null, got {d!r}")
        if "note" in rec and (not isinstance(rec["note"], str) or len(rec["note"]) > 200):
            raise Violation(f"runs.jsonl:{ln}: note must be a string of at most 200 chars")
        if rec["tag"] not in tags:
            unclaimed.add(rec["tag"])
        n += 1
    return n, unclaimed


def check_ledger(path, slugs):
    try:
        doc = _load_json(path, "loop-ledger.json")
    except Missing:
        return None
    if not isinstance(doc, dict) or doc.get("schema") != "loop-deck-ledger/1":
        raise Violation("loop-ledger.json: schema must be 'loop-deck-ledger/1'")
    mx = doc.get("max_attempts")
    if not isinstance(mx, int) or isinstance(mx, bool) or mx <= 0:
        raise Violation(f"loop-ledger.json: max_attempts must be a positive int, got {mx!r}")
    items = doc.get("items")
    if not isinstance(items, dict):
        raise Violation("loop-ledger.json: items must be an object keyed by '<slug>:<item>'")
    for key, it in items.items():
        if ":" not in key or not SLUG_RE.match(key.split(":", 1)[0]):
            raise Violation(f"loop-ledger.json items[{key!r}]: key must be '<slug>:<item>'")
        if key.split(":", 1)[0] not in slugs:
            raise Violation(f"loop-ledger.json items[{key!r}]: slug {key.split(':', 1)[0]!r} is not a registry row")
        if not isinstance(it, dict) or not isinstance(it.get("attempts"), int) or isinstance(it.get("attempts"), bool) or it["attempts"] < 0:
            raise Violation(f"loop-ledger.json items[{key!r}]: attempts must be a non-negative int")
        if "last_ts" in it:
            _ts(it["last_ts"], f"loop-ledger.json items[{key!r}] last_ts")
    return len(items)


def check_state(path):
    try:
        doc = _load_json(path, "state.json")
    except Missing:
        return None
    if not isinstance(doc, dict) or doc.get("schema") != "loop-deck-state/1":
        raise Violation("state.json: schema must be 'loop-deck-state/1'")
    if not isinstance(doc.get("paused"), bool):
        raise Violation("state.json: paused must be true or false (a missing or stringy flag is UNKNOWN, not running)")
    _ts(doc.get("ts"), "state.json ts")
    return doc["paused"]


def validate(data_dir):
    """Return a report dict or raise Violation."""
    tags = check_registry(_load_json(os.path.join(data_dir, "registry.json"), "registry.json"))
    n_runs, unclaimed = check_runs(os.path.join(data_dir, "runs.jsonl"), tags)
    if unclaimed:
        raise Violation(f"runs.jsonl: tags not claimed by any registry row (REGISTRY-DRIFT): {sorted(unclaimed)}")
    n_items = check_ledger(os.path.join(data_dir, "loop-ledger.json"), set(tags.values()))
    paused = check_state(os.path.join(data_dir, "state.json"))
    return {"rows": len(set(tags.values())), "tags": len(tags), "runs": n_runs,
            "ledger_items": "absent" if n_items is None else n_items,
            "paused": "absent" if paused is None else paused}


# ---- self-test: plant one defect per file, each must be REJECTED ----------------------------------------
def _plant(base, out, registry=None, runs=None, ledger=None, state=None, runs_bytes=None, state_raw=None,
           subdir=None, chmod_registry=None):
    if subdir:
        out = os.path.join(out, subdir); os.mkdir(out)
    for name in ("registry.json", "runs.jsonl", "loop-ledger.json", "state.json"):
        src = os.path.join(base, name)
        if os.path.exists(src):
            with open(src) as fh, open(os.path.join(out, name), "w") as oh:
                oh.write(fh.read())
    if registry is not None:
        json.dump(registry, open(os.path.join(out, "registry.json"), "w"))
    if runs is not None:
        open(os.path.join(out, "runs.jsonl"), "w").write(runs)
    if runs_bytes is not None:
        open(os.path.join(out, "runs.jsonl"), "wb").write(runs_bytes)
    if ledger is not None:
        json.dump(ledger, open(os.path.join(out, "loop-ledger.json"), "w"))
    if state is not None:
        json.dump(state, open(os.path.join(out, "state.json"), "w"))
    if state_raw is not None:
        open(os.path.join(out, "state.json"), "w").write(state_raw)
    if chmod_registry is not None:
        os.chmod(os.path.join(out, "registry.json"), chmod_registry)
    return out


def planted_cases(reg, runs, ledger, state):
    """One planted defect per contract rule: (label, plant kwargs, the reason substring the validator MUST give).
    Importable so a doc-truth guard can count them (contract §9 states the number)."""
    def mut_reg(fn):
        r = copy.deepcopy(reg); fn(r); return r
    # the uppercase slug is planted on the LAST row (nobody depends on it): planting it on row 0 was rejected by
    # the depends_on rule of row 1 instead, so the slug-shape rule itself was pinned by nothing (review finding)
    return [
        ("registry: slug with uppercase", dict(registry=mut_reg(lambda r: r["loops"][-1].__setitem__("slug", "Hindsight"))), "slug must match"),
        ("registry: cadence_s null on a periodic loop", dict(registry=mut_reg(lambda r: r["loops"][0].__setitem__("cadence_s", None))), "cadence_s may be null only for kind 'seed'"),
        ("registry: empty tags", dict(registry=mut_reg(lambda r: r["loops"][0].__setitem__("tags", []))), "tags must be a non-empty list"),
        ("registry: enabled as string", dict(registry=mut_reg(lambda r: r["loops"][0].__setitem__("enabled", "yes"))), "enabled must be true or false"),
        ("registry: depends_on unknown slug", dict(registry=mut_reg(lambda r: r["loops"][0].__setitem__("depends_on", ["ghost"]))), "depends_on names unknown slug"),
        ("registry: zero loops", dict(registry=mut_reg(lambda r: r.__setitem__("loops", []))), "'loops' must be a non-empty list"),
        ("runs: unclaimed tag (drift)", dict(runs=runs + '{"ts": "2026-10-13T18:00:00Z", "tag": "nobody-owns-me", "rc": 0, "dur_s": 1}\n'), "REGISTRY-DRIFT"),
        ("runs: rc as string", dict(runs=runs + '{"ts": "2026-10-13T18:00:00Z", "tag": "harness-autonomy-loop", "rc": "0", "dur_s": 1}\n'), "rc must be an int"),
        ("runs: missing ts", dict(runs=runs + '{"tag": "harness-autonomy-loop", "rc": 0, "dur_s": 1}\n'), "missing required key 'ts'"),
        ("runs: non-JSON line", dict(runs=runs + "FIRE 3 END rc=0 duration=12s\n"), "not JSON"),
        ("runs: timestamp without Z", dict(runs=runs + '{"ts": "2026-10-13 18:00:00", "tag": "harness-autonomy-loop", "rc": 0, "dur_s": 1}\n'), "timestamp must be YYYY-MM-DDTHH:MM:SSZ"),
        ("ledger: attempts negative", dict(ledger={**ledger, "items": {"autonomy-loop:x": {"attempts": -1}}}), "attempts must be a non-negative int"),
        ("ledger: key without slug prefix", dict(ledger={**ledger, "items": {"fire-12": {"attempts": 3}}}), "key must be '<slug>:<item>'"),
        ("state: paused as string", dict(state={**state, "paused": "false"}), "paused must be true or false"),
        ("state: wrong schema", dict(state={**state, "schema": "loop-deck-state/0"}), "schema must be 'loop-deck-state/1'"),
        ("runs: NaN dur_s", dict(runs=runs + '{"ts": "2026-10-13T18:00:00Z", "tag": "harness-autonomy-loop", "rc": 0, "dur_s": NaN}\n'), "dur_s must be a finite non-negative number"),
        ("runs: non-ASCII digits in ts", dict(runs=runs + '{"ts": "\u0662026-10-13T18:00:00Z", "tag": "harness-autonomy-loop", "rc": 0, "dur_s": 1}\n'), "timestamp must be YYYY-MM-DDTHH:MM:SSZ"),
        ("runs: out-of-range timestamp (month 13)", dict(runs=runs + '{"ts": "2026-13-13T18:00:00Z", "tag": "harness-autonomy-loop", "rc": 0, "dur_s": 1}\n'), "timestamp out of range"),
        ("ledger: key slug not in the registry", dict(ledger={**ledger, "items": {"ghost:x": {"attempts": 1}}}), "is not a registry row"),
        ("runs: not UTF-8", dict(runs_bytes=b'\xff\xfe{"ts": "2026-10-13T18:00:00Z"}\n'), "not UTF-8"),
        ("state: malformed JSON in a directory whose name contains 'missing' (review finding)", dict(state_raw="{not json", subdir="missing-word"), "is not valid JSON"),
        ("registry: unreadable (mode 000)", dict(chmod_registry=0), "unreadable"),
    ]


def self_test(base):
    reg = json.load(open(os.path.join(base, "registry.json")))
    runs = open(os.path.join(base, "runs.jsonl")).read()
    ledger = json.load(open(os.path.join(base, "loop-ledger.json")))
    state = json.load(open(os.path.join(base, "state.json")))
    cases = planted_cases(reg, runs, ledger, state)
    ok = True
    with tempfile.TemporaryDirectory() as td:
        pristine = os.path.join(td, "pristine"); os.mkdir(pristine); _plant(base, pristine)
        try:
            rep = validate(pristine); print(f"PASS  pristine examples validate: {rep}")
        except Violation as exc:
            print(f"FAIL  pristine examples rejected: {exc}"); ok = False
        for i, (label, kw, expect) in enumerate(cases):
            d = os.path.join(td, f"case{i}"); os.mkdir(d); d = _plant(base, d, **kw)
            if kw.get("chmod_registry") == 0 and os.geteuid() == 0:
                print(f"SKIP  {label} (root reads mode-000 files; not a verdict)"); continue
            try:
                validate(d); print(f"FAIL  accepted planted defect: {label}"); ok = False
            except Violation as exc:
                if expect in str(exc):   # the RIGHT rule rejected it, not a collateral one (review finding)
                    print(f"PASS  rejected: {label}\n      {str(exc)[:110]}")
                else:
                    print(f"FAIL  rejected for the wrong reason: {label}\n      wanted {expect!r}, got {str(exc)[:100]!r}"); ok = False
            except Exception as exc:  # a traceback is a fail-OPEN path for the generator's reuse: count it as a defect
                print(f"FAIL  crashed instead of rejecting ({type(exc).__name__}): {label}"); ok = False
            finally:
                if kw.get("chmod_registry") == 0:
                    os.chmod(os.path.join(d, "registry.json"), 0o600)
    print("SELF-TEST", f"PASSED — the validator discriminates ({len(cases)} planted defects, each rejected by its own rule)" if ok
          else "FAILED — a planted defect was accepted or rejected by the wrong rule")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("data_dir", nargs="?", default=EXAMPLES)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test(a.data_dir)
    try:
        rep = validate(a.data_dir)
    except Violation as exc:
        print(f"CONTRACT VIOLATION: {exc}", file=sys.stderr)
        return 2
    print(f"OK: {a.data_dir} satisfies loop-deck harness contract v1 — {rep}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
