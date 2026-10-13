# Interrogate

**You own the verdict.** Reviewers from a model family other than the author's challenge a diff or design; you filter their findings into a verdict. The signal comes from model diversity, not assigned personas. Nothing is applied automatically.

## 1. Scope and intent

- Scope: the files or diff named, else `git diff origin/<base>...HEAD`, plus the context files a reviewer needs to follow it.
- Intent: one paragraph from the brief, the commits, the PR body and the code. Reviewers judge whether the code achieves it, never the intent itself.

## 2. Pick reviewers by family

The author's family is the model that wrote the code: yours, and any subagent's that edited it.

| Author | Reviewer agent types |
|---|---|
| Anthropic (`@opus`) | `review`, and `grok` for a second voice |
| OpenAI (`@sol`, `@review`) | `opus`, and `grok` for a second voice |
| Mixed Anthropic and OpenAI | `grok` |

Spawn all reviewers in one `task` call with the same prompt (below), read-only. When the results return, check the model each one actually ran on in the task output: a reviewer that fell back to the author's family does not count, so rerun that review on another type. No reviewer outside the author's family means the review is incomplete; say so.

## 3. Reviewer prompt

> You are an adversarial code reviewer. Find real problems in the change below: bugs, design flaws, security issues, maintainability risks. Assume the intent is right and challenge the execution.
>
> Intent: {INTENT}. Change: {DIFF_OR_FILES}.
>
> Review through the lenses that apply; a small fix needs no essay on architecture.
> - **Correctness.** Edge cases, error handling, races and shared mutable state, idempotency after a crash halfway. Trace the call chain that makes a value bad instead of flagging "could be null".
> - **Root causes.** Guards that mask a broken invariant, retries hiding a broken contract, casts silencing a modeling error, a "don't do X" convention where a type, lint or runtime check would make X impossible. Read callers and callees beyond the diff.
> - **Structure.** Validation at system boundaries, data structures that fit the access pattern, a change that reads as bolted on, an old path kept alive beside the new one.
> - **Verification.** Tests that check behavior rather than implementation, a test for the bug, checks of the real artifact instead of a proxy or a self-report.
> - **Complexity.** Abstractions with one caller, configuration for cases that don't exist, dead code. Look for a restructuring that deletes whole branches or layers. Flag a file pushed past 1,000 lines and ad-hoc conditionals tangled into unrelated flows.
> - **Security.** Input reaching shell, SQL, eval or HTML unsanitized; auth gaps; secrets in code or logs; check-then-use races. Show the input path.
>
> For each finding give severity (`critical`, `warning`, `nit`), location (`path:line` or function), what is wrong, the evidence, and a concrete suggestion if you have one. Restating the code or praising it is not a finding. Zero findings is a valid review.

## 4. Synthesize and judge

Merge duplicate findings and note which reviewers raised each. Findings raised independently by two reviewers are the strongest signal; a lone finding still gets read. You are the lead reviewer, a pragmatic senior engineer with context the reviewers lack, not a neutral aggregator:

- A reviewer whose findings are all nits is telling you the code is fine.
- "What if this is null" counts only if a caller can pass null; trace it.
- An abstraction suggested for code that changes one way is premature.
- "I would have done it differently" without a concrete problem is dismissed.
- Correctness and security findings get extra scrutiny before you dismiss them, even from one reviewer.

Put each finding in one bucket, with the reviewers that raised it and a one-line rationale:

- **Act on**: real issues in correctness, security or maintainability that would block a PR. More than five means you are not filtering hard enough.
- **Consider**: legitimate, but the cost of addressing it now may not pay.
- **Noted**: valid, not actionable at this stage.
- **Dismissed**: wrong, nitpicky or missing context, with the reason. This list lets the reader override your judgment.

**Report:** the intent, each reviewer with its served model and finding count, the four buckets, and where the reviewers agreed and diverged. In a lane, fix the Act-on items before the PR opens and carry real Consider items into the PR body's Tradeoffs.
