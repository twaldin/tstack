# PR

**You own one PR from open to merged.** This is the default pipeline. A project's `pipeline.md`, appended after this file, adds stages or overrides gates.

Hard gates: proof the change works (the repo's definition of done, or the check the brief names) on the exact head, and CI green where the repo has CI. Soft: the local cross-family review and bot reviews. Merge when the hard gates pass, through `lane merge`.

## 1. Commit

- Commit liberally, then rebase into small ordered commits before the PR opens. Each commit is landable and tells one step of the story; a failing repro lands before its fix.
- Rebase onto the current `origin/<base>` before opening.

## 2. Review locally (soft)

Run `~/tstack/playbooks/interrogate.md` over `git diff origin/<base>...HEAD` with a reviewer from another model family. Fix what is real, answer what you reject in the PR body's Tradeoffs, skip nits.

## 3. Open

- Push the branch (`git push -u origin HEAD`) and open the PR ready, never draft: `gh pr create --base <base> --title "<type>(<scope>): <subject>" --body-file <file>`.
- Title: Conventional Commits. Type is `feat`, `fix`, `docs`, `refactor`, `test`, `chore` or `perf`; scope is the changed area; the subject is short, imperative and has no trailing period.
- The body is a briefing: a reviewer with the diff learns why it exists, what it leaves out, what it could break and how it was proved, in under a minute. The squash commit body is the PR body, so keep it under about 40 lines. Sections under `##` headings, in order, dropping any with nothing to say:
  - `## Why`: the problem and the approach in one to three sentences.
  - `## What changed`: one to three bullets; name both sides of a rename.
  - `## Scope`: what the PR covers and what it deliberately leaves out.
  - `## Tradeoffs`: only rejected alternatives a reviewer would ask about.
  - `## Blast radius`: who or what the change touches and why that is safe or risky.
  - `## Verification`: one to three bullets, each a real run and its outcome; a performance change gives one number as `before → after`.
- Prove the change on the pushed head, then report and end the turn: `lane report --status waiting --waiting-on ci --summary "PR open" --pr <url> --head <sha> --proof "<check>=pass"`.

## 4. Drive to green

Each resume brings an event. Read the PR's current state first (`gh pr view <n> --json state,mergeable,mergeStateStatus,headRefOid,statusCheckRollup,reviewDecision` and `gh pr checks <n>`), then work in this order, batching every known fix into one push:

1. **Conflicts.** Rebase your branch onto `origin/<base>` and publish with `git push --force-with-lease`. Your branch is yours to rewrite; no other branch is.
2. **Review threads and bot comments.** Verify each claim against the code. Fix real findings with a red-first proof; dismiss noise with a concrete reason. Push before replying so the reply cites the commit. Reply with `gh api --method POST repos/<owner>/<repo>/pulls/<n>/comments/<comment-id>/replies --input <payload.json>`, the body in the JSON file; never put comment text into a shell command. A bot finding that arrives after the hard gates pass is fixed only if real, and the merge never waits for a bot. A review-limit notice counts as approval.
3. **CI.** Classify before any rerun:
   - Flake or infrastructure gets one full rerun (`gh run rerun <run-id>`). An identical second failure was never a flake: read the logs.
   - A failure in code the diff never touches usually means a stale base: check with `git merge-base --is-ancestor origin/<base> HEAD`, then rebase.
   - A check that also fails on the base branch is not yours: report `stuck`, naming the check and the base commit.
   - Only a failure in the diff's own code gets a commit.

Every push makes a new head and voids earlier proof. Prove again on the new head, then report `waiting` with the new `--head` and `--proof`. Two failed fix waves on one check: report `stuck` with what you tried.

## 5. Merge

When the head has proof and every check on it passed, run `lane merge`. It merges with squash, or arms auto-merge where the repo requires checks, and refuses with the reason when a gate is missing. A brief that names a dependency PR waits for that PR to merge first (`--waiting-on dependency`). A pipeline with the line `merge: operator` reserves the merge for the operator and makes `lane merge` refuse; report `waiting --waiting-on operator` once the hard gates pass.

After the merge, `lane report --status done --summary "<what landed>" --pr <url> --head <sha>`. Main CI on the merge commit, run-after-merge steps and deploys belong to your seat.

**Report:** the PR link, what landed, the proof per check and head, what you fixed and what you dismissed with reasons, follow-ups.
