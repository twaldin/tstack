# tstack

tstack runs pstack's engineering playbooks in [omp](https://github.com/can1357/oh-my-pi) on easl boards. Three small CLIs do what agents repeat: `lane` runs one task as a detached omp session in its own worktree, `watch-pr` reports a PR's facts through merge and deploy, and `tstack` sets a host up. None of them refuses, demands proof or blocks an agent. Errors from gh, git and systemd pass through as errors.

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
2. Run `~/tstack/bin/tstack setup`, adding `--machine <name>` when the host name differs from the instance's machine directory. It links pstack's skills, the instance's skills, the agent types, the bins and the update units, writes `~/.omp/agent/AGENTS.md` from the `global.md` files, and installs orch's dependencies. It replaces only its own links and reports anything else in the way.
3. Turn on hourly updates once: `systemctl --user enable --now tstack-update.timer` on Linux, or `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tstack.update.plist` on macOS.

| Variable | Default | Sets |
|---|---|---|
| `TSTACK_HOME` | the checkout the CLI runs from | the engine |
| `TSTACK_INSTANCE` | `~/agents` | the instance |
| `TSTACK_MACHINE` | the instance machine directory matching `hostname -s` | the machine |
| `TSTACK_WORKTREES` | `~/worktrees` | where project clones and lane worktrees live |
| `TSTACK_SEAT` | the nearest ancestor of the cwd with `state/` | the seat `lane` acts on |
| `TSTACK_LAUNCHER` | `systemd` on Linux, `process` on macOS | how `lane` starts a run |

## Lanes

`lane start <name> --repo <owner/repo or path> --brief <file>` makes `~/worktrees/<project>/<id>` on branch `lane/<id>` and runs `omp -p --mode json` there, under `systemd-run --user` on Linux, with `roles/lane.md` appended and the brief as the prompt. A lane's record, brief and final answer live in `<seat>/state/lanes/<id>/`. Each run's json stream, stderr and exit code live in `<seat>/state/run/lanes/<id>/`. `lane wait <id>...` exits when a run ends and prints its final answer, so a seat runs it as a background job to wake on it.

## Upstream

`pstack-bump <sha>` snapshots upstream pstack onto `pstack-upstream`, merges it into the current branch, drops the `DROPPED` paths again and prints the conflicts and our edit diff. pstack is MIT licensed; its notice is `pstack/LICENSE`.
