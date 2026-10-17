# tstack

tstack runs pstack's engineering playbooks in [omp](https://github.com/can1357/oh-my-pi) on easl boards. Three small CLIs do what agents repeat: `lane` runs one task as a detached omp session in its own worktree, `watch-pr` reports a PR's facts through merge and deploy, and `tstack` sets a host up and draws mission control on a seat's board. None of them refuses, demands proof or blocks an agent. Errors from gh, git and systemd pass through as errors.

| Path | Holds |
|---|---|
| `pstack/` | The pstack fork. Branch `pstack-upstream` holds unchanged upstream snapshots, `pstack/DROPPED` lists what we leave out, and `git diff pstack-upstream main -- pstack/` lists our edits. |
| `bin/` | `lane`, `watch-pr`, `tstack` and `pstack-bump`. Each takes `--help`. |
| `global.md`, `roles/` | Rules every agent loads, and the assistant, coordinator and lane roles. |
| `agents/` | omp agent types, one model binding each. |
| `systemd/`, `launchd/` | Hourly `tstack update`. |

An instance repo at `~/agents` holds one operator's rules, machines, projects and seat state. Project code lives in `~/worktrees/<project>/main`, with each lane's worktree beside it.

## Install

You need git, Python 3.9 or newer, `gh` logged in, bun, omp and easl.

1. Clone this repo to `~/tstack` and the instance to `~/agents`.
2. Run `~/tstack/bin/tstack setup`, adding `--machine <name>` when the host name differs from the instance's machine directory. It links pstack's skills, the instance's skills, the agent types and the bins. It writes the update units, which carry the engine, instance and machine it ran with, and `~/.omp/agent/AGENTS.md` from the `global.md` files, then installs orch's dependencies. It replaces only its own links and files and reports anything else in the way.
3. Turn on hourly updates once: `systemctl --user enable --now tstack-update.timer` on Linux, or `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tstack.update.plist` on macOS.

| Variable | Default | Sets |
|---|---|---|
| `TSTACK_HOME` | the checkout the CLI runs from | the engine |
| `TSTACK_INSTANCE` | `~/agents` | the instance |
| `TSTACK_MACHINE` | the instance machine directory matching `hostname -s` | the machine |
| `TSTACK_WORKTREES` | `~/worktrees` | where project clones and lane worktrees live |
| `TSTACK_SEAT` | the nearest ancestor of the cwd with `state/` | the seat `lane` acts on |
| `TSTACK_LAUNCHER` | `systemd` on Linux, `process` on macOS | how `lane` starts a run, and whether setup writes systemd or launchd units |

## Lanes

`lane start <name> --repo <owner/repo or path> --brief <file>` makes `~/worktrees/<project>/<id>` on branch `lane/<id>` and runs `omp -p --mode json` there, under `systemd-run --user` on Linux, with `roles/lane.md` appended and the brief as the prompt. A lane's record, brief and final answer live in `<seat>/state/lanes/<id>/`. Each run's json stream, stderr and exit code live in `<seat>/state/run/lanes/<id>/`. `lane wait <id>...` exits when a run ends and prints its final answer, so a seat runs it as a background job to wake on it.

## Commit seat state

Run `tstack commit "<seat>: <what>"` from a seat's directory to commit its `state/` and push to `main`. Each `--path P` adds a path relative to the repo root, such as another seat's intake file. The command builds from `origin/main` in a private index, includes deletions, respects `.gitignore` and retries rejected pushes when origin moves. It skips added or changed files over 1 MiB and prints each path and staged size. `--max-size BYTES` overrides the limit. It never stashes or rewrites state files. After a push it fast-forwards local `main` when possible, refreshes only changed index entries and writes changed upstream files outside every `state/` only when they still match old `main`. It reports committed, synced and skipped paths, and leaves an unpushed local `main` alone.

## Board

`tstack board` draws mission control and arranges the seat's board in horizontal bands. The operator band contains the assistant terminal, the status strip above the operator's to-do and Done tiles, and the assistant's own tiles. The to-do lists every open question. Each project has a band below it, in card order. Moving a project card above or below another card reorders the bands on the next draw. Every region names a project by `project` in its `status.json`, else by its seat directory's name. A failed read shows as a problem, never as zero. `tstack board --print` prints what it read as JSON and draws nothing.

Each project's left block is about 1000 points wide and holds its coordinator terminal, compact project card, and open question tiles. Its showcase is about 4000 points wide. Tiles run left to right in slug order, wrap into rows, and have 40-point gutters. Other keyed tiles made by the seat sit beside its showcase. The tail follows these blocks and holds unkeyed objects made by that seat's terminal in creation order. tstack preserves non-follow objects and leaves unrelated objects alone. Band heights include all their contents so bands do not overlap. Parked projects, including projects without a running coordinator, have a one-line card and reserve no empty showcase space.

Coordinators publish work with `object.upsert` and keys `show/<project>/<slug>`, where `<project>` is the seat directory name, such as `sky`. Any tile type can participate, including images, browsers, HTML, code, changes, and notes. The seat chooses `frame.w` and `frame.h`. Omit `x` and `y` on refresh so a seat does not overwrite the user's placement. tstack changes only the position, never the size, and preserves a showcase tile the user moved. Media files live under `~/agents/showcase/<project>/`; the instance will ignore that directory in git. A project's whole showcase must never be one programmatic HTML page. Use separate clips, screenshots, charts, and interactive tiles that show the work itself.

When a showcase tile leaves its automatic position, tstack checks the newest move in board history. It re-places agent-moved tiles in their band and prints each re-placement. User moves stay pinned. Missing history also keeps a tile pinned, since the log can expire or restart. tstack records its own move entries separately so its draws never count as user moves. Calls use `EASL_TILE_ID` as the caller when it is present.

Showcase position updates carry the object's revision. A concurrent edit raises easl's conflict instead of overwriting the newer frame. Rolled-back batch revisions do not count as moves, so a failed draw cannot pin an agent's tile through its rollback events.

tstack turns off file following on every seat terminal it places and removes those terminals' code follow tiles. Publish code or changes under a `show/<project>/<slug>` key when they belong on the board. Each draw prints the number of showcase tiles and tail objects placed per band and the number of user-moved tiles kept.

Mission-control HTML heights come from `object.measure` at each tile's width. If the board has no attached app, the instance's `machines/<machine>/board.json` can name an app host with `{"measure_host":"history-archive-host"}`. For deckbox, that file is `~/agents/machines/deckbox/board.json`. Measurement uses the existing easl SSH transport and the app's CLI. The board stays on its original host, and the draw prints `heights measured on <host>`. If neither app is reachable, it prints `heights estimated`. Closed disclosures count as one summary row, not their hidden content. This estimate does not model CSS or text wrapping.

The measuring app needs an open board. Native measurement errors, such as no open board or a busy page, pass through rather than claiming no app is attached. Generated mission-control cards are self-contained. Showcase tiles keep their seat's frame and need no measurement. Unreadable machine settings or an unreachable SSH host use the estimate. SSH connections use batch mode and a ten-second connection timeout.

A seat's board is the agents board, the one the assistant's `state/board.json` names, unless the seat's own `state/board.json` or `--board` names another. The assistant draws every project there. A project seat on the agents board draws nothing, so it never overwrites mission control. Its `tstack board` prints that the assistant's board draws it, then the `choice` and `note` lines left on its project tile. A seat on a board of its own gets that seat's mission control there, plus the same lines from the agents board. While the assistant's `board.json` is missing or unreadable, a project seat draws nothing and prints why.

It reads these files from each seat's `state/`:

| File | Holds | Written by |
|---|---|---|
| `status.json` | The project's status: `{project, repos[], where, what, recorded_goal, status, status_line, last_activity, drift, in_flight[], done_recent[], asks[], risks[], next_steps[], evidence[]}`. `status` is one of active, waiting-on-tim, stalled, parked, shipped or dormant. A `done_recent` entry with `kind` release or deploy counts as deployed. | the seat's coordinator, or the assistant |
| `tasks.tsv`, `lanes/`, `intake.md` | Tasks, lane records and unhandled intake. | the seat |
| `todo.json` | The drawing seat's to-do for the operator: `{sync: {date, title}, items: [{id, title, body, due, with[], steps[], links[{label, url}], drafts[{to, channel, text}], done}]}`. A `due` that is a date, an ISO time, today or tomorrow counts down on the board; other words show as written. | the assistant |

Each project card computes a six-stage pipeline. Backlog and Todo come from `tasks.tsv`. Build counts running lanes and in-flight agents. Review counts open PRs on the card's repos, and Merged counts merges in the last 14 days. Deployed counts production deployments and releases from GitHub, plus the `done_recent` releases and deploys whose `ref` GitHub does not list. A check that GitHub refused to start, with a "job was not started" annotation, shows as CI not run with GitHub's reason instead of as failing.

The instance file `remote-seats` lists seats on other hosts, one per line: `<name> <ssh target> <seat dir>`. tstack reads each over ssh with `--redact`, so only counts, ids, states and PR links leave that host.

What the operator leaves on the board prints after the draw, one line each. The seat that owns it acts on it. `choice` and `note` lines come from the project tiles, and a choice prints until `tasks.tsv` reflects it. A `todo-done <id>` line comes from Mark done on the to-do and prints until `todo.json` marks that item done.

## Upstream

`pstack-bump <sha>` snapshots upstream pstack onto `pstack-upstream`, merges it into the current branch, drops the `DROPPED` paths again and prints the conflicts and our edit diff. pstack is MIT licensed; its notice is `pstack/LICENSE`.
