# Build a feature

**You own the design and its delivery: map, design, build, prove.**

1. **Map the subsystem.** Read the code the feature touches; fan broad reading out to `scout` subagents, each returning findings with `path:line`.
2. **Name the data shape** and its organizing structure: a state machine over scattered booleans, a table or registry over branching, a typed model over repeated shape assumptions. Code that crosses a function boundary gets its interface designed first per the `codebase-design` skill; a contested shape gets two or three competing sketches and a pick with reasons.
3. **Write the throughput checkpoint**, four lines, each answered or marked `n/a: <reason>`:
   - Blocking first steps: what must land before any fan-out.
   - Independent workstreams: disjoint files or layers that can run in parallel.
   - Shared mutable state: split the target first; serialize only for a real invariant.
   - Smallest safe decomposition: if one worker is best, say why.
4. **Build.** Delegate parallel edits on disjoint files to subagents with a specific scope: file paths, the named data shape, success criteria. One writer per file. Review each diff yourself.
5. **Verify on the matching surface**: the real CLI, UI or API the user touches. An inconclusive result or a check on another surface is not a pass; flag it.
6. **Shape the history** into small ordered commits, each built and verified before the next.
7. **Ship** per `~/tstack/playbooks/pr.md`. A contested design gets a full `~/tstack/playbooks/interrogate.md` pass before the PR opens.

**Report:** what you built, what you chose and why, the throughput checkpoint, open decisions. Tables for design alternatives.
