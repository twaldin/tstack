# tstack

tstack runs pstack's engineering playbooks in [omp](https://github.com/can1357/oh-my-pi) on easl boards. The engine lives in `~/tstack`, the instance and seat state in `~/agents`, and project clones and lane worktrees in `~/worktrees`. Commands use Python's standard library and pass through errors from git, gh, and systemd. Each command has `--help`.

## Setup

`tstack setup` links the engine and instance skills, agent types, and commands, and writes `~/.omp/agent/AGENTS.md` and the systemd update units. It replaces only its own links and files. Install git, Python 3.10 or newer, authenticated `gh`, omp, and easl, then clone the engine to `~/tstack` and the instance to `~/agents`. Run `~/tstack/bin/tstack setup`, with `--machine <name>` if needed. On Linux, enable hourly updates with `systemctl --user enable --now tstack-update.timer`. On macOS, setup skips systemd units, and coordinators run `tstack update` on heartbeat. `TSTACK_HOME`, `TSTACK_INSTANCE`, `TSTACK_MACHINE`, and `TSTACK_WORKTREES` override the engine, instance, machine, and worktree locations. `TSTACK_SEAT` selects a seat instead of the nearest ancestor directory with `state/`.

Setup manages two full git checkouts with the existing git authentication:

| Source | Checkout | Installed skill |
| --- | --- | --- |
| [twaldin/frontend-studio](https://github.com/twaldin/frontend-studio) | `~/worktrees/frontend-studio/main` | `~/.omp/agent/skills/frontend-studio` links to the checkout root. |
| [mattpocock/skills](https://github.com/mattpocock/skills) | `~/worktrees/mattpocock-skills/main` | `~/.omp/agent/skills/grilling` links to `skills/productivity/grilling` in the checkout. |

Frontend-studio's `SKILL.md` references `../grilling/SKILL.md`. Setup also links `~/worktrees/frontend-studio/grilling` to the grilling skill root, so that reference works through both the installed skill link and the canonical checkout path. Grilling has no further sibling-skill dependencies. Only these two skills are installed from the managed sources. Full checkouts retain their source and license files. There are no gitlinks or vendored dependency files in the instance.

Engine, instance, and machine skills override either managed default, in that order. The canonical sibling link continues to use the managed grilling source. Setup preserves user-owned files, directories, and unrelated symlinks at skill destinations and the sibling path, and reports collisions. It also preserves content that blocks a checkout. A failed clone, missing required `SKILL.md`, or unresolved dependency makes setup return nonzero while other setup steps continue. Git failures include git's native error. Repeated setup keeps existing managed links.

Setup does not install the studio's JavaScript dependencies or start a server or daemon. Follow the skill when you need the studio.

## Update

`tstack update` pulls the engine and both installed managed checkouts with `git pull --ff-only`, fetches the instance, syncs upstream files without overwriting changed local files or seat state, and reruns setup. Setup clones either managed checkout if it is missing and restores missing skill and sibling links. Updates do not reset, stash, or discard local checkout edits. A failed pull reports git's native error and makes update return nonzero, even if the other repositories and setup succeed. The hourly systemd timer runs the same command.

## Commit

`tstack commit "<seat>: <what>"` commits the current seat's `state/` and pushes to `main`. Each `--path <path>` adds a path relative to the instance root. The command uses a private index based on `origin/main`, includes deletions, respects `.gitignore`, and retries rejected pushes. It skips changed files over 1 MiB and reports their paths and staged sizes. Afterward, it advances local `main` when possible and syncs changed upstream files outside `state/` only if their local contents still match old `main`. It never stashes or rewrites seat state. `committed` lines identify pushed paths. `checkout: left` identifies preserved state files, and `skipped` reports local or staged edits. On a sync error, it restores completed checkouts and index updates that still match the attempted tip, preserving intervening edits and returning the original error. After successful rollback, rerunning syncs the latest upstream tip. Abrupt termination or a rollback error can leave partial changes.

## Board

`tstack board` draws the assistant's board from local seat state and easl health, without GitHub or Linear calls. The first group, **You**, holds open questions with no key or a `tstack/*` or `show/*` key, a panel of open `state/todo.json` items, and the digest. Project labels identify who each item belongs to. Questions keyed to another tool stay where that tool placed them.

Each project has a titled, colored easl column with an HTML status panel on top, followed by its coordinator terminal and its `show/<project>/<slug>` tiles in key order. The panel shows `status`, `status_line`, up to three `next_steps`, tasks with their states, running lanes with PR links, and unreadable-state warnings. The column count balances the total width and height, wrapping columns into rows when needed. You and the assistant occupy the first row at the left. Published tiles keep their IDs, content, and chosen sizes. Publish them with `object.upsert`, setting `w` and `h` but omitting positions.

tstack reconciles its `tstack/*` keys in batches of at most 50 operations. It deletes keys no longer drawn and retires the old project cards. Its answered and cancelled questions are deleted only after their close time is at least 30 minutes old. A question already in You keeps its slot after closure until easl archives it or retention expires, so it does not overlap other cards or shift every column. Unrelated objects stay untouched. The columns keep their current horizontal origin when clear, including when foreign tiles occupy gaps below short columns. Otherwise, tstack searches rightward from zero, moving past only objects that intersect a column. An unchanged redraw writes nothing. Each draw reports `layout.check`, labeled `approximate` when easl explicitly flags an approximate finding and `precision unreported` otherwise. An absent flag does not prove exact measurement.

Project seats leave drawing to the assistant. `--print` prints the local snapshot without drawing, including from project seats. Each seat's `state/status.json` holds only `project`, `status`, `status_line`, and `next_steps`. `state/todo.json` holds `items[]` with `id`, `title`, `steps`, and `done`. An item's `project` or `with` identifies its project. Without either, tstack matches a project prefix in the title or uses the source seat.

## Lane

`lane start <name> --repo <owner/repo or path> --brief <file> --max-time <duration>` starts detached omp in a `lane/<id>` worktree through `systemd-run --user` on Linux or a new POSIX session with `nohup` on macOS. `--base`, `--model`, `--thinking`, and `--memory` select run settings. macOS ignores `--memory` and stops the run's process group by its recorded leader PID. Records and final answers live in `<seat>/state/lanes/<id>/`, with condensed events and run output in `<seat>/state/run/lanes/<id>/`. New streams keep only session ids, assistant text and total costs, and tool names with intent or the first 200 characters of serialized arguments. omp keeps the full tool results and thinking in its session. Existing streams are not rewritten. `list --all` reads cached records without GitHub calls or file writes. `logs` reads the stream without refreshing. Use `show` or `wait <id>...` to refresh run results, `resume` to continue, and `stop` to end a run. `show` discovers the PR. Refresh checks a known PR after the run exits and records merges made outside `lane merge`. After a merge, refresh removes the worktree and branch only with a clean tree and HEAD matching the merged head. `merge` performs the same cleanup after merging, and `clean` discovers the PR and cleans on demand. Dirty trees and unpushed commits stay untouched. Cleanup refusals and errors appear on stderr and in the lane's saved cleanup result.

Briefs carry `PLAYBOOK: <name>`, where the name is a file stem in `pstack/skills/poteto-mode/playbooks/`, or `none`. The field label is case-insensitive. `start` saves the field and `show` displays it. A nonexistent name produces a fact line on stderr with valid names and still starts the lane. An absent field also leaves startup unchanged.

Both `start` and `resume` require `--max-time <duration>`, with no default or inherited budget. Use an omp duration such as `20m` or `1h`. Each run passes the budget to omp, saves it as `max_time`, and displays it on its `lane show` run row. After a budget expires, the coordinator reads the result and chooses a new explicit budget before resuming.

`lane resume <id> <text> --max-time <duration> --memory <size>` changes the Linux memory cap and saves it for later runs. Without `--memory`, resume reuses the saved cap. Both commands pass the size to systemd for validation. A rejected resume leaves the previous cap and run history intact. macOS ignores the cap.

For nohup runs, liveness uses the recorded PID and its process start time, not its command title. An exit file ends the run even if the runner is still shutting down. Otherwise, a missing PID, a zombie, or a different start time ends it. Failed, empty, or timed-out `ps` output does not prove death. If launch-time inspection failed, or an older run has no start time, an existing PID stays live until it disappears or an exit file arrives. `show` and `wait` repair a stale ended record when its process lives. `list` displays that run as running without saving changes. `resume` checks liveness again under the lane lock before launch and refuses while the previous run lives.

Sending a signal requires confirmed identity. A run with a recorded start time must still match it. For older runs without one, `stop` checks the runner's command for the full run path. An unconfirmed identity reports the PID and sends no signal. Linux uses boot ID and process start ticks for PID identity and retains systemd's active state until unit teardown captures the process inventory.

Linux runs capture systemd's exit result before unit collection, with the peak memory and cap when available. `show` and `wait` report failures such as `oom-kill (peak 8.0G of 8G cap)`. `list` includes the failure reason in its status column and reads captured results without changing files. macOS reports the process exit code without systemd memory data.

Each run marks omp and its descendants with a unique `TSTACK_LANE_RUN` environment value. The collector excludes the runner pipeline, omp main pid, and omp helpers with an argument starting `__omp_worker`. It never saves omp's argv and limits saved commands to 120 characters. Linux snapshots the unit's cgroup before teardown, including children that clear their environment; a same-uid `/proc` marker scan also finds survivors outside the unit. macOS scans same-uid environments with `ps -wwE`. Linux cgroup members are labeled `killed by unit stop`; Linux survivors outside the unit are labeled `still running after the run`. Stop and OOM snapshots include their end reason. `show` prints each non-empty historical snapshot; `wait` prints the latest run's snapshot. Empty snapshots print nothing, and cleanup preserves captured facts.

macOS reports only environment-visible processes. Its non-empty inventory says `still running (partial: macOS hides environments of system binaries)`. The kernel can omit inherited environments for system shells and `sleep`, so these processes can remain running without appearing in the inventory. An empty snapshot prints no extra line.

A failed PR refresh reports the native error on stderr without blocking local run operations. Missing merge SHAs never authorize automatic cleanup.

`lane clean <id>` and automatic post-merge cleanup check the PR head, clean status, ignored files, and process working directories before removal. Recorded lanes have no 24-hour delay after a terminal PR. Lanes without a PR retain the published-commit or local-base check. A failed gate leaves the tree and names the gate. Removal uses `git worktree remove` without `--force`.

Every ignored file must sit under a regenerable directory in `REGENERABLE_DIRS` in `bin/lane`, or name a file in `REGENERABLE_FILES`. Only directories at or below the collapsed ignored root count; a tracked `build/` parent does not authorize an ignored `.env` file. An ignored parent directory is safe only when every file below it passes, so `scripts/node_modules/` alone is safe but adding `scripts/keep.txt` blocks removal. Proof files inside regenerable directories below an ignored root, such as `.local/acceptance/build/result.proof`, are removable. Process inspection uses noninteractive `sudo lsof` on Linux and macOS. An inspection failure keeps the tree.

`lane clean --list` reads unrecorded trees below `~/worktrees`, without following directory symlinks or entering checkout roots. It excludes main checkouts and lanes recorded in the current seat or instance seats. Each terminal-PR row shows its state and failed gates, including the 24-hour terminal-age gate. The final line counts unrecorded, finished, eligible, blocked, and inspection-error trees. Listing never removes a tree or changes lane records.

## Watch a PR

`watch-pr <owner/repo>#<number>` or `watch-pr <url>` prints PR facts once with `--once` as the default. `--until change`, `--until checks`, and `--until merged` wait for changed facts, completed checks, or a merged or closed PR. Every mode stops on a closed PR. `--interval <seconds>` sets the polling interval, and `--json` prints JSON. Reports include checks, reviews, comments, unresolved threads, mergeability, and the head SHA. Merged reports include the merge SHA, runs on that SHA, and their deploy or release jobs. The command never writes to GitHub.

`lane merge` refuses to merge unless a structured cross-family `READY` verdict covers the exact current PR head. `watch-pr` reports the same verdict fact without enforcing it. The PR body carries `tstack-author-model: <provider/model>` when the lane record has no resolved model. Review comments carry the `tstack-verdict` JSON line documented in `roles/lane.md`. The latest current-head record wins. Prose-only reviews, model aliases, unknown families, stale heads, and dismissed reviews do not establish approval.

## Update pstack

Run `git fetch origin` before `pstack-bump <sha>`. The command restores a missing local `pstack-upstream` ref from the fetched snapshot, imports upstream pstack, merges it into the current branch, and removes paths listed in `pstack/DROPPED`. Resolve reported conflicts while keeping local edits, then publish the snapshot with `git push origin pstack-upstream` alongside the PR branch. `pstack/` holds the fork, `global.md` and `roles/` hold agent rules, and `agents/` holds model-bound agent types. pstack is MIT licensed with its notice in `pstack/LICENSE`.

The vendored snapshot is cursor/plugins `e73df471cd4bd918367e99f3dd9c51bd53ddf5f0`. It includes `/statechart` for formal-verification results. `/ketchup-video` remains dropped with its `/ketchup` dependency.
