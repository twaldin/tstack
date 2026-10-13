# Changing tstack

This repo is the engine: identical on every host from a fresh clone, with nothing personal in it. Every change reaches it through a PR that the operator decides.

## Before you edit

- Start from an observed failure: a session, lane report or check that went wrong. The PR's Why names it.
- Delete before adding. A change that adds a rule, file, flag or skill says what it replaces or removes. A mechanical mistake becomes a check in `bin/` or `lib/` with a test, not another sentence of prose.
- Keep each meaning in one file. `global.md` holds what every agent needs, a role file what that seat needs, a playbook what its kind of task needs.
- Agent-facing text (`global.md`, `roles/`, `playbooks/`, `skills/`, `agents/`) follows the `writing-for-agents` and `unslop` skills: leading words, positive instructions, nothing a file or `--help` already says.
- `roles/restart.md` is sent verbatim as a prompt and `roles/lane.md` opens every lane's system prompt; read how `bin/` uses a file before changing its shape.
- A skill keeps its place by use: about 30 sessions in 14 days where agents reach for it unprompted, or the operator asking for it by name. A rare safety or recovery skill stays when the operator names it as an exception.

## Personal data

The engine names no person, employer, host, home path, phone number or email. Write "the operator", `~/agents`, `<machine>` and `<owner>/<repo>`; facts about one operator belong in their instance. `tstack check` fails on home paths, emails, phone numbers, the running user's login and the instance's machine names.

## Forked files

`playbooks/`, `roles/coordinator.md`, `roles/lane.md`, `global.md` and `skills/unslop` derive from pstack (MIT, notice in `playbooks/LICENSE`). `skills/research`, `skills/codebase-design` and `skills/writing-for-agents` derive from Matt Pocock's skills (MIT, a `LICENSE` in each). `upstream.lock` pins every source; porting an upstream change is a PR that edits the file and moves its commit there.

## Checks

- Run `tstack check` before opening the PR: links, generated files, seat state, size caps and personal data.
- Run `tstack check --smoke` when the change touches anything omp loads (`global.md`, `roles/`, `skills/`, `agents/`, `extensions/`) or a CLI that launches omp.
- The CLIs are Python 3.9 standard library only, so they run on every host. Run the behavior tests in `tests/` (python unittest) when you change `bin/` or `lib/`.

## PRs

- Any agent may open a PR here. The assistant keeps it green and brings it to the operator with its reason, what it deletes, the line change and a recommendation. The operator decides each one, and it merges only on their word.
- One PR per change, with a body per `playbooks/pr.md`.

## Size caps

`tstack check` fails a file over its cap. A line is one paragraph or one list item; merging lines to fit under a cap defeats it. Raising a cap is its own PR with the reason.

- `AGENTS.md`: 60 lines
- `README.md`: 65 lines
- `global.md`: 110 lines
- `roles/assistant.md`: 115 lines
- `roles/coordinator.md`: 140 lines
- `roles/lane.md`: 50 lines
- `roles/restart.md`: 12 lines
- `playbooks/build-feature.md`: 22 lines
- `playbooks/fix-bug.md`: 20 lines
- `playbooks/interrogate.md`: 65 lines
- `playbooks/investigate.md`: 16 lines
- `playbooks/pr.md`: 60 lines
- `playbooks/resume.md`: 16 lines
- `skills/*/SKILL.md`: 130 lines
- `agents/*.md`: 30 lines
