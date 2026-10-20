### Orchestrate

Run a program through lanes. One standing coordinator authors briefs, collects results, and keeps the lowest unmerged PR green. It never writes code. Use a single lane for work that needs no program.

#### Roles and placement

The coordinator owns scope, dependencies, merge order, state, and the operator report. Lanes own code, runtime proof, review, and their PRs. Give each lane its own worktree and one writer per branch. Delegate conflicts and code fixes to lanes too.

#### Seat state

Use the seat's existing `state/`, not a second store. The coordinator writes program state; lane commands write lane records and run output.

- `orders.md` holds standing rules. Read it before dispatch and carry the relevant rules into every brief and resume.
- `tasks.tsv` tracks units, dependencies, lane ids, state, and PRs. Reconcile it from lane reports and forge facts.
- `lanes/<id>/` holds the brief, lane record, and final answer. Read these through `lane show <id>`.
- `efforts/<name>.md` holds the done predicate, ordered PRs and heads, verification receipts, decisions, blocked questions, and follow-ups.
- `status.json` holds the current operator summary, per the coordinator role.

Record facts once and link to receipts. Do not copy lane run logs into program state.

#### The brief

Write a self-contained brief with the goal, repo and base branch, scope and forbidden paths, dependencies, acceptance criteria, exact proof, and report requirements. Include standing rules and conditions for stopping early. Relay upstream findings into dependent briefs; lanes cannot see your conversation.

Include this line, naming a file stem in poteto-mode's `playbooks/`, or `none`:

```
PLAYBOOK: feature
```

`lane start` reports a nonexistent name and lists valid names, but still starts. On that report, run `lane stop <id>` and start a corrected brief. A small unit needs a small brief, not a second planning document.

#### Steps

1. **Frame.** Write the countable done predicate, units, dependencies, and merge order in the effort file. Read standing orders and reconcile existing lanes and PRs before starting more work.
2. **Prepare.** Write briefs and task rows. Use `lane start <name> --repo <repo> --brief <file>` with an explicit base for dependent work. Record each returned lane id.
3. **Pilot.** Take one unit through proof, review, and merge. Fix the brief and verification recipe from its result before repeating the unit across the program.
4. **Scale.** Keep a rolling window of independent lanes. Refill as lanes finish, not after the slowest member of a batch. Start dependent work only with the required upstream context and base.
5. **Drain.** `lane wait <ids>` returns one ended lane. Remove that id from the wait list, read it with `lane show <id>`, and repeat for the remaining lanes. Apply the queue discipline below before dispatching more work.
6. **Land.** Keep landing verified units while other lanes work. Use the stack rules below. Run `lane merge <id>` when the project's merge rule holds, then `lane clean <id>` for completed worktrees.
7. **Close.** Reconcile every lane to landed, abandoned, or blocked. Confirm the done predicate on the real artifact and record remaining work and questions. Leave the effort file and lane reports as the trail.

#### Queue and drain

Finish the brief or state update in progress before processing a completion. A completion is a queue event, not an interrupt.

At each drain, read the completed lane reports, update task rows and the effort file, and classify each unit as landed, needs review, failed, or blocked. Account for every started lane. Record missing results rather than silently redoing their work. Read reports before authoring dependent briefs.

Use `lane resume <id> "<consolidated instructions>"` when a fix needs that lane's existing session or worktree. Give a fresh lane a consolidated brief for replacement work. A resume starts work; never use it to check liveness.

#### Stack safety

Keep PRs in base-branch order in the effort file. Confirm current heads and bases with `gh pr view <pr> --json state,headRefOid,headRefName,baseRefName`. Use `watch-pr <pr> --once` for checks, reviews, threads, and mergeability. Use `watch-pr <pr> --until change` when waiting on the lowest unmerged PR.

Work that PR before upper-stack fixes. Follow `playbooks/babysit.md` for review and CI. Assign one lane to stack mutations; never let lanes rebase or retarget the same chain concurrently. After a merge, reconcile dependent bases and heads before accepting their proof. Delegate any required code or conflict resolution.

Stacked lanes' briefs forbid merging. The coordinator owns their merges, bottom-up, after the PR targets the default branch and its current head meets the global merge rule. Independent lanes may merge their own PRs under that rule.

#### Verification

Require proof on the real artifact and review from another model family for the exact PR head, per the project's merge rule. Record the PR, head SHA, reviewer model, verdict, and evidence link in the effort file. A new head invalidates the old approval. CI green alone is not proof of behavior.

A failed verification gets a fix. An unavailable runtime stays blocked, not passed. Land only the verified prefix of a stack.

#### Liveness and failure

Read `lane show` and `lane logs` for run facts. After a coordinator restart, read orders, tasks, the effort file, and lane reports; query current PR facts before dispatching or merging.

For a failed run, record its exit and evidence. Narrow scope after a resource failure. Retry a transient infrastructure failure once; repeated failure needs a changed plan. Stop dispatching dependent work when upstream output or acceptance is wrong.

#### Escalation

Record human-only decisions and contradictions of standing orders in the effort file. Route other work around them. Send them through the seat's established operator channel. Routine review triage, CI fixes, and frontier progress do not need a human decision.

Keep unrelated discoveries in follow-ups. Do not widen every lane's scope.

**Reply:** Report the done predicate and task counts, landed PR links, the lowest unmerged PR and head, proof and review verdicts, abandoned work, human-only questions, and the effort file path.
