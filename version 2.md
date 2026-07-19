# Design Document: Python CLI Migration (v2.0.0)

This document outlines the proposed transition of the Pi Coding Agent Container orchestration layer from legacy Bash/Python scripts (`manage.sh` and `pick.py`) to a unified, standalone Python package named `pib` (`Pi Box`). This tool is built using **Typer** and managed by **uv**.

---

## 1. Motivation and Goals

The migration to a native Python CLI resolves several key limitations of the legacy shell scripts:

* **Unified CLI Environment**: Merges the configuration selection logic (`pick.py`) and container orchestration (`manage.sh`) into a single executable codebase (`pib`).
* **Robust Argument Parsing**: Replaces manual positional parameter checks in Bash with standard CLI flags, option type-checking, and interactive help menus.
* **Shell Auto-Completion**: Delivers out-of-the-box native auto-completion support for shells like `bash`, `zsh`, `fish`, and `powershell`.
* **Standardized Environments**: Leverages `uv` to manage dependency isolation, installation tool caching, and package updates without impacting the host's system Python environment.

---

## 2. Target Package Architecture

The package structure will be organized inside a `pib/` package directory:

```text
pi-coding-agent-container/
├── pyproject.toml         # Package metadata, dependency tracking, & entrypoint definition
└── pib/
    ├── __init__.py        # Package initialization marker
    ├── main.py            # Typer application definition & subcommand routing
    └── cli_pick.py        # Model selector interface & configuration builder
```

### pyproject.toml Configuration
The CLI is declared as a pyproject-compliant executable tool:

```toml
[project]
name = "pib"
version = "0.1.0"
description = "Pi Coding Agent container management CLI"
readme = "README.md"
requires-python = ">=3.10"
dependencies = [
    "typer>=0.12.0",
]

[project.scripts]
pib = "pib.main:app"
```

---

## 3. CLI Subcommand Mapping

The CLI will map all legacy functions directly to subcommands:

| Legacy Bash Command | New Python Tool Target | Functionality |
|---------------------|------------------------|---------------|
| `./manage.sh build` | `pib build`            | Compiles the container images using local Dockerfiles. |
| `./manage.sh run [args]` | `pib run [args]` | Launches the Pi Agent container. Defaults to print mode if arguments are provided. |
| `./manage.sh run-proxy` | `pib run-proxy`       | Starts the model-proxy and launches the interactive agent. |
| `./manage.sh up-proxy`  | `pib up-proxy`        | Starts the proxy, portal, and agent services in the background. |
| `./manage.sh up-runner` | `pib up-runner`       | Starts the local model runner container in the background. |
| `./manage.sh pick` & `./manage.sh profile` | `pib configure` (or `pib pick`) | Consolidates model selection and hardware profile configuration into a single interactive wizard. |
| `./manage.sh shell`     | `pib shell`           | Spawns an interactive bash terminal inside the running agent. |
| `./manage.sh profile [name\|clear]` | `pib profile [name\|clear]` | Shorthand to list, switch, or clear the active configuration profile without running the wizard. |
| `./manage.sh scan`      | `pib scan`            | Audits the stack's containers for security vulnerabilities via Trivy. |
| `./manage.sh clean`     | `pib clean`           | Stops and removes all container layers and network bridges. |

---

## 4. Subprocess Execution & Safety

Subprocess calls will be handled natively via Python's standard `subprocess` module:

* **Tty Preservation**: For subcommands that spawn interactive shells (`pib run` and `pib shell`), Python will delegate execution directly to the user's terminal session without intercepting `stdout` or `stderr`.
* **Graceful Interrupts**: Subprocesses will be launched with `check=False` to handle normal user exits, container terminations, and signals (e.g. `Ctrl+C`) gracefully, preventing standard Python traceback dumps.
* **Fail-Safe Checks**: Configuration checkpoints (e.g., verifying that a `PORTAL_PASSWORD` is configured or confirming if a port collision is present) will run synchronously before orchestrating docker resources.

---

## 5. Model & Hardware Profiles Configuration

The `pib` CLI carries forward the composable configuration profile design to easily switch between hardware setups (CPU vs GPU) and different local model configurations.

### Profile Environment Merging
* **Directory Structure**: Profile files continue to be stored in the `./profiles/` directory as `.env` files (e.g. `gemma-12b-gpu.env`).
* **Active Profile**: The active configuration name is tracked in `.env` via the `ACTIVE_PROFILE` variable. When `pib` executes, it will:
  1. Load the base `.env` file.
  2. Parse the `ACTIVE_PROFILE` configuration, loading variables from `./profiles/{ACTIVE_PROFILE}.env` if configured.
* **GPU Passthrough Decoupling**: Rather than embedding the GPU `deploy` resources inside the primary `docker-compose.yml`, these are isolated in a separate, optional `docker-compose.gpu.yml` file.
* **Pythonic Compose Execution**: Instead of relying on host-specific separator logic (colons vs semicolons in `COMPOSE_FILE`), the `pib` subprocess runner will dynamically compose the execution argument list:
  ```python
  cmd = ["docker", "compose"]
  # Automatically merge the GPU override file if USE_GPU=true is loaded
  cmd.extend(["-f", "docker-compose.yml"])
  if use_gpu:
      cmd.extend(["-f", "docker-compose.gpu.yml"])
  cmd.extend(["up", "-d"])
  ```
  This guarantees that compose composition behaves identically on Linux, macOS, and Windows.

### Consolidated Interactive Configuration Wizard
In `pib`, model picking and profile selection are unified into a single interactive configuration wizard. When a user runs `pib configure` (or `pib pick`), it guides them step-by-step:
1. **Model Family**: Prompts the user to select the model architecture family (e.g., `Gemma`, `Qwen`, or `Other`).
2. **Model Size / File**: Scans the `./models` directory for GGUF files matching that family and prompts the user to select the specific size/quantization file they want to load (or input a custom endpoint for remote API models).
3. **Execution Profile**: Based on the chosen model family and size, it lists matching hardware profile templates (e.g., `GPU` vs `CPU`, with speculative decoding adjustments automatically mapped).

The wizard then automatically writes the updated variables (e.g. `ACTIVE_PROFILE=gemma-12b-gpu`) and paths to `.env` to load them cleanly on the next command invocation.

---

## 6. End-User Installation & Shell Auto-Completion

The migration replaces manually sourced scripts with a globally installed utility.

### Installation via `uv`
To install or upgrade the management binary globally using `uv`:
```bash
uv tool install git+https://github.com/earendil-works/pi-coding-agent-container.git
```

### Auto-Completion Setup
Generate auto-completion scripts dynamically for your shell of choice:

* **Fish Shell**:
  ```bash
  pib --show-completion fish > ~/.config/fish/completions/pib.fish
  ```
* **Zsh**:
  ```bash
  pib --show-completion zsh > ~/.zshrc.d/_pib
  ```
* **Bash**:
  ```bash
  pib --show-completion bash > ~/.local/share/bash-completion/completions/pib
  ```