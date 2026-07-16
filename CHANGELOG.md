# Changelog

All notable changes to this project will be documented in this file.

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
