# Browser supplements

Use a supplement only for a capability the native browser tool lacks, or where the harness has none. Follow the host's configured tool restrictions; a fallback is not a way around them. The machine notes in the instance say which hosts have `agent-browser` installed.

## agent-browser

A headless browser CLI any agent runs from a shell. Agents share the installed CLI, never a browser: each task drives its own headless Chrome, launched for that task's session and closed when the task ends, so pages stay off the operator's screen, input, clipboard and Chrome profile.

Before the first command:

1. Run `agent-browser skills get core` (add `--full` for every command) for the usage guide matching the installed version. Where it differs from this file, this file wins: its `session id --scope worktree` names collide when agents share a worktree.
2. Run `env | cut -d= -f1 | grep '^AGENT_BROWSER_'`. It must print nothing. Any name it prints overrides the clean config below (auto-connect, a profile, a cloud provider): stop and report it.
3. Name a session for this task alone: `ab-<agent>-<task>-<unix time>`. In a shell that keeps no variables between calls, repeat the literal name.

Every call carries `--config ~/tstack/skills/browser/agent-browser.json --session <name> --headed false`. The config replaces any user or project `agent-browser.json`. Repeat every other launch flag the first call used (`--allowed-domains`, …) as well: a call whose launch flags differ lands in a fresh blank page.

```sh
C=$HOME/tstack/skills/browser/agent-browser.json S=ab-docs-check-1791402000
agent-browser --config "$C" --session "$S" --headed false open https://example.com
agent-browser --config "$C" --session "$S" --headed false snapshot -i -c
agent-browser --config "$C" --session "$S" --headed false click @e1
agent-browser --config "$C" --session "$S" --headed false close
```

- Read the page from the compact accessibility snapshot (`snapshot -i -c`), `read` or `get text`. Screenshot only after a snapshot, and only when layout, canvas or imagery is the question. Write captures to a `mktemp -d` scratch dir.
- `close` ends every task, failed ones included. A browser left open exits after an idle hour.
- Detailed recording: `trace`, `network har`, `console` and `errors` record CDP traces, network and page logs for the session. Record on demand and store captures with the task, outside shared configuration.

Drive only the fresh browser your session launched. These flags and commands reach the operator's live browser, logins, screen or paid services, so they stay unused:

- `--auto-connect`, `--cdp`, `connect`, `--profile`, `profiles`, `--state` taken from their browser.
- `--args` and `--executable-path`: launch overrides that can name a profile, a debugging port or their Chrome.
- `--headed true`, `inspect`, `-p ios`, `dashboard`: windows and servers on their machine.
- `clipboard`: the system clipboard they type with.
- `chat` and cloud providers (`-p browserbase`, `browseruse`, …): paid inference and hosting.
- `install` and `upgrade`: the machine's setup pins the version.

A site that needs the operator's login gets a separate test account or a login they approve; ask through your seat.

## Other routes

- The operator wants to watch the page: an easl browser tile.
- Deterministic tests and captures stay on the project's own test libraries (Playwright and the like).
- A headed browser or desktop GUI: the machine's GUI tooling, described in the instance's machine notes.
