# Changing tstack

This repo is the engine, the same on every host. Every change reaches it through a PR that the operator decides.

- Start from an observed failure: a session, a lane's final answer, a check. Name it in the PR.
- Delete before adding. A change that adds a line, file or flag says what it replaces.
- A repeated mistake changes what a tool does or shows. Never answer it with a check, a refusal or a required format.
- A CLI wraps gh, git, easl, omp or systemd only to add knowledge, and passes their errors through.
- Edit `pstack/` only where Cursor is baked in, and keep `git diff pstack-upstream main -- pstack/` small. Bring upstream in with `pstack-bump`.
- The CLIs use the Python standard library only (`pstack-bump` is bash), so they run on every host.
- Tests call a CLI as its users do, with gh, omp, easl and systemd faked by small executables on PATH, and assert literal output. Run `python3 -m unittest discover -s tests`.
- Agent-facing prose (`global.md`, `roles/`, `agents/`) follows the `unslop` skill. A role states facts and judgment; the CLIs' `--help` carries their usage.
