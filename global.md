# tstack: rules for every agent

Agents here run pstack's playbooks and principles inside omp. Before engineering work, read `skill://poteto-mode` and the `omp.md` beside it, which maps pstack's Cursor names to omp, easl, `lane` and `gh`. The instance's rules follow this file and win where the two differ.

## Rules

1. **Tools in code, judgment in prose.** A repeated operation with intricacies gets one CLI that does it fast and correctly. When and whether to use it stays in playbooks and roles.
2. **Tools never block.** A CLI does what it is asked and reports facts: state, what failed, what to try next. Native errors (gh refused, the head moved) pass through as errors. The tool adds no refusals of its own.
3. **Fix a repeated mistake in what a tool does or shows, never with a check.**
4. **Wrap native tools only to add knowledge** (gh, git, easl, omp, systemd).
5. **pstack is the base.** Use its files unchanged where they fit, edit where Cursor is baked in, and drop what no seat uses. `git diff pstack-upstream main -- pstack/` in `~/tstack` is the full list of our edits.
6. **Secrets live in prose.** Never read secret files and never print a secret value, a token or the environment. No code enforces this, so it rests on you.

## Seats

| Seat | Runs as | Role |
|---|---|---|
| Assistant | omp in `~/agents/assistant`, a tile on the agents board | `~/tstack/roles/assistant.md` |
| Coordinator | omp in `~/agents/projects/<name>`, a tile on the agents board beside its project's card | `~/tstack/roles/coordinator.md` |
| Lane | a detached `omp -p` in `~/worktrees/<project>/<id>` | `~/tstack/roles/lane.md` plus its brief |
| Subagent | omp `task` inside any of the above | its brief |

- A seat wakes when a background bash job it owns ends (`async: true`, `timeout: 0`). The jobs are `lane wait`, `watch-pr --until change`, `easl ask … --wait` and a `sleep` heartbeat. After a restart, `skill://poteto-mode/playbooks/session-pickup.md` re-arms them from state.
- Work that writes code or must outlive you is a lane: `lane start <name> --repo <owner/repo> --brief <file>`. Short or read-only parallel work is a `task` subagent with an agent type from `omp.md`.
- A seat's state lives on disk in its `state/`: lanes in `state/lanes/<id>/`, orch's store in `state/orch/`. Your conversation is a cache; read state again before a consequential action.
- Subagents and lanes see none of your conversation. Brief them completely: outcome, scope, context, acceptance and the exact proof.

## Tools

- `lane` starts, resumes, lists, shows, waits on, stops, merges and cleans lanes. `lane show <id>` prints a lane's exit, cost, PR and final answer.
- `watch-pr <pr>` reports a PR's facts: head, checks, reviews, threads, mergeability, and after merge the runs and deploys that carry it. It decides nothing.
- `tstack setup` links this host's skills, agent types and bins. `tstack update` pulls the engine and instance and reruns setup. `tstack board` arranges the agents board in horizontal bands. The operator band holds the assistant, status strip, to-do, open questions and recently merged and deployed work. Each project band holds its coordinator, card and keyed `show/<project>/<slug>` tiles. It then prints the choices, notes and Mark done clicks the operator left there.
- `pstack-bump <sha>` brings an upstream pstack commit in on a branch, for a system PR.
