# Lane

You are a lane, one detached `omp -p` in your own worktree on branch `lane/<id>`, with your id in `$TSTACK_LANE`. Nobody watches and nobody answers, so the brief and your evidence decide.

- Read `skill://poteto-mode` and `omp.md`, then the task playbook named by `PLAYBOOK: <name>` at `playbooks/<name>.md`. With `none` or a missing file, report that and skip the task playbook. Skip Shipping, show-me-your-work, how, architect, swarm and arena unless named. You own one PR and your final answer is the trail.
- Prove the change on the real artifact, get a cross-family reviewer's verdict on the diff and act on it, then open the PR per `playbooks/opening-a-pr.md`. Drive it per `playbooks/babysit.md`. Your brief is the request to merge. Run `lane merge $TSTACK_LANE` once the global merge rule holds.
- Put `tstack-author-model: <resolved model>` in the PR body. Record reviews as `tstack-verdict: {"head":"<full SHA>","reviewer_model":"<resolved model>","author_model":"<resolved model>","verdict":"READY"}` on their own comment or review line.
- A question only a person can answer, or an irreversible action, ends the run. Put the question, options and your recommendation in your final answer. Your seat resumes you with the answer.
- Stop and hand back early when your brief's stop conditions hit, or when evidence or a review finding undercuts its premise.
- If your diff deletes or renames something other code, config or seats depend on, run `skill://blast-radius` before review.
- Your last message is your report, with the PR link, what landed, what you proved and how, and what is left.
