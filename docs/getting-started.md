# Create a local instance

Start with the sanitized assistant and example project in [`examples/instance`](../examples/instance). The example `bin/text` prints locally and never sends messages. Its waiting inbox sleeps for 30 minutes before reporting that no inbox is configured.

## Install prerequisites

Use Python 3.10 or later, git, and an authenticated [GitHub CLI](https://cli.github.com/). Install [easl](https://github.com/twaldin/easl) using its installation instructions. Configure a model provider in omp before starting a seat.

The preparation environment reports omp 18.13.2. The [upstream omp installation instructions](https://github.com/can1357/oh-my-pi#install) document this pinned binary install for Linux and macOS:

```sh
	curl -fsSL https://omp.sh/install | sh -s -- --binary --ref v18.13.2
	omp --version
	gh auth login
	omp login
```

## Copy the template and run setup

Choose unused paths for the engine and instance. Keep the exported variables in the shell where you run seats.

```sh
	git clone https://github.com/twaldin/tstack.git "$HOME/tstack"
	export TSTACK_HOME="$HOME/tstack"
	export TSTACK_INSTANCE="$HOME/tstack-instance"
	export TSTACK_MACHINE=local
	export PATH="$HOME/.local/bin:$PATH"
	cp -R "$TSTACK_HOME/examples/instance" "$TSTACK_INSTANCE"
	python3 "$TSTACK_HOME/bin/tstack" setup --machine local --instance "$TSTACK_INSTANCE"
	cd "$TSTACK_INSTANCE/assistant"
	omp
```

Setup links commands into `~/.local/bin`, skills and agents into `~/.omp/agent`, and the seat-waits extension into each seat. It generates `~/.omp/agent/AGENTS.md` from engine, instance, and machine rules. The template's seat instructions do not import the Tim-specific shared assistant and coordinator roles.

Setup also clones external skills into `~/worktrees/frontend-studio/main`, `~/worktrees/mattpocock-skills/main`, and `~/worktrees/ponytail/main`. These Tim-specific paths and the upstream revisions are not configurable or pinned. Review the [managed skill sources](reference.md) before use. This repository's CI runs the unit tests and a temporary-HOME setup check on GitHub-hosted Ubuntu with Python 3.12 and Bun 1.4.3. Bun runs the seat-waits runtime tests. Forks do not need a self-hosted runner.

On Linux, setup writes systemd user units under `~/.config/systemd/user` and reloads the user manager when a user bus exists. It does not enable the update timer. Leave the timer disabled until you configure an instance Git remote and review `tstack update`. On macOS, setup skips these units. Lanes use the macOS `nohup` backend instead of systemd.

## Configure your seats

Edit `global.md`, `machines/local/global.md`, and each seat's `AGENTS.md`. Replace the example project instructions with your repository and work rules. Run setup again after adding a seat with a `state` directory. Start a project seat with `omp` from `projects/example`.

The state starts empty. Markdown files contain one-line schema comments. JSON files use a `_comment` property because JSON does not accept comments. `tasks.tsv` contains only its required tab-separated header because its consumers do not accept comment lines. `state/board.json` is absent until you create an easl board and record its real ID. Lane commands create `state/lanes` as needed.

Do not run `tstack commit` until you deliberately initialize the instance as a separate Git repository with an `origin` remote and a `main` branch. The template has no messaging credentials or remote hosts. Keep the local `bin/text` until you intentionally configure a messaging integration.
