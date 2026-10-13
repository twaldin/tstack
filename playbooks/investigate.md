# Investigate

**You own the answer.** An investigation is read-only. It produces a cited explanation or a recommendation, not a code change.

1. **Map the question.** Read the code, history and docs that own the answer. Fan broad reading out to `scout` subagents, one per area, each returning findings with `path:line`. For "why was it built this way", read the history: `git log -L <start>,<end>:<file>`, `git blame`, the PRs and issues behind the change. For external facts, use the `research` skill.
2. **Check before you claim.** Run the code, query the system or read the source that decides the behavior. A claim you could check by running something gets checked.
3. **Write the answer** in this shape: overview, key concepts, how it works, where things live (`path:line`), gotchas. A decision between alternatives gets a recommendation with a tradeoffs table instead.
4. **Stop at the code change.** When the answer calls for a change, end with it as a proposed fix-bug or build-feature task; the investigation itself changes nothing.

Every claim carries its evidence (`path:line`, command output, a link) or its label: inferred or guess.

**Report:** the answer. For "are we sure?" questions, your real judgment with reasons; push back when the premise is wrong.
