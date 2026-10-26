# Changing tstack

This repo is the engine, the same on every host. Every change reaches it through a PR, merged per the global PR rule.

- Start from an observed failure: a session, a lane's final answer, a check. Name it in the PR.
- Delete before adding. A change that adds a line, file or flag says what it replaces.
- **Tools in code, judgment in prose.** A repeated operation with intricacies gets one CLI that does it fast and correctly. When and whether to use it stays in playbooks and roles.
- Keep workflow judgment in prose. Enforce a rule in a tool only after the prose version failed twice, and keep the check small and named after the failure.
- **Wrap native tools only to add knowledge** (gh, git, easl, omp, systemd).
- **pstack is the base.** Use its files unchanged where they fit, edit where Cursor is baked in, and drop what no seat uses. Keep `git diff origin/pstack-upstream -- pstack/` small. Bring upstream in with `pstack-bump`.
- The CLIs use the Python standard library only (`pstack-bump` is bash), so they run on every host.
- Tests call a CLI as its users do, with gh, omp, easl and systemd faked by small executables on PATH, and assert literal output. Run `python3 -m unittest discover -s tests`.
- Agent-facing prose (`global.md`, `roles/`, `agents/`) follows the `unslop` skill. A role states facts and judgment; the CLIs' `--help` carries their usage.
- PRs that change roles, global rules or poteto-mode run `playbooks/eval.md` from `skill://poteto-mode` on old and new text before review.
