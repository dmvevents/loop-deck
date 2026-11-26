# Loop Deck

Loop Deck is a loopback-only observability console for scheduled automation loops. It has three parts:
a static single-page app (`app/`), a generator that writes `out/status.json` and appends `out/history.jsonl`
(`generate-loop-viz.py`), and a same-origin server for the page plus a gated `POST /act` endpoint
(`deck-server.py`). This tree is Loop Deck 1.7.1.

## Release notes

Loop Deck 1.7.1:

- `generate-loop-viz.py` no longer runs commands through a shell. In fleet mode it ran `crontab -l` and `git -C <checkout> show origin/master:STATE.md` through `/bin/sh` (`subprocess.run(..., shell=True)`), so a space in the checkout path split it and a `$(...)` in it ran. Its `sh()` helper now takes an argument list and starts each program directly. Both commands print the same output as before; the dropped `2>/dev/null` only discarded stderr, which the helper already captured and ignored.
- Harness mode, the supported mode of this tree, runs neither command. The page (`app/`), the data contract (`docs/CONTRACT.md`) and the examples are unchanged from 1.7.0, and `deck-server.py` changes only its version (`/__version` answers `1.7.1`), so upgrading means replacing the tree.
- New test: `tests/test_no_shell.py` fails if a shipped program starts a shell again.
- `LICENSE` now carries the full standard MIT text; the 1.7.0 copy stopped inside the warranty disclaimer and had no liability clause.
- New file: `THIRD-PARTY-LICENSES` lists the runtime dependencies: none.

## Supported mode

Harness mode is the supported mode of this tree (docs/CONTRACT.md §1). Fleet mode stays in the source repository and is not supported from the release tree: the fleet-only identifiers in its code were rewritten by declared rules, which changes nothing that runs in harness mode.

## Install

There is nothing to build: the Python standard library is the only dependency.

    git clone <this repository> loop-deck && cd loop-deck
    python3 -I deck-server.py --help

## Runtime requirements

- Python 3.9 or newer (the code uses 3.9 features; developed on 3.12). No third-party packages.
- A browser with JavaScript. The page fetches only from the server's own origin (CSP `'self'`).
- Optional: `mermaid.min.js` (mermaid 11, MIT) placed beside `index.html` in the served directory renders the
  Topology and Runbook diagrams. It is not shipped here; without it the page shows each diagram's source.
- Harness mode: set `LOOP_DECK_MODE=harness` and `LOOP_DECK_DATA_DIR=<abs dir>`; the generator then reads only the adapter files in that directory and writes `out/` under it (docs/CONTRACT.md §1). Without `LOOP_DECK_MODE` the generator falls back to fleet mode, which this tree does not support.

## Run

    export LOOP_DECK_MODE=harness LOOP_DECK_DATA_DIR=/abs/data   # adapter-written files (docs/CONTRACT.md §3)
    python3 -I generate-loop-viz.py          # exit 0 rendered, 2 bad input, 3 registry drift
    python3 -I deck-server.py --port 8091 --root "$LOOP_DECK_DATA_DIR/out"   # binds 127.0.0.1 only
    ssh -L 8091:localhost:8091 <host>          # from your workstation; then open http://localhost:8091

The server binds 127.0.0.1 only and asserts it at startup. Never publish the port beyond loopback; reach it
through an SSH tunnel or a same-host reverse proxy. The page fetches every URL relative to its own path, so a same-host reverse proxy may serve it under a path prefix (docs/CONTRACT.md §7).

## Environment variables

| Variable | Read by | Meaning |
|---|---|---|
| `DECK_GATE` | `deck-server.py` | gate script run as `bash $DECK_GATE check-action deck-<verb>` and `bash $DECK_GATE paused` (default `loop-gate.sh` beside deck-server.py; a missing or failing gate refuses every verb) |
| `DECK_PAUSE_FILE` | `deck-server.py` | pause file the `pause` verb creates and `resume` archives (default `~/.loop-deck/LOOP_PAUSE`; the gate script must read the same file) |
| `LOOP_DECK_ACT_TOKEN` | `deck-server.py` | enables `POST /act`: unset or empty refuses every POST (403 `DENIED-ACT-DISABLED`); set, every POST must carry it in `X-Loop-Deck-Token`, compared in constant time, else 403 `DENIED-TOKEN` (docs/CONTRACT.md §7); `GET /__act` reports only whether actions are on |
| `LOOP_DECK_DATA_DIR` | `deck-server.py`, `generate-loop-viz.py` | harness mode: absolute path of the adapter-written data directory (docs/CONTRACT.md §1); unset, relative, missing or unreadable is a hard error (exit 2) |
| `LOOP_DECK_MODE` | `deck-server.py`, `generate-loop-viz.py` | `harness` selects harness mode for the generator and the server (docs/CONTRACT.md §1); unset or anything else is fleet mode, which a release tree does not support |
| `LOOP_MEMBERS_LINT_RC` | `generate-loop-viz.py` | fleet mode only: the exit code of an external membership check, folded into the health of the generator's own refresh row when that row exists |

deck-server.py passes its environment to the generator it spawns, without `LOOP_DECK_ACT_TOKEN`.

## Live actions (`POST /act`)

`POST /act` is refused (403, audited `DENIED-ACT-DISABLED`) unless `LOOP_DECK_ACT_TOKEN` is set. With it set, every POST must carry `X-Loop-Deck-Token: <token>` (403 `DENIED-TOKEN` otherwise); the loopback Origin allowlist, the static verb whitelist (`rerun-viz`, `pause`, `resume`, `ack`), the gate script and the pause check still apply after the token. The page reads `GET /__act` and hides the buttons when actions are off.

## Layout

| Path | What it is |
|---|---|
| `deck-server.py` | same-origin server: static files from `--root` plus `POST /act` |
| `generate-loop-viz.py` | the generator (status + history) |
| `app/` | the page: `index.html`, `app.js`, `app.css` |
| `docs/CONTRACT.md` | the harness-mode data contract |
| `examples/` | one worked example per contract input file |
| `tools/check-contract.py` | validates a data directory against the contract (`--self-test` proves it can fail) |
| `tests/` | regression tests |
| `RELEASE.json` | the pinned source of this tree (machine-readable) |
| `.gitignore` | the working files a test run leaves beside the tree (`.tmp/`, `__pycache__/`) |

Not in this tree: these source files stay with the source repository: `check-loop-deck-contract-citations.py` (the contract's citation checker: it reads fleet files and other repositories); `test_loop_deck_contract_citations.py` (that checker's tests); `test_loop_viz_reboot_probe.py` (tests of the fleet-mode @reboot probe).

## Tests

Python runs every suite. node is an optional test dependency: without it on PATH, the rendered checks in `tests/test_loop_deck_spa.py` skip, and each skip names node.

    python3 -I -B tests/test_deck_actions.py
    python3 -I -B tests/test_loop_deck_harness_mode.py
    python3 -I -B tests/test_loop_deck_spa.py
    python3 -I -B tests/test_no_shell.py
    python3 -I -B tools/check-contract.py              # validate examples/ against docs/CONTRACT.md
    python3 -I -B tools/check-contract.py --self-test  # each planted defect must be rejected

| Suite | Measured when this tree was cut |
|---|---|
| `tests/test_deck_actions.py` | 69 run, all pass |
| `tests/test_loop_deck_harness_mode.py` | 21 run, 1 skipped by design: fleet-only suites are not part of this tree |
| `tests/test_loop_deck_spa.py` | 8 run, all pass (with node on PATH) |
| `tests/test_no_shell.py` | 6 run, all pass |
| `tools/check-contract.py --self-test` | passes |

## Pinned source

This tree was cut from the Loop Deck source at commit `4c878e19dc21c25280cbd80ac776393b01aa3794` (git tree `785afbda72872aceb6be94d95c4352da93df69c5`).
It differs from that source only by a flat layout and declared scrub rules, and `RELEASE.json` carries the
same pin (features at this revision: harness_mode=yes, act_token=yes, relative_urls=yes). Do not edit this tree by hand: changes are made at the
source and the tree is cut again.

## License

MIT License: see `LICENSE`, shipped unchanged from the source. Runtime dependencies and their licenses: see `THIRD-PARTY-LICENSES`.
