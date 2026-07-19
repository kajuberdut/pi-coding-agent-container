# Changelog

All notable changes to this project will be documented in this file.

---

## [1.1.1] - 2026-07-18

### Detailed Changes

#### 1. Fix Docker Compose Interpolation Error for PORTAL_PASSWORD
* **Runtime Validation Refactor**: Replaced the strict, parse-time Docker Compose validation (`${PORTAL_PASSWORD:?set PORTAL_PASSWORD in .env}`) with runtime shell validation inside the portal container. This prevents administrative commands (like `./manage.sh clean` or `./manage.sh build`) from failing when `PORTAL_PASSWORD` is not configured in `.env`.
* **Interactive Setup Helper**: Added a helper function to `manage.sh` that prompts the user interactively to configure and confirm the web portal password if it is blank or unset when running commands that start the portal (e.g., `./manage.sh up-proxy`). It securely writes the password to `.env` using Python to escape special characters.
* **DRY Refactoring of `manage.sh`**: Extracted duplicate argument parsing and `docker compose run` logic into a reusable `run_agent_container` function. Centralized the sequence to stand up and verify model proxy connectivity into a `start_model_proxy` function, reducing duplication and improving maintainability.

#### 2. Dependency Upgrades & Security Patches
* **Alpine Socat Upgrade**: Updated the `MODEL_PROXY_IMAGE` (socat proxy container) to `alpine/socat:1.8.1.3` (with content digest pinning) to address the **CVE-2026-56123** heap-based buffer overflow vulnerability.
* **Pi Coding Agent Upgrade**: Bumped the globally installed agent dependency from `0.80.7` to `0.80.10` in `Dockerfile.agent`.

#### 3. Automated Test Suite
* **Unit Testing Support**: Introduced a automated unit test suite inside [tests/test_manage.py](file:///home/giblesnot/code/pi-coding-agent-container/tests/test_manage.py). The test suite verifies setup directories/permissions, non-interactive password failures, interactive password setups (using pseudo-TTYs), and orchestrator targets (mocking `docker`, `docker compose`, and `curl`).
* **Test Command Integration**: Integrated a new `test` target in [manage.sh](file:///home/giblesnot/code/pi-coding-agent-container/manage.sh) that allows running the entire test suite via `./manage.sh test`.
* **Repository Cleanliness**: Updated [.gitignore](file:///home/giblesnot/code/pi-coding-agent-container/.gitignore) to exclude Python compilation artifacts (`.pyc`, `.pyo`, `.pyd`, `__pycache__/`) and local test runner caches (`.pytest_cache/`).

#### 4. Dynamic SSH Tunnel Routing for Linux & Docker Desktop (Issue #9)
* **Dynamic Host Gateway IP**: Replaced the hardcoded `host.docker.internal` routing in `model-proxy` with the `MODEL_PROXY_HOST` environment variable, defaulting to `host.docker.internal`.
* **Dynamic OS Detection**: Configured `manage.sh` to auto-detect the operating system. On native Linux, it exports `MODEL_PROXY_HOST` as the custom bridge gateway IP `172.28.0.1`. On macOS/Windows, it falls back to `host.docker.internal`. This resolves connection failures on Linux when the SSH tunnel is bound strictly to the recommended secure gateway interface address instead of loopback or wildcards.

#### 5. Early Model Runner Crash Diagnostics & Log Output
* **Startup Status Polling**: Implemented a non-blocking startup polling check in `manage.sh` for commands that orchestrate local model execution (`run`, `up-runner`, `shell`).
* **Instantaneous Crash Loop Interception**: If the `llama_cpp_server` container fails to start, goes into a `restarting` state, or increments its restart count, the CLI immediately aborts startup, prints the exit code/status, and outputs the last 25 lines of the container's logs to `stderr`. This gives immediate visibility into CUDA OOM errors, missing model files, or invalid command-line flags instead of silently hanging on the compose healthcheck wait.
* **Silent Successful Progress Output**: Replaced direct shell output/redirection with the `docker compose --progress quiet` flag when launching the `local-model-runner` and `model-proxy` services. This suppresses successful progress status lines (`✔ Container Running`) on the terminal while preserving actual compose parsing or runtime errors.

#### 6. Composable Configuration Profiles
* **Profile Configuration Directory**: Created a `profiles/` directory to house model and hardware configuration sets (e.g. `qwen-27b-gpu.env`, `gemma-12b-gpu.env`, `gemma-12b-cpu.env`).
* **Hardware Isomorphic Compose Integration**: Extracted the VRAM GPU resource reservation blocks out of the main [docker-compose.yml](file:///home/giblesnot/code/pi-coding-agent-container/docker-compose.yml) into a separate, composable [docker-compose.gpu.yml](file:///home/giblesnot/code/pi-coding-agent-container/docker-compose.gpu.yml) file.
* **Dynamic Compose Merging**: Configured the orchestrator to automatically load files via the `COMPOSE_FILE` environment variable. If `USE_GPU=true` is set (either in a profile or `.env`), `manage.sh` dynamically appends the GPU override file using the host OS's native compose file path separator (`:` on Linux/macOS and `;` on Windows).
* **CLI Profile Targets**: Added a `./manage.sh profile [name|clear]` case subcommand to list available configurations, highlight the active configuration, dynamically swap active profile settings by updating `ACTIVE_PROFILE` in `.env`, or revert to default settings.
* **Expanded Verification Coverage**: Configured isolated unit tests inside [tests/test_manage.py](file:///home/giblesnot/code/pi-coding-agent-container/tests/test_manage.py) to simulate profiles setup, list, activate, clear, and error scenarios.

---

## [1.1.0] - 2026-07-18

### Detailed Changes

#### 1. Web Portal Security Hardening (Issue #4)
* **Loopback Port Binding**: Configured the web portal container's exposed port in `docker-compose.yml` to bind strictly to loopback (`127.0.0.1:7681:7681`) to prevent unauthorized local area network (LAN) access to the session.
* **Session Credentials Verification**: Configured `ttyd` to enforce password authentication (`pi` user) mapped to the `PORTAL_PASSWORD` environment variable.
* **Fail-Safe Startup**: Used Docker Compose variable validation (`${PORTAL_PASSWORD:?set PORTAL_PASSWORD in .env}`) to prevent the portal container from starting if the password is unset or empty.
* **Documentation**: Updated the README to guide users on establishing secure SSH tunnels for remote portal access, configure credentials, and warned about the security risks of exposing the portal directly to the LAN without TLS termination.

#### 2. Vulnerability Scanner Scoping (Issue #5)
* **Scoping Refactor**: Refactored the `scan)` case branch in `manage.sh` into `scan` and `trivy_scan` functions, correcting a bash syntax error where the `local` keyword was used outside of function scope. This ensures that the local model runner image scan is not aborted early by `set -e` and completes successfully.

#### 3. Dedicated Pinned Versions Configuration (Issue #6)
* **Dedicated Env File**: Created a dedicated `pinned-versions.env` file to track default pinned digests (`MODEL_RUNNER_IMAGE`, `MODEL_PROXY_IMAGE`, `TRIVY_IMAGE`) separately from the user's local configuration.
* **Auto-Loading**: Modified `setup()` in `manage.sh` to automatically load `pinned-versions.env` before `.env`. This exports the default pinned images to Docker Compose while keeping the user's local `.env` clean (users can still override them in `.env` if desired).
* **Decoupled Configuration**: Replaced all inline hardcoded digests in `docker-compose.yml` and `manage.sh` with these environment variables, establishing `pinned-versions.env` as the single source of truth for the stack's pinned image digests.
* **Documentation**: Updated the README to guide users on using multiple `--env-file` flags when calling `docker compose` directly, with encouragement to use `manage.sh` to automate loading of pinned versions.

#### 4. DNS Alias Collision Resolution (Issue #7)
* **Conflict Resolution**: Centralized the stopping of conflicting containers via a new `stop_conflicting` helper in `manage.sh`. Integrated this guard across the `run`, `run-proxy`, `up-proxy`, `up-runner`, and `shell` case branches to stop any lingering `model-proxy` before local model execution. This prevents DNS alias collision coin-flips on the internal Docker bridge network.
* **Auto Network Initialization**: Added a dedicated `init_network` helper in `manage.sh` that runs `docker compose up --no-start model-proxy` to guarantee that the isolated external network and bridge gateway interface exist on the host automatically on first run. This helper is called selectively only on container-startup branches (`run`, `run-proxy`, `up-proxy`, `up-runner`, and `shell`), preventing any containers from being created during administrative runs like `./manage.sh clean` or `./manage.sh build`.

#### 5. Remote Tunnel Bridge Binding (Issue #8)
* **First-run SSH Tunnel Failure**: Documented that the Docker network bridge interface (`172.28.0.1`) does not exist on a fresh clone prior to initialization, which causes first-run SSH binding commands to fail. Added notes in the README directing users to run `./manage.sh build` (or any other `./manage.sh` command) to trigger network initialization before establishing the SSH tunnel.

#### 6. Model Loading Health Check
* **Model Loading Synchronization**: Configured a fast-polling health check for the `local-model-runner` service and updated the `pi-agent` dependency to `condition: service_healthy`, preventing the agent from querying the server while the model is loading (resolving `503` errors).

#### 7. Run Command Modes
* **Default Print Mode & Interactive Flag**: Configured `./manage.sh run` and `run-proxy` to automatically default to print mode (`-p`) when prompt arguments are passed, and introduced a new `-i` flag to request an interactive session starting with an initial prompt.

---

## [1.0.0] - 2026-07-15

This release focuses on **Security Hardening, Sandbox Isolation, and Supply Chain Integrity** across the containerized execution environment.

### 🚀 Highlights & Summary
* **Docker Socket Removal:** Replaced the Docker socket mount in the web portal container with a secure, unprivileged shared `tmux` socket over a named volume.
* **Inference Exposure Tightening:** Pinned the local model runner port to localhost loopback, updated remote tunneling advice to bind to the gateway IP, and scoped UFW firewall suggestions to a narrow `/24` subnet.
* **Supply Chain Pinning & Hardening:** Pinned all parent and third-party Docker images by SHA-256 digests. Pinned the global agent CLI install to a specific version with transitive npm script executions disabled.
* **Vulnerability Audits:** Integrated an on-demand container image vulnerability scanner target (`./manage.sh scan`) powered by Trivy.

---

### Detailed Changes

#### 1. Web Portal Shell Hardening (Issue #1)
* **Removed `/var/run/docker.sock` Mount:** Completely decoupled the network-exposed web portal from the host’s Docker socket, eliminating a critical host-root escalation vector.
* **Shared tmux Socket Architecture:** The portal container now interacts with the agent container exclusively by attaching to a shared `tmux` session via a Unix domain socket (`/shared/tmux.sock`) on a named Docker volume (`tmux-shared`).
* **Container Hardening:** Added unprivileged runtime execution controls:
  * Restructured the container to run under the host's unprivileged UID/GID.
  * Dropped all Linux capabilities (`cap_drop: [ALL]`).
  * Enforced a read-only root filesystem with `no-new-privileges:true` and a temporary writable `tmpfs` on `/tmp`.
* **Opt-in Profile:** Moved the portal container under the opt-in compose profile (`portal`) so it is not launched by default.

#### 2. Inference Isolation & Port Bindings (Issue #2)
* **Loopback default:** Bound the local `llama.cpp` runner port mapping in `docker-compose.yml` strictly to loopback (`127.0.0.1`) by default.
* **SSH Tunnel Gateway Binding:** Replaced the `-g` (GatewayPorts) remote inference guidance in the README. Users are now directed to bind the SSH port-forward specifically to the Docker bridge gateway interface IP (`172.28.0.1`):
  ```bash
  ssh -L 172.28.0.1:<EXTERNAL_MODEL_PORT>:localhost:8080 user@remote-desktop
  ```
* **Scoped Subnetting & UFW Rules:** Pinned the compose `external_network` to the `172.28.0.0/24` subnet. Scoped UFW instructions in the README and warnings in `pick.py` strictly to this `/24` block (avoiding broad RFC-1918 `/12` blocks).
* **Pre-flight Connectivity Validation:** Implemented an early-exit check in `manage.sh` that validates container-side model runner access before the agent starts, catching tunnel dropouts and firewall blocks immediately.
* **Alias Collision Resolution:** Configured `docker compose stop` triggers in `manage.sh` to enforce mutual exclusivity between `model-proxy` and `local-model-runner` containers, resolving DNS alias resolution flips on the internal bridge network.

#### 3. Supply Chain Security & Reproducibility (Issue #3)
* **Digest Pinning:** Pinned all base and dependency images by their cryptographic SHA-256 digests in `Dockerfile.agent`, `Dockerfile.portal`, `docker-compose.yml`, `.env`, and `.env.example`.
* **NPM Script Disabling:** Pinned the global agent package to version `0.80.7` and appended the `--ignore-scripts` flag during installation in `Dockerfile.agent` to block pre/post-install script executions from compromised upstream dependencies.
* **Vulnerability Scanning:** Added a new `./manage.sh scan` target using the official `aquasec/trivy:0.72.0` (pinned by digest) to check both built custom images and pulled upstream images (`socat` and `llama.cpp`) for high and critical CVEs.
* **Multi-Profile Builds:** Updated `build` and `update` commands in `manage.sh` to build under `--profile all` to ensure all container configurations build predictably.
