# tstack

tstack is a small agent setup for [omp](https://github.com/can1357/oh-my-pi): one assistant per operator, one coordinator per active project, and headless lanes that each take one task from brief to merged PR. Agents talk through files: a brief goes down, one report comes up, and every seat's state lives in git. Boards in easl show that state and carry the operator's decisions.

The engine is identical on every host and holds nothing personal. Everything about one operator lives in their instance repo.

## The three repos

| Repo | Checkout | Holds |
|---|---|---|
| engine (this repo) | `~/tstack` on every host | rules, roles, playbooks, skills, agent types, the `tstack` and `lane` CLIs, unit templates |
| instance (private) | `~/agents` on the operator's hosts | the operator's rules, machines, project files and seat state |
| history (private) | never checked out by agents | decisions, audits, board exports and retired agent files |

Project code never sits under `~/agents`, because omp loads every ancestor `AGENTS.md`. Each project is cloned at `~/worktrees/<project>/main`, with one worktree per lane beside it.

## How it fits together

- `global.md` holds the rules every agent loads. `tstack setup` writes `~/.omp/agent/AGENTS.md` from it, the instance's `global.md` and the machine's `global.md` if present.
- A role is a directory whose `AGENTS.md` links to a file in `roles/`; the agent's working directory picks its role. The assistant runs in `~/agents/assistant`, coordinators in `~/agents/projects/<name>`.
- Lanes run `omp -p` in their worktree with `roles/lane.md`, a playbook from `playbooks/`, the project's `pipeline.md` and the brief appended to the system prompt.
- Seats wake through `tstack wait`, a background job that exits when something needs them. A timer runs `tstack check --liveness` and restarts a seat whose wait went quiet (never forcing one that works); every `tstack update` run compares the files a seat loads with the ones its omp loaded and, while they differ, asks it to run `tstack restart-self` at a safe point. Either way the seat then gets `roles/restart.md` as a prompt, and the first wait under the new omp records what it loaded.
- `skills/` holds the six skills every agent may use; `agents/` holds the omp agent types that `task` subagents run as.
- Guards in `lane` (a lane may act only on itself; `lane merge` checks proof and checks) prevent mistakes, not attacks: lanes run as the same Unix user with the same `gh` login and could write seat files or call `gh` directly. Delivery of the restart-self request is best effort: if the seat's easl integration drops between tstack's check and the send, easl types the text into its editor.

## Install on a new host

Requirements: git, Python 3.9 or newer, `gh` logged in, omp, and easl (the Mac app, or `easld` on Linux).

1. Clone the engine to `~/tstack` and your instance to `~/agents`.
2. Preview, then apply: `~/tstack/bin/tstack setup --dry-run`, then `~/tstack/bin/tstack setup`. Pass `--machine <name>` when `hostname -s` is not the machine's name in the instance, and `--timers` on macOS to install the launchd timers. Setup moves anything it replaces into `~/.cleanup-quarantine/<date>/` and never deletes.
3. Check: `tstack check --smoke`. It runs a headless omp session and asserts that the global rules, a role, an appended playbook, the skills and the secret guard all load.

| Environment variable | Default | Overrides |
|---|---|---|
| `TSTACK_HOME` | the checkout the CLIs run from, normally `~/tstack` | where roles, playbooks and `global.md` are read |
| `TSTACK_INSTANCE` | `~/agents` | the instance repo |
| `TSTACK_MACHINE` | `hostname -s`, matched to a directory in the instance's `machines/` | the machine name; `--machine` sets it too |
| `TSTACK_WORKTREES` | `~/worktrees` | where project clones and lane worktrees live |
| `TSTACK_SEAT` | the nearest ancestor of the cwd that has `state/` | the seat; `--seat` wins; `lane` sets it for each run |
| `TSTACK_LANE` | none; `lane` sets it for each run | the lane `lane report` and `lane merge` act on |
| `TSTACK_LAUNCHER` | `systemd` on Linux, `process` on macOS | how `lane` starts a run: `systemd-run --user` or a detached process |
| `TSTACK_WAIT_POLL` | `60` | seconds between `tstack wait` polls |
| `TSTACK_WAIT_BATCH` | `180` | seconds `tstack wait` holds non-urgent events to batch them |
| `TSTACK_SMOKE_MODEL` | `@tiny` | the model `tstack check --smoke` runs |
| `TSTACK_LOCK_WAIT` | `30` | seconds a command waits for a lane record, gates or recovery record lock before it reports the lock busy |

## Starting a seat

A seat runs as a terminal tile on an easl board. The assistant's tile lives on the instance's board:

```sh
easl agent spawn --name assistant --command omp --cwd ~/agents/assistant --board <board> \
  --prompt "You are the assistant. Read your role and state, then arm tstack wait."
```

Then record the tile in `~/agents/assistant/state/board.json` so liveness can restart it. The assistant starts coordinators the same way.

## Changing tstack

Agents and people change tstack through PRs; the operator decides each one. `AGENTS.md` holds the rules for that, including the size caps that `tstack check` enforces.

## Upstream and licenses

The playbooks, the coordinator and lane roles and parts of `global.md` are forked from pstack; `upstream.lock` pins every forked source and its commit. pstack's MIT notice is in `playbooks/LICENSE`; the forked Matt Pocock skills and `unslop` keep theirs in their own directories.
