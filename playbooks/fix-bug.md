# Fix a bug

**You own this fix: reproduce, find the cause, fix, prove.**

Be scientific. Every shipped line traces to runtime evidence. A change that "might help" is a hypothesis, not a fix, and it doesn't ship. When evidence refutes a hypothesis, revert what it motivated. The smallest change the evidence justifies ships, nothing more.

1. **Reproduce it yourself** on the surface where it was reported: the CLI for a CLI, a browser (the `browser` skill) for a web UI, the API for an API. If it won't reproduce directly, synthesize the trigger, tighten the conditions or add instrumentation until it fires. A bug you cannot reproduce after driving the surface as far as it goes is a `stuck` report with what you tried.
2. **Binary-search the cause.** List candidate hypotheses, seeded from the affected code and its history (`git log -L <start>,<end>:<file>`, `git blame`, the PRs that touched it). Each pass, take the split that cuts the most remaining space, get runtime evidence and eliminate. When program state is unclear, add logging and read it as the code runs. Confirm the surviving mechanism with runtime evidence before planning the fix.
3. **Plan the fix.** A fix that crosses a function boundary gets its design first, per the `codebase-design` skill. Name the data shape the fix changes.
4. **Write the failing repro as a test** when the bug has a cheap local test path, and commit it before the fix so history shows red, then green. Skip the test when it would be expensive, integration-heavy or unclear, and say why.
5. **Fix it.** Delegate parallel edits on disjoint files to subagents with a specific scope; one writer per file.
6. **Verify on the same surface.** The original repro now passes. An inconclusive result or a check on another surface is not a pass; flag it. Unit tests show branch behavior, not that the bug is gone.
7. **Ship** per `~/tstack/playbooks/pr.md`.

**Report:** what was broken, the root cause, the fix, and how you verified it, with the failing and then passing repro output verbatim.
