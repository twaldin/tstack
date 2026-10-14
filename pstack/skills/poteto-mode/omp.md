# pstack in omp

pstack is written for Cursor. Here it runs in omp on easl boards, and lanes take the place of Cursor's cloud agents. Read every pstack file as written and translate the names below. Where a seat role, `global.md`, a project's `AGENTS.md` or standing orders differ from pstack, they win.

| pstack says | Here |
|---|---|
| `Task` with `subagent_type` and `model` | omp `task` with an `agent` type. `poteto-agent` for delegates, `scout` for read-only exploration (how explorers, why investigators), `opus` for judgment, explainers, synthesis and the hardest code, `grok` for fast code, `review` for OpenAI-family review. A task item has no model field. |
| `readonly: true`, `run_in_background: true` | Say read-only in the brief. omp tasks already run in the background and report back on their own. |
| Cursor cloud agent, `environment: "cloud"`, cloud VM, `cloud_base_branch` | `lane start` for work that writes code or must outlive you (a worktree plus a detached `omp -p`). `task` with `isolated: true` for short parallel work inside your session. Name the base branch in the brief. |
| `pstack-models.mdc`, `/setup-pstack`, role lines | omp model roles and the agent types above. Review and design panels (interrogate, arena, architect) use `opus` and `grok`, plus `review` when the author is Opus. |
| An agent arming `/loop`, a watcher subagent | One owned background bash job (`async: true`, `timeout: 0`) whose exit starts your next turn. `lane wait <id>...` for lanes, `watch-pr <pr> --until change` for a PR, `sleep <seconds>` for a heartbeat. omp's own `/loop` is a command Tim types. |
| Completion notifications | `lane wait` exits with the lane's id, exit code and final answer. Background tasks report on their own. |
| `AskQuestion` | `easl ask "<question>" --option <id>=<label> --recommend <id> --wait` as a background bash job. Tim answers in the easl tile. |
| `control-ui`, `control-cli` | The real CLI for CLIs and TUIs. The browser tool or an easl browser tile for web UI. |
| `/deslop` | Run the project's checks and remove code the diff does not need. |
| `create-skill` | A SKILL.md with `name` and `description` frontmatter, written per `playbooks/authoring-a-skill.md`. |
| `agent-transcripts/`, `~/.cursor/projects` | `history://<id>` for your subagents. `lane show <id>` for a lane's omp session. |
| `git show origin/main:pstack/<path>` | Read the installed file, `skill://<name>/<path>`. |
| Bugbot, the agentic security review | Your team's review bot. |
| `scripts/watch-pr/watch-pr` | tstack's `watch-pr <pr>`. It prints facts (head, checks, reviews, threads, mergeability, and after merge the runs and deploys that carry it). `--once` reads once; `--until change` waits for the next change. |
| The agent store, `orchestrate/<project-slug>/` | The seat's `state/orch`. Run orch from this skill's directory as `ORCH_STORE=<seat>/state/orch bun scripts/orch/orch.ts <command>`. |
| `orch frontier` | Skip it; it needs Graphite. Read the frontier with `gh pr list`, `gh pr view` and `watch-pr`. |
| `gates.md`, `orch gate` | Ask Tim with `easl ask`, then record the ask's id with `orch gate park <ask-id> --question … --options … --default …` so `orch status` shows it. Tim answers in the easl tile; record it with `orch gate resolve <ask-id> --answer …`. |
| Graphite `gt`, Origin | Not installed. Use `gh`. |
| A Cursor restart, the Cursor dashboard | An easl seat restart (`easl agent.restart --mode resume`), then `playbooks/session-pickup.md`. `lane list` shows lanes. |
| Not shipped here | `playbooks/multi-phase-plan.md` (write an effort file and briefs instead), `playbooks/worktree-cleanup.md` (`lane clean <id>`), and the skills setup-pstack, poteto-help, automate-me, make-bot-ui, recall, reflect, teach, correct, create-verification-skill and maintain-verification-skill. `../../DROPPED` lists every file left out. |
