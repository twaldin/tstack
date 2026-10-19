# tstack

tstack runs pstack's engineering playbooks in [omp](https://github.com/can1357/oh-my-pi) on easl boards. The engine lives in `~/tstack`, the instance and seat state in `~/agents`, and project clones and lane worktrees in `~/worktrees`. Commands use Python's standard library and pass through errors from git, gh, and systemd. Each command has `--help`.

## Setup

`tstack setup` links the engine and instance skills, agent types, and commands, writes `~/.omp/agent/AGENTS.md` and the systemd update units, and installs orch's dependencies. It replaces only its own links and files. Install git, Python 3.10 or newer, authenticated `gh`, bun, omp, and easl, then clone the engine to `~/tstack` and the instance to `~/agents`. Run `~/tstack/bin/tstack setup`, with `--machine <name>` if needed. On Linux, enable hourly updates with `systemctl --user enable --now tstack-update.timer`. On macOS, setup skips systemd units, and coordinators run `tstack update` on heartbeat. `TSTACK_HOME`, `TSTACK_INSTANCE`, `TSTACK_MACHINE`, and `TSTACK_WORKTREES` override the engine, instance, machine, and worktree locations. `TSTACK_SEAT` selects a seat instead of the nearest ancestor directory with `state/`.

## Update

`tstack update` pulls the engine with `git pull --ff-only`, fetches the instance, syncs upstream files without overwriting changed local files or seat state, and reruns setup. The hourly systemd timer runs the same command.

## Commit

`tstack commit "<seat>: <what>"` commits the current seat's `state/` and pushes to `main`. Each `--path <path>` adds a path relative to the instance root. The command uses a private index based on `origin/main`, includes deletions, respects `.gitignore`, and retries rejected pushes. It skips changed files over 1 MiB and reports their paths and staged sizes. Afterward, it advances local `main` when possible and syncs changed upstream files outside `state/` only if their local contents still match old `main`. It never stashes or rewrites seat state. `committed` lines identify pushed paths. `checkout: left` identifies preserved state files, and `skipped` reports local or staged edits. On a sync error, it restores completed checkouts and index updates that still match the attempted tip, preserving intervening edits and returning the original error. After successful rollback, rerunning syncs the latest upstream tip. Abrupt termination or a rollback error can leave partial changes.

## Board

`tstack board` draws the assistant's board from local seat state and easl health, without GitHub or Linear calls. The first group, **You**, holds every open question, a panel of open `state/todo.json` items, and the digest. Project labels identify who each item belongs to.

Each project has a titled, colored easl group with an HTML status panel, its coordinator terminal, and its `show/<project>/<slug>` tiles. The panel shows `status`, `status_line`, up to three `next_steps`, tasks with their states, running lanes with PR links, and unreadable-state warnings. Groups wrap in rows. Published tiles keep their IDs, content, and chosen sizes. Publish them with `object.upsert`, setting `w` and `h` but omitting positions.

tstack reconciles its `tstack/*` keys in batches of at most 50 operations. It deletes keys no longer drawn and retires the old project cards. Answered and cancelled questions are deleted only after their close time is at least 30 minutes old. Unrelated objects stay untouched. An unchanged redraw writes nothing. Each draw reports `layout.check`.

Project seats leave drawing to the assistant. `--print` prints the local snapshot without drawing, including from project seats. Each seat's `state/status.json` holds only `project`, `status`, `status_line`, and `next_steps`. `state/todo.json` holds `items[]` with `id`, `title`, `steps`, and `done`. An item's `project` or `with` identifies its project. Without either, tstack matches a project prefix in the title or uses the source seat.

## Lane

`lane start <name> --repo <owner/repo or path> --brief <file>` starts detached omp in a `lane/<id>` worktree through `systemd-run --user` on Linux or a new POSIX session with `nohup` on macOS. `--base`, `--model`, `--thinking`, and `--memory` select run settings. macOS ignores `--memory` and stops the run's process group by its recorded leader PID. Records and final answers live in `<seat>/state/lanes/<id>/`, with condensed events and run output in `<seat>/state/run/lanes/<id>/`. `list --all` reads cached records without GitHub calls or file writes. `logs` reads the stream without refreshing. Use `show` or `wait <id>...` to refresh run results and compact old streams, `resume` to continue, and `stop` to end a run. `show` discovers the PR. Refresh checks a known PR after the run exits and records merges made outside `lane merge`. After a merge, refresh removes the worktree and branch only with a clean tree and HEAD matching the merged head. `merge` performs the same cleanup after merging, and `clean` discovers the PR and cleans on demand. Dirty trees and unpushed commits stay untouched. Cleanup refusals and errors appear on stderr and in the lane's saved cleanup result.

A failed PR refresh reports the native error on stderr without blocking local run operations. Missing merge SHAs never authorize automatic cleanup.

## Watch a PR

`watch-pr <owner/repo>#<number>` or `watch-pr <url>` prints PR facts once with `--once` as the default. `--until change`, `--until checks`, and `--until merged` wait for changed facts, completed checks, or a merged or closed PR. Every mode stops on a closed PR. `--interval <seconds>` sets the polling interval, and `--json` prints JSON. Reports include checks, reviews, comments, unresolved threads, mergeability, and the head SHA. Merged reports include the merge SHA, runs on that SHA, and their deploy or release jobs. The command never writes to GitHub.

## Update pstack

Run `git fetch origin` before `pstack-bump <sha>`. The command restores a missing local `pstack-upstream` ref from the fetched snapshot, imports upstream pstack, merges it into the current branch, and removes paths listed in `pstack/DROPPED`. Resolve reported conflicts while keeping local edits, then publish the snapshot with `git push origin pstack-upstream` alongside the PR branch. `pstack/` holds the fork, `global.md` and `roles/` hold agent rules, and `agents/` holds model-bound agent types. pstack is MIT licensed with its notice in `pstack/LICENSE`.
