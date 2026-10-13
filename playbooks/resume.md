# Resume

**You own the resume point. Read the prior trail; don't redo it.** For taking over in-flight work: a lane, a seat's state after a crash, a pushed branch or an open PR.

1. **Claim it.** Taking over a lane's session by hand starts with `lane hold <id>`, so the seat's wait cannot resume the same session while you work in it. Release it with `lane release <id>` when you hand it back.
2. **Locate the trail.** For a lane: `lane show <id>` gives its brief, report, session path, worktree, branch and PR; `omp --resume <session>` reopens the session. For a seat: its `state/` files and git log. For a branch or PR: `gh pr view`, the commits and the review threads. Read the last messages first, then scan back for the decision points. Parse a long transcript in a subagent and keep the reduced timeline.
3. **Reconstruct the operational state**: the worktree and branch, what landed (`git log` and `git diff` against the base), the PR's checks and threads, open items and decisions made. The prior trail is authoritative input; resist re-deriving it.
4. **Diff done against pending.** Name the resume point. Completed work stays completed; don't rerun the prior repro or redo landed steps.
5. **Route the rest** to the matching playbook and pick the verdict: continue the execution, ship a finished result, ratify or override a prior conclusion, or write a postmortem of a failed run. That playbook owns the remaining steps.
6. **Verify inherited claims** against the original goal on the real artifact. A passing self-report from the prior run is not proof.

**Report:** where the prior run stopped, what you inherited and what you redid (ideally nothing), the resume point, and the outcome.
