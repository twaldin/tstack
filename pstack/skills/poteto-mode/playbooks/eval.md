### Eval

Test role, global-rule and poteto-mode edits on real briefs before review.

1. Choose a handful of recent briefs that exercise the changed decisions. Write 3 to 6 observable criteria before running them. Keep the criteria outside the workers' directories.
2. Put the old and new engine text in separate scratch directories made with `mktemp -d`. Give each run a fresh scratch HOME, project and instance. Copy the applicable instance and machine rules, not live seat state. Keep the repo's `AGENTS.md` unchanged. Use neutral project names. Hide grading criteria and comparisons from workers. Give both variants the same project inputs and brief.
3. Link each HOME to its engine variant with `HOME=<home> TSTACK_HOME=<engine> TSTACK_INSTANCE=<instance> <engine>/bin/tstack setup`. Check the generated rules and skill links. Reuse the existing broker config and token through symlinks, not credential copies. Pass the role variant through `--append-system-prompt`, never through the repo's `AGENTS.md`. Contain external effects with disposable remotes and CLI fakes, or scope the brief to local artifact authoring.
4. Run each brief with `HOME=<home> PI_CODING_AGENT_DIR=<home>/.omp/agent omp -p --append-system-prompt <engine>/roles/<role>.md --model <model> --thinking <level> --cwd <project> --session-dir <logs> --max-time <duration> @<brief>`. Keep the model, thinking level, tools and timebox fixed across variants. Save stdout, stderr, session logs and produced artifacts. An auth or setup failure is not a graded run.
5. Give one reviewer from another model family the criteria, artifacts and session logs for both variants, labeled only with neutral ids. Grade each criterion from observed actions and outputs. Verify tool reads and their order in the session logs, not the worker's claims. Do not search unrelated sessions.
6. Read every final answer and the cited log evidence yourself. Compare both grades on the same scale. Report regressions, inconclusive cases and disagreements with the reviewer. Recommend whether to keep the edit. Put the graded result and evidence in the PR, then remove exactly the scratch paths you created.

Keep briefs organic. Do not mention an eval, ask workers to recite rules or tell them another run exists. A timebox exit is evidence, not a pass. For visual work, grade the actual screenshots or browser interaction too.

**Reply:** changed text, briefs, criteria, model and timebox, grades for both variants, log evidence, regressions and recommendation.
