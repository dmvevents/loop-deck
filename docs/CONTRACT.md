# Loop Deck — harness adapter DATA CONTRACT (v0.8, 2026-10-13)

> Status: v0.8 — the DECISION sections are final for the first harness release; the MEASURED sections quote
> the Deck's code at an earlier source revision with file:line, from the
> read-only audit (an internal document, not shipped; 68/68 of ITS citations checked mechanically).
> Harness mode shipped on 2026-10-13 as Loop Deck 1.7.0 and was built against this document;
> the MEASURED sections still describe the fleet mode they were measured on. Where that code contradicts a DECISION,
> the contradiction is stated, not hidden.

Owner: the Loop Deck maintainers. Consumers: the agent-harness adapter, the agent-loop
drill-down view, and the verifier.

## 0. One-paragraph model

Loop Deck renders **loops** (rows of a registry) against **evidence** (fire-and-result records, breaker
ledger, pause flag). In the fleet the evidence comes from cron/syslog/tmux probes of a live host. In
**harness mode** the Deck reads ONLY files under one data directory that an adapter writes, probes nothing,
and writes its own outputs to `out/` under that directory. Rows without evidence render as honest empty
states ("no runs yet"), never as green.

## 1. DECISION — mode, data directory, file set

| Item | Value |
|---|---|
| Mode switch | `LOOP_DECK_MODE=harness` (explicit; anything else = fleet mode, unchanged) |
| Data directory | `LOOP_DECK_DATA_DIR=<abs path>` (required in harness mode; missing/unreadable = hard error, exit 2) |
| Adapter-written inputs | `registry.json`, `runs.jsonl`, `loop-ledger.json`, `state.json` (all under `LOOP_DECK_DATA_DIR`) |
| Deck-owned outputs (never written by the adapter) | `out/status.json`, `out/history.jsonl`, `out/acks.jsonl`, `out/actions.jsonl`, `out/rerun.log`, `out/.generation.lock`, `out/history.jsonl.lock`, `out/.rerun.lock`, the transient input snapshot `out/.inputs-*` (validated bytes = rendered bytes), and the page files `out/index.html`, `out/app.css`, `out/app.js` the harness generator copies on every run |
| Not rendered in harness mode v1 | `out/acks.jsonl` and `out/actions.jsonl` are written by the server but not read by the harness generator (ack chips and the audit tail stay empty); moot while actions are off |
| Generator invocation | `python3 -I generate-loop-viz.py` with the two variables above; exit 0 = rendered, 2 = hard error (input unreadable/malformed), 3 = registry drift (see §4) |
| Server invocation | `python3 -I deck-server.py --port <internal port> --root "$LOOP_DECK_DATA_DIR/out"` (binds 127.0.0.1 only; asserted) |
| Actions (/act) | OFF unless `LOOP_DECK_ACT_TOKEN` is set; see §7 |
| Cadence | the adapter decides when to re-run the generator (the fleet runs it every 15 min); the SPA re-fetches it every 60 s (`fetch('status.json'` at `app.js:109`, `setInterval(poll, 60000)` at `:566`) |
| Runtime | python3 ≥ 3.10, standard library only; no network; loopback only. The harness image installs python3 (`Container-Root/setup/install-comms.sh:25`) |

MEASURED today (fleet mode): there is no data-dir switch — `REPO` and `OUT` derive from `__file__`
(`OUT = os.path.join(REPO, "out")`, `generate-loop-viz.py:26-27`), the only variable the generator reads through `os.environ` is
`LOOP_MEMBERS_LINT_RC` (`:556`; `HOME` enters only through `expanduser` at `:130`), and the server's `rerun-viz` hard-wires `REPO/.refresh.lock`, `REPO/generate-loop-viz.py` and
`cwd=REPO` (`deck-server.py:161-170`). Harness mode adds the switch and routes both through the data dir.

Harness mode **never** runs: `crontab -l`, syslog/journal reads, `tmux …`, `pgrep`, `git fetch`, the
fleet-members lint-tag check, the A1 auto-carding step (`card_red_loops.py`), or any Plane call (§8).

## 2. MEASURED — what the generator reads today (fleet mode, `generate-loop-viz.py` at an earlier source revision)

| # | Input | Path / command | Lines | When missing or empty |
|---|---|---|---|---|
| 1 | Registry | `REPO/LOOP.md`, markdown table; active region before `## Archived Loops`; group headers `^### ([A-D])\. (.+)$`; 7 cells by position `name, driver, cadence, pattern, aut, verify, handoff`; slug = lower-cased name with non-`[a-z0-9]` runs → `-`, duplicates `-2`, `-3` | 64-81, 55-61, 516-518 | `open()` raises uncaught → exit 1, nothing written |
| 2 | Schedule | `crontab -l` via `sh()` (shell, 20 s timeout, any exception → `""`); `row_tokens`/`tok_matches` map each `dir/script.(sh|py)` token in a row's driver text onto cron commands, many-to-many | 40-44, 109-136, 188-204, 297-303 | zero entries; cron-like rows go RED `DRIFT: …` → exit 3 |
| 3 | Fire evidence | `/var/log/syslog.1`, `/var/log/syslog`: lines with ` CRON[` and ` CMD (`; ISO timestamp first token; payload matched verbatim to the crontab command | 86-104, 132 | `OSError` skips silently; no fires; `window_min` stays 1440 |
| 4 | Fallback evidence | mtime of the first `>`/`>>` log target in the cron command | 124-131, 158-162, 242-244 | no age → cron SCHED |
| 5 | Sessions | `tmux ls -F #{session_name}` (argv, 20 s) | 46-53 | `None` → `tmux_observation: "UNKNOWN"` |
| 6 | @reboot liveness | `pgrep -f <basename> \| head -1` via shell | 237-241 | **always "alive": the pattern matches its own `/bin/sh -c` (false GREEN, measured live on a fleet daemon)** — fleet bug, see §8 |
| 7 | Previous snapshot | `REPO/out/status.json` (expected/missing sessions) | 257-269 | `{}` |
| 8 | Fixer text | `git -C REPO fetch -q origin master` then `git show origin/master:STATE.md`; falls back to local `STATE.md`; only `Last run: <ts>Z` is parsed | 465-479 | `""` |
| 9 | Breaker ledger | `REPO/loop-ledger.json`: flat object `{item: {attempts, resolved, fix|note|class, …}}`; a row's ledger = the first key that CONTAINS a driver script basename; threshold 3 **hard-coded** | 334-341, 481-484, 591-596 | `{}` |
| 10 | Exit codes | `REPO/out/runs.jsonl` (`tools/run-logged.sh` records `{"ts","tag","rc","dur_s"}`, `ts` = START time) → `run_success` per tag over a fixed 24 h; `last_rc`/`last_ts` from the LAST LINE IN FILE ORDER; a row gets it when slug == tag; `rc` compared with `== 0` | 487-501, 564-572 | no `run_success` |
| 11 | Acks | `out/acks.jsonl` (`e ≥ now-24h`, truthy `slug`) → `ack_ts` | 502-505, 573-574 | none |
| 12 | Escalations | `out/escalations.jsonl` | 506-507, 612-615 | zeros |
| 13 | Budget | `REPO/loop-budget.json` | 508-511, 616-619 | `null` |
| 14 | Audit tail | `out/actions.jsonl`, last 20 lines | 620 | `[]` |
| 15 | Prune ledger | `out/state-head.snapshot`, `out/state-prunes.jsonl` | 375-398, 583 | seeded on first run |
| 16 | Env | `LOOP_MEMBERS_LINT_RC` (membership guard for the Deck's own row) | 540-563 | ledger decides alone |

Status model today: `SEV = {RED:5, STALE:4, UNKNOWN:4, GREEN:3, LIVE:3, SCHED:2, NA:1}` (`:32`); per cron
GREEN if `age <= max(2 * per, 10)`, STALE if `age <= max(6 * per, 30)`, else RED (`:236-251`); rows without a
matched cron (`if not matched:`, `:304-326`) follow the 7-rule table (agentic-fixer special case, session loss → RED drift,
tmux UNKNOWN → UNKNOWN, session up → LIVE, hub/session driver → NA, cron-like cadence → RED drift, else NA).
**Run evidence only degrades a row** (`:564-572`): `rc not in (0, None)` turns it RED with
`run-FAILURE: last rc=N`; run success never raises a row to GREEN. Health = (GREEN+LIVE)/max(1, loops−NA)
(`recount AFTER degradation`, `:575-590`). Drift (`drift_missing` or `drift_unregistered`) → `sys.exit(3 if drift else 0)` (`:641`); any uncaught
exception → exit 1 with `status.json` left stale (table 1.18 of the audit: a `runs.jsonl` `ts` that is null
or a number; non-object lines in acks/escalations/state-prunes; a non-object ledger; a non-object budget).
Outputs: `status.json` (schema 3, atomic replace, mode 0600; `json.dump(status, f, indent=1)`, `:598-630`), `history.jsonl`
(`{"ts","e","health","drift","c":{STATUS:n},"s":{slug:letter}}`, letters via `LETTER[l["status"]]`, `:632-636`),
`state-head.snapshot`, `state-prunes.jsonl`; stdout `emitted N loops, M crons, window=…, health=…, missed=…,
history=…, drift=… -> …/status.json` (`:638-640`). The A1 carding step and the lint run live in
the fleet's refresh script (not shipped), not in the generator.

## 3. Input schemas (harness mode)

### 3.1 `registry.json` — the rows

```json
{
  "schema": "loop-deck-registry/1",
  "generated_at": "2026-10-13T20:00:00Z",
  "source": "example harness adapter 0.1",
  "loops": [
    {
      "slug": "autonomy-loop",
      "name": "Autonomy loop",
      "kind": "loop",
      "owner": "/agent/loop.sh (AUTONOMY_LOOP_INTERVAL=300)",
      "cadence_s": 300,
      "tags": ["harness-autonomy-loop"],
      "enabled": true,
      "depends_on": [],
      "runbook_md": "Stop: touch /wd/loop/STOP. Resume: remove it; loop-start.sh relaunches. Logs: /wd/loop/loop.log, fires/fire-N.out."
    }
  ]
}
```

| Field | Type | Required | Meaning / how the Deck uses it |
|---|---|---|---|
| `slug` | `[a-z0-9][a-z0-9-]{0,63}` | yes | row identity; key for acks, history letters, the drill-down route `#/loop/<slug>` (links `'#/loop/' + l.slug` at `app.js:288, :303, :338-339` and `'#/loop/' + e.slug` at `:474`; router `loop: () => vDetail(arg)` at `:557`) |
| `name` | string | yes | display name |
| `kind` | `loop` \| `watch` \| `sync` \| `seed` | yes | `seed` = one-shot (never "missed", never stale); others are periodic |
| `owner` | string | yes | WHAT drives it (a script/mechanism), never a person |
| `cadence_s` | int \| null | yes | expected period; `null` only for `kind: seed` |
| `tags` | [string] | yes, ≥1 | the `tag` values in `runs.jsonl` that count as this row's fires; a tag belongs to exactly one row |
| `enabled` | bool | yes | `false` renders the row as DISABLED (grey), never RED, never counted in health |
| `depends_on` | [slug] | no | edges for the Topology view (today the hub-only `mermaid_overview` (`generate-loop-viz.py:344-360`) has a fixed hub node and NO per-loop edges; harness mode draws these) |
| `runbook_md` | string (markdown) | no | shown in the Runbook view per row (today the Runbook is static text, `function vRunbook()` at `app.js:481-539`); empty = "no runbook" |

### 3.2 `runs.jsonl` — the single fire-and-result feed (append-only)

One JSON object per line, UTC timestamps `YYYY-MM-DDTHH:MM:SSZ`:

```json
{"ts": "2026-10-13T16:44:51Z", "tag": "harness-autonomy-loop", "rc": 0, "dur_s": 43}
{"ts": "2026-10-13T17:27:59Z", "tag": "harness-slack-chat", "rc": 0, "dur_s": 1, "note": "ts=1700000000.000000 posted=yes"}
```

| Field | Type | Required | Rule |
|---|---|---|---|
| `ts` | ISO-8601 Z | yes | when the fire ENDED (the source line's own timestamp). The fleet producer stamps the START (`TS="$(date -u +%FT%TZ)"`, `tools/run-logged.sh:12`); harness mode orders by `ts`, never by file position |
| `tag` | string | yes | must match one registry row's `tags`; an unclaimed tag is REGISTRY-DRIFT (§4), never silently kept (today unknown tags sit in `run_tags` unflagged — `l["run_success"] = run_tags[l["slug"]]`, `:564-565`) |
| `rc` | int | yes | 0 = success; any other value = failure (RED); the adapter never invents an rc it did not observe |
| `dur_s` | number \| null | yes | wall time; `null` when the source has none (stamps). Ignored by today's generator; rendered in harness mode |
| `note` | string | no | ≤200 chars, shown in History / the row detail |

**DECISION — run-derived row state (harness mode).** Today's code has no path from run records to GREEN
(`rc not in (0, None)`, `:564-572`), so harness mode defines one and pins it with a test per state:

Let `age = now − ts` of the latest record (ordered by `ts`), `F = HARNESS_FRESH_FACTOR = 2`,
`M = HARNESS_MISSED_FACTOR = 6` (the fleet's own band `max(6 * per, 30)` at `generate-loop-viz.py:247-251`, without its
10/30-minute floors: `cadence_s` is explicit here and must be ≥ the adapter's generator interval, or rows flap).

| State | Rule |
|---|---|
| `GREEN` | latest `rc == 0` and `age ≤ F × cadence_s` |
| `STALE` | latest `rc == 0` and `F × cadence_s < age ≤ M × cadence_s` (the loop stopped reporting) |
| `RED` | latest `rc != 0` at any age; or `age > M × cadence_s` for a periodic kind ("missed") |
| `NO-DATA` | zero records for the row (grey; excluded from the health denominator) |
| `DISABLED` | `enabled: false` (grey; excluded; run records ignored) |
| `seed` rows | GREEN on `rc == 0` at any age, RED on `rc != 0`, never STALE/missed |

Boundaries are pinned by tests: age exactly `F×` is GREEN, `F×+1 s` STALE, exactly `M×` STALE, `M×+1 s` RED.

`run_success` keeps the fleet shape `{runs, ok, last_rc, last_ts, success_pct}` over the trailing 24 h.

### 3.3 `loop-ledger.json` — breakers (attempt counters)

```json
{"schema": "loop-deck-ledger/1", "max_attempts": 3,
 "items": {"autonomy-loop:fire-12": {"attempts": 3, "last_ts": "2026-10-13T17:10:00Z", "note": "timeout exit status 124"}}}
```

A row is a **breaker** (Breakers view, RED) when any item whose key starts with `<slug>:` has
`attempts >= max_attempts`. MEASURED today: a flat object matched by script-basename substring with a
hard-coded 3 (`def ledger_for(toks, ledger)` at `generate-loop-viz.py:334-341`, `breakers.sort` at `:591-596`; `${b.attempts}/3` at `app.js:443` and `maxAttemptsPerItem 3` at `app.js:447`); harness mode reads this explicit
form and takes the threshold from the file.

### 3.4 `state.json` — the pause flag

```json
{"schema": "loop-deck-state/1", "paused": false, "reason": "", "ts": "2026-10-13T20:00:00Z"}
```

`paused: true` → `status.json` carries `paused: true` + `pause_reason`, the SPA shows a PAUSED banner on every
view, and (only when actions are enabled) every verb but `resume` is refused. MEASURED today: `status.json`
has no pause field and the SPA no banner (the fleet's refresh script, not shipped, exits before generating when paused: `paused — exit`). The
adapter maps the harness `STOP` flag per row (`enabled: false`), NOT to this global flag, unless the whole
harness is paused. A missing `state.json` = not paused; a malformed one = UNKNOWN = exit 2.

## 4. Drift and hard errors (fail-closed)

| Condition | Generator behaviour (harness mode) |
|---|---|
| `LOOP_DECK_DATA_DIR` unset / missing / unreadable | exit 2, no `status.json` written |
| `registry.json` missing or not matching 3.1 | exit 2 |
| `runs.jsonl` missing | **zero runs** (honest empty states), exit 0 |
| a `runs.jsonl` line that is not JSON or lacks a required key or has a non-string `ts` | exit 2 with a one-line reason (today: traceback, exit 1, stale status — audit table 1.18) |
| a `tag` in `runs.jsonl` that no row claims | exit 3 REGISTRY-DRIFT; `status.json` written with `registry_drift: true` and the unclaimed tags listed |
| `loop-ledger.json` / `state.json` missing | treated as empty / not paused, exit 0 |
| `loop-ledger.json` / `state.json` present but malformed | exit 2 |

The shipped validator `tools/check-contract.py` enforces the SHAPE rules of §3 and reports every
violation — including an unclaimed tag — with exit 2; only the generator distinguishes exit 3 (drift) from 2. Its
`--self-test` plants defects and requires each to be rejected; the generator reuses its rules.

## 5. Honest empty states (fresh install, zero runs)

MEASURED today (audit §3.5): with zero loops the hero shows `0%` and `0/1 loops healthy` (denominator invented:
`denom = max(1, len(loops) - counts.get("NA", 0))` at `generate-loop-viz.py:586`, `${st.healthy}/${st.denom} loops healthy` at `app.js:126-127`);
an empty `attention` prints `✔ all clear` (`app.js:281`) even with nothing observed; `missed fires 0` and `window 24h` appear with no syslog; `in sync` appears with
no crontab; the History view says `no RED episodes … that is the goal` with zero samples (`app.js:478-479`).
Harness mode replaces these with the following, each pinned by the fixture test on an empty data dir:

| View | With zero runs | Never |
|---|---|---|
| Overview | row count, "no runs recorded yet", health shown as `—` (status.json `health_pct: null`), paused banner if `state.json` says so | `100%`, `0/1`, `✔ all clear`, `0 missed` as a green |
| Loops | every row present, state `NO-DATA` (grey), last run `—` | a green or red dot |
| Topology | nodes from the registry with `depends_on` edges; all nodes grey | an invented hub or edge |
| Breakers | "no breakers" (ledger empty or absent) | — |
| History | "no history yet" | "no RED episodes … that is the goal" |
| Runbook | each row's `runbook_md` or "no runbook" | — |

The SPA also gains the `UNKNOWN`/`U` rendering it lacks today (its status tables `STATUS_SYM` at `app.js:12-14` and the chip styles `.chip.NA` at `app.css:70-74` and the cell styles `.cell.N` at `app.css:112-116` have no UNKNOWN entry).

**Implementation markers (harness mode, `generate-loop-viz.py` at the LIVE tree — every line number below is
re-checked against the source by a citation checker that is not shipped, so a generator edit that moves one fails that check until this paragraph is updated):**
`health_pct` null with zero runs (`if denom else None`, `:654`); row status `"NO-DATA"` with detail `["no runs recorded yet"]` (`:567`) and
`"DISABLED"` with detail `["disabled in registry.json (enabled: false); run records ignored"]` (`:565`); `missed_fires_total` null (`:671`); history not written when there are zero runs (`if runs:`, `:694`);
`mode: "harness"` (`:664`), `tmux_observation: "NOT-OBSERVED"` (`:666`), `registry_drift` + `unclaimed_tags` (`:667`),
`"max_attempts": max_att` (`:676`, null without a ledger), `paused` + `pause_reason` (`:677-678`); every row carries
`expected_sessions: []` (`:621`) and `missing_sessions: []` (`:622`); history letters `E` = NO-DATA and `D` = DISABLED join
the letter table (`"NO-DATA": "E", "DISABLED": "D"`, `:41`); the band constants `HARNESS_FRESH_FACTOR = 2` / `HARNESS_MISSED_FACTOR = 6` (`:485-486`).

## 6. DECISION — mapping the harness's own loops (what the adapter writes)

Source formats quoted from the harness repository at the revision the adapter was written against (the line numbers below refer to that revision). Every `log()` line is `date -u +%Y-%m-%dT%H:%M:%SZ` + space + message (`loop.sh:123-125`).

| Harness loop | Where it fires | Evidence line / stamp | Registry row | Run record |
|---|---|---|---|---|
| Autonomy loop | `Container-Root/agent/loop.sh` (`loop-start.sh:37`), interval `AUTONOMY_LOOP_INTERVAL` 300 s | `loop.sh:248` writes `FIRE N END rc=R duration=Ds` to loop.log (branch fix/loop-fire-outcome: `… outcome=<word>`, rc forced 97 when outcome≠ok, `:253-254`) | `autonomy-loop`, kind `loop`, `cadence_s` 300, tags `["harness-autonomy-loop"]` | one per END line: `ts` = line ts, `rc` = R, `dur_s` = D, `note` = outcome when present |
| slack-watch | per fire from `loop.sh:180`, or from slack-chat | `/wd/slack/last-watch` stamp `TS new=N replies=N inbox=N` (`slack-watch.sh:278-279`) | `slack-watch`, kind `watch`, cadence = the autonomy interval, `depends_on: ["autonomy-loop"]` | one per stamp change: `ts` = stamp TS, `rc` 0 (a stamp is a completed poll; the source has no rc), `dur_s` null, `note` = `stamp: new=… replies=…` |
| slack-chat | `slack-chat.sh` daemon, poll `SLACK_CHAT_POLL_INTERVAL` | `slack-chat.sh:281/:300` write `CHAT MODEL ts=… rc=R duration=Ds posted=…` to chat.log | `slack-chat`, kind `loop`, `cadence_s` = the configured expectation or `enabled:false` when chat is off | one per MODEL line: `ts` = line ts, `rc` = R, `dur_s` = D, `note` = `ts=<slack ts> posted=<yes|no>` |
| comms-watch | `scheduler.py` daemon (`comms-watch-start.sh:70`) | `watch.log` `cycle: polled=N fresh=N unattended=N next-fire<=Ns [answered=N]` — **no timestamp, no rc** (the comms engine's scheduler, at the revision the adapter was written against) | `comms-watch`, kind `watch`, `cadence_s` = the configured cycle | one per new cycle line: `ts` = adapter read time, `rc` 0, `dur_s` null, `note` = the line + `"ts=adapter-read-time"` |
| kanban-sync / plane-sync | per fire from `loop.sh` (branch ux/stack-fixes only, `:129-138`), seed once per start | `loop.log` `FIRE N PLANE|KANBAN … [not mirrored (rc=R …)]` (`:173-175,194-196`); seed exit status in `${UI_DIR}/kanban-seed.done` / `plane-seed.done`; exit codes 0/2/3/4 | `kanban-sync`, `plane-sync`, kind `sync`, `depends_on: ["autonomy-loop"]`, `enabled` = URL set | one per PLANE/KANBAN line: `ts` = line ts, `rc` = R (0 when the line carries none); seed: one record from the `.done` status |
| hindsight seed | one-shot at startup (`startup.sh:20-25`) | `/wd/hindsight-seed.stamp` `seeded_at=<UTC>`, `items=N`, `bank:<name>=N` (`hindsight-seed.sh:276-286`) | `hindsight-seed`, kind `seed`, `cadence_s` null | exactly one record: `ts` = `seeded_at`, `rc` 0 if the stamp is complete, `dur_s` null, `note` = `stamp: items=N` |

Rules for the adapter: never fabricate an rc or a timestamp without saying so in `note`; never write into
`out/`; append `runs.jsonl` under a lock; regenerate `registry.json` whole on each run; re-run the generator
after writing. An adapter-chosen `rc 0` for a stamp counts toward `ok`/`success_pct` — that is the honest
reading of "the poll completed", and the `note` says it is a stamp.

## 7. DECISION — serving inside the harness and /act

* The server binds `127.0.0.1:<PORT_INTERNAL_LOOP_DECK>` and the harness home proxy publishes it under a
  path prefix. The tmux-agent precedent (`home-render.sh:419-431`) proxies `/tmux-agent/` with
  `proxy_set_header Origin $tmux_agent_origin` from a `map $http_origin` (`:280`) and a `sub_filter` for absolute
  API paths. The Deck SPA must therefore fetch **relative** URLs — today `status.json`/`history.jsonl` are relative
  (`fetch('status.json'` at `app.js:98,109`, `fetch('history.jsonl'` at `:101`) but `fetch('/act'` is absolute (`app.js:78`); the release fixes it and pins it with a
  static test.
* `/act` is **refused** (403, audited `DENIED-ACT-DISABLED`) whenever `LOOP_DECK_ACT_TOKEN` is unset; with the
  token set, a POST must carry header `X-Loop-Deck-Token: <token>` or is refused (403 `DENIED-TOKEN`); the
  existing checks then apply unchanged in this order (`deck-server.py:84-129`): Origin ∈ {`f"http://127.0.0.1:{self.server.server_port}"`,
  `http://localhost:<port>`}, body size, JSON, verb whitelist, `gate check-action deck-<verb>`, action lock,
  `gate paused`. Behind the proxy the Origin check will deny even token-bearing POSTs unless nginx rewrites
  `Origin` as the precedent does; v1 ships with actions OFF and documents this.
* The SPA learns the state from `GET /__act` → `{"enabled": false}` and hides the buttons with an honest
  "actions disabled in this deployment" note; it never probes with a POST.
* Static serving is an allowlist (`/`, `index.html`, `app.js`, `app.css`, `mermaid.min.js`, `status.json`,
  `history.jsonl`, `acks.jsonl`, `/__version`, `/__act`); everything else 404 and no directory listings. The
  Runbook's raw-data links list only those files; `runs.jsonl` is an adapter INPUT under the data dir, never served.
  MEASURED today the server publishes all of `--root` with listings (`/driver/`, `/state-head.snapshot`,
  `/refresh.log`, `/runs.jsonl` all return 200), which the harness must not inherit.
* What `/act` does today and why it stays behind the token (audit §4.6): `pause` creates the kill switch every
  fleet loop honours; `resume` lifts an emergency stop written by anyone; `rerun-viz` spawns the full fleet
  generator; `ack` rewrites the page for 24 h; every POST appends an fsynced audit row with no rate limit on
  denials; the Origin check is not authentication.

## 8. For the record — fleet-mode probes and fleet-specific code that harness mode skips or scrubs

Probes (audit §1.17): `["tmux", "ls", "-F", "#{session_name}"]` (`:49`), `crontab -l` (`:111`), `pgrep -f {base} | head -1` (`:239`),
`["git", "-C", REPO, "fetch", "-q", "origin", "master"]` (`:467`, network), `show origin/master:STATE.md` (`:469`),
plus the syslog reads (`" CRON["`, `:86-104`) and the refresh script's lint run and A1 step (`tools/card_red_loops.py --apply`, in the refresh script, which is not shipped).

Fleet-specific code paths to neutralise in the release (audit §5): the `agentic-fixer` special case
(`"agentic-fixer" in name`, `generate-loop-viz.py:305`), the fleet driver vocabulary
`("tmux", "hub", "claude session", "per-invocation")` (`:321`), the fixed hub node (`:345`) emitted into `topology_mmd`, the hard-coded
slug and run-log tag of the Deck's own fleet row (`:541`, `:544`), its `fleet-members-lint:` detail (`:561`), the `origin master` layout
(`origin/master:STATE.md`, `:467-469`), the `note`/`evidence_note` strings carrying fleet dates and ids (`:610-627`), the pause-file default
of the fleet host (`deck-server.py:33`, `app.js:415,531`), port `8091` defaults, and the fleet's tracker
card ids and the private repository's name in comments and `index.html:25`. The leak gate's denylists
(precedent `leak-scan.sh` gates 2g and 2k) fire on all of these, which is the proof they are gone.

Fleet-side defects the audit MEASURED, tracked separately (not harness scope): the @reboot liveness
probe self-matches its own shell and renders a dead daemon GREEN (`pgrep -f {base} | head -1`, `:239`); a parseable non-object ledger or
entry, or a `runs.jsonl` `ts` that is null/number, exits 1 with a traceback and leaves `status.json` stale
(table 1.18; an UNPARSEABLE ledger is silently `ledger = {}`, `:481-484`); the refresh script exits with the lint's code and swallows the
generator's (`LOOP_MEMBERS_LINT_RC="$MEMBERS_RC"` and `exit "$MEMBERS_RC"` in the fleet's refresh script, not shipped); the SPA lacks UNKNOWN rendering; the server lists
its whole root.

## 9. Worked examples and tests

* `examples/{registry.json,runs.jsonl,loop-ledger.json,state.json}` — three rows, four
  records, one breaker, not paused — exactly the shapes above (the release tree's golden test input).
* `tools/check-contract.py [DATA_DIR]` — exit 0 on the examples, 2 on any §3/§4 violation;
  `--self-test` plants 22 defects and requires each to be rejected by its own rule (contract acceptance check 1; the count here is
  checked against the validator's list by the citation guard).
* Release-tree tests: empty data dir → the §5 markers; examples → golden fields; drift → exit 3;
  malformed → exit 2; probe shims prove no `crontab`/`tmux`/`git`/`journalctl` call leaves harness mode;
  token refusal per verb; no absolute fetch URL in `app.js`.
* Doc-truth: the audit's own 68 Deck-side citations were checked mechanically by its author; THIS document's
  citations were checked by an independent review (92 Deck-side, one mismatch — the drill-down route lines — fixed in
  v0.4). The contract acceptance check 4 guard is a citation checker that stays with the source: every in-repo citation must
  resolve to the marker quoted beside it, at its declared revision or at the LIVE tree (exit 1 on drift; other repos'
  files are listed, never checked); the release tree re-pins
  them to its own layout.
