# tstack

tstack runs pstack's engineering playbooks in [omp](https://github.com/can1357/oh-my-pi) on easl boards. The engine lives in `~/tstack`, the instance and seat state in `~/agents`, and project clones and lane worktrees in `~/worktrees`. Commands use Python's standard library and pass through errors from git, gh, and systemd. Each command has `--help`.

## Setup

`tstack setup` links the engine and instance skills, agent types, and commands, writes `~/.omp/agent/AGENTS.md` and the systemd update units, and installs orch's dependencies. It replaces only its own links and files. Install git, Python 3.10 or newer, authenticated `gh`, bun, omp, and easl, then clone the engine to `~/tstack` and the instance to `~/agents`. Run `~/tstack/bin/tstack setup`, with `--machine <name>` if needed. Enable hourly updates with `systemctl --user enable --now tstack-update.timer`. `TSTACK_HOME`, `TSTACK_INSTANCE`, `TSTACK_MACHINE`, and `TSTACK_WORKTREES` override the engine, instance, machine, and worktree locations. `TSTACK_SEAT` selects a seat instead of the nearest ancestor directory with `state/`.

## Update

`tstack update` pulls the engine with `git pull --ff-only`, fetches the instance, syncs upstream files without overwriting changed local files or seat state, and reruns setup. The hourly systemd timer runs the same command.

## Commit

`tstack commit "<seat>: <what>"` commits the current seat's `state/` and pushes to `main`. Each `--path <path>` adds a path relative to the instance root. The command uses a private index based on `origin/main`, includes deletions, respects `.gitignore`, and retries rejected pushes. It skips changed files over 1 MiB and reports their paths and staged sizes. Afterward, it advances local `main` when possible and syncs changed upstream files outside `state/` only if their local contents still match old `main`. It never stashes or rewrites seat state. `committed` lines identify pushed paths. `checkout: left` identifies preserved state files, and `skipped` reports local or staged edits. On a sync error, it restores completed checkouts and index updates that still match the attempted tip, preserving intervening edits and returning the original error. After successful rollback, rerunning syncs the latest upstream tip. Abrupt termination or a rollback error can leave partial changes.

## Board

`tstack board` draws horizontal bands on the assistant's board from local state and seat health, without GitHub calls or an attached app. Project seats decline draws with a message that the assistant draws it. Bands hold terminals, open questions, fixed-size cards, and `show/<project>/<slug>` tiles at seat-chosen sizes. Cards show status, `status_line`, three `next_steps`, running lanes with PR links, health, and unreadable-state warnings. Parked or down seats have compact cards. tstack owns positions, turns terminal following off, leaves unrelated tiles alone, and reports `layout.check`. Publish work with `object.upsert`, preserving tile sizes and omitting positions. The digest carries operator decisions and completed work. `--print` prints JSON without drawing, including from project seats.

## Lane

`lane start <name> --repo <owner/repo or path> --brief <file>` starts detached omp in a `lane/<id>` worktree through `systemd-run --user`. `--base`, `--model`, `--thinking`, and `--memory` select run settings. Records and final answers live in `<seat>/state/lanes/<id>/`, with condensed events and run output in `<seat>/state/run/lanes/<id>/`. Use `resume` to continue, `wait <id>...` to await completion, `show`, `list --all`, or `logs` to inspect, `stop` to end a run, and `merge` to merge its PR. Refresh uses local state without GitHub calls. After a recorded merge and the run's exit, it removes the worktree and branch only with a clean tree and HEAD matching the merged head. `clean` performs the same cleanup on demand and can discover merges made outside `lane merge`. Dirty trees and unpushed commits stay untouched. Cleanup refusals and errors appear on stderr and in the lane's saved cleanup result.

## Watch a PR

`watch-pr <owner/repo>#<number>` or `watch-pr <url>` prints PR facts once with `--once` as the default. `--until change`, `--until checks`, and `--until merged` wait for changed facts, completed checks, or a merged or closed PR. Every mode stops on a closed PR. `--interval <seconds>` sets the polling interval, and `--json` prints JSON. Reports include checks, reviews, comments, unresolved threads, mergeability, and the head SHA. Merged reports include the merge SHA, runs on that SHA, and their deploy or release jobs. The command never writes to GitHub.

## Update pstack

`pstack-bump <sha>` snapshots upstream pstack onto `pstack-upstream`, merges it into the current branch, removes paths listed in `pstack/DROPPED`, and prints conflicts and the local edit diff. `pstack/` holds the fork, `global.md` and `roles/` hold agent rules, and `agents/` holds model-bound agent types. pstack is MIT licensed with its notice in `pstack/LICENSE`.
