# Pi Coding Agent (Dockerized)

A containerized, secure, and isolated development environment for running the Pi coding agent (`@earendil-works/pi-coding-agent`).

---

## Table of Contents

- [General Information](#general-information)
  - [Key Features](#key-features)
  - [Mounted Directories](#mounted-directories)
  - [Infrastructure Security & Isolation](#infrastructure-security--isolation)
- [The Pieces](#the-pieces)
  - [Pi-agent](#pi-agent)
  - [llama.cpp](#llamacpp)
  - [ttyd Container](#ttyd-container)
  - [Model Proxy Container](#model-proxy-container)
  - [manage.sh](#managesh)
- [Running and Configuration](#running-and-configuration)
  - [Prerequisites](#prerequisites)
  - [Quick Start](#quick-start)
  - [Web Portal Workspace](#web-portal-workspace)
  - [Local Inference Setup](#local-inference-setup)
  - [Proxy / Remote Inference Setup](#proxy--remote-inference-setup)
  - [Advanced Configurations](#advanced-configurations)
- [Model Files](#model-files)
  - [Why GGUF?](#why-gguf)
  - [Recommended Models](#recommended-models)

---

## General Information

This repository provides a containerized workspace for developer agent operations, designed for security, flexibility, and ease of use.

### Key Features

* **Complete Isolation**: The agent runs in an unprivileged, air-gapped Docker container with no direct access to your local network or the internet.
* **Flexible Model Inference**: Easily route traffic to a local llama.cpp runner or proxy requests to a remote model server via a secure SSH tunnel.
* **Aggressively Extensible**: Maintains the pi agents ability to self-author skills and extensions.
* **Web Portal Shell**: Start the stack and interact with the agent workspace from any browser using a lightweight web terminal.

### Mounted Directories

To enforce security and isolation, the containerized agent has access to only two host-mounted directories. These are the only areas of the host filesystem the agent can view or modify, and each serves a distinct purpose:

* **`workspace/`**: The target development area where the agent operates. Place the source code or project files you want the agent to work on here.
* **`.pi-data/`**: The agent's persistent configuration and storage area. This holds global configurations, settings, API keys, session history, and agent enhancements (skills and extensions) across container restarts.

### Infrastructure Security & Isolation

* **Network Isolation**: The agent container runs completely isolated on an air-gapped internal bridge network with no routing to the internet or the host's LAN.
* **Unprivileged Execution**: Processes in the agent container run under the `node` user (UID 1000). Host mapping prevents files written inside `workspace/` from becoming owned by `root`.
* **Filesystem Restrictions**: The agent's root filesystem is mounted as read-only. Writable directories (`/tmp`, `/home/node/.config`, `/home/node/.npm`) are backed by non-executable memory storage (`tmpfs`).

---

## The Pieces

### Pi-agent

**Pi** (`@earendil-works/pi-coding-agent`) is the terminal coding agent that powers this workspace. It is a minimal, aggressively extensible coding harness designed to adapt to your workflows. It ships with four built-in tools (`read`, `bash`, `edit`, `write`) and avoids opinionated features like sub-agents or permission popups.

#### Pi Enhances Itself

During a session, Pi can write enhancements to itself. These persist on the host filesystem inside the mounted `.pi-data/` directory (mapped from `/home/node/.pi/`):

* **Skills**: Self-contained Markdown-based capability packages loaded on-demand. They provide instructions, helper scripts, and reference documentation for specific tasks (e.g., loaded via `/skill:name` or matched automatically).
* **Extensions**: TypeScript modules that extend Pi with custom tools, commands, event handlers, keyboard shortcuts, and UI components.

| Container Path | Host Path | Purpose |
|---|---|---|
| `/home/node/.pi/agent/skills/` | `.pi-data/agent/skills/` | Global skills |
| `/home/node/.pi/agent/extensions/` | `.pi-data/agent/extensions/` | Global extensions |

> [!TIP]
> Ask Pi to build a skill or extension whenever you need a recurring capability (e.g., _"Write a skill for deploying Docker containers"_, or _"Create an extension that auto-commits after each task"_). Pi will write the files directly to `.pi-data/` so they are active on subsequent runs.

### llama.cpp

The `local-model-runner` service runs `llama.cpp` to provide local inference for the agent. It loads GGUF model files from the host-mounted `./models/` directory and exposes an OpenAI-compatible API endpoint inside the container network.

For details on supported model formats and recommended models, see [Model Files](#model-files).

### ttyd Container

A lightweight web-based terminal (`ttyd`) container that allows you to access and interact with the agent's workspace directly from a web browser. It mounts the host's Docker socket to execute commands inside the running agent container.

### Model Proxy Container

An internal network proxy container (powered by `socat`) that acts as a secure bridge, forwarding requests from the air-gapped agent container to an external model server or SSH tunnel endpoint on the host.

### manage.sh

The `./manage.sh` wrapper script on the host automates workspace directory creation and coordinates container building, selection, and execution flags:

* **`./manage.sh build`**: Builds/rebuilds the agent and web portal containers.
* **`./manage.sh update`**: Force-rebuilds the containers bypassing Docker cache.
* **`./manage.sh run [args...]`**: Performs directory setup and launches the interactive agent container, overriding the background keep-alive.
* **`./manage.sh shell`**: Spawns a bash terminal inside a temporary instance of the agent container for sandbox experimentation.
* **`./manage.sh clean`**: Tears down any active containers and shuts down the internal network bridges.
* **`./manage.sh pick`**: Scans the `./models` directory for GGUF model files and allows you to interactively select one to update the configuration. *(Note: Python is required in order to use the pick functionality).*

---

## Running and Configuration

### Prerequisites

Before starting, ensure your host environment has:
1. **Docker & Docker Compose** installed.
2. **NVIDIA Container Toolkit** (optional, required if you want to offload model inference to an NVIDIA GPU).
3. **Python 3** (Optional for core functionality but required on the host in order to use the pick command).

### Quick Start

#### 1. Initialize and Configure the Environment

Running any `./manage.sh` command (such as `./manage.sh build` below) will automatically generate your local `.env` configuration file from `.env.example` if it doesn't already exist. 

Open `.env` in a text editor and configure:
* **File Permissions**: Verify `HOST_UID` and `HOST_GID` match your host user (run `id -u` and `id -g`).
* **Git Identity**: Fill in your Git name and email so the agent's commits are correctly attributed to you.
* **Model Runner**: Set `MODEL_RUNNER_IMAGE` (use `:server-cuda` for GPU or `:server` for CPU) and place your GGUF model file inside the `./models/` directory.

#### 2. Build the Containers

Build the isolated agent and web portal images:
```bash
./manage.sh build
```

#### 3. Run the Agent

To start an interactive command-line session with the agent:
```bash
./manage.sh run
```
You can also pass arguments directly to the agent CLI:
```bash
# Get the agent version
./manage.sh run --version

# Log in to the agent
./manage.sh run /login

# Provide a direct coding instruction
./manage.sh run "Write a fast Fibonacci sequence calculator in Rust"
```

### Web Portal Workspace

To run the agent with a browser-based web terminal:

1. **Start the Stack**: Run the services in detached mode:
   ```bash
   docker compose up -d
   ```
2. **Access the Portal**: Open your browser and navigate to `http://localhost:7681` to launch the interactive `pi` agent CLI inside the workspace.

### Local Inference Setup

The local inference engine runs llama.cpp. To configure the agent to communicate with the local runner, configure the following files in `.pi-data/agent/`:

#### `.pi-data/agent/models.json`

```json
{
  "providers": {
    "llama-cpp": {
      "baseUrl": "http://local-model-runner:8080/v1",
      "api": "openai-completions",
      "apiKey": "none",
      "models": [
        {
          "id": "gemma-4-26B-A4B-it-GGUF"
        }
      ]
    }
  }
}
```

#### `.pi-data/agent/settings.json`

```json
{
  "defaultProvider": "llama-cpp",
  "defaultModel": "gemma-4-26B-A4B-it-GGUF",
  "autocompleteMaxVisible": 7,
  "defaultThinkingLevel": "off"
}
```

### Proxy / Remote Inference Setup

If you are running the `pi-agent` and the web portal on one machine (e.g., your laptop) but running your model server on a remote machine (e.g., a desktop with a GPU), you can route LLM traffic over a secure SSH tunnel without giving the agent container any external network access.

This is achieved using the `model-proxy` service (powered by `socat`), which sits on both the isolated `internal_network` and the `external_network`. It acts as a gateway, forwarding internal requests from `local-model-runner:8080` to `host.docker.internal:<EXTERNAL_MODEL_PORT>` on your host.

The default host port is `8080`. If you need to use a different port (e.g., because `8080` is already in use on your host), you can customize it by setting `EXTERNAL_MODEL_PORT` in your `.env` file.

#### Step 1: Establish the SSH Tunnel

Run this command in a terminal on your client laptop to forward your desktop's model server port (normally `8080`) to the configured `EXTERNAL_MODEL_PORT` on your local machine.

Use the `-g` flag to bind the forwarded port to all interfaces (GatewayPorts) so it is accessible to the Docker bridge:
```bash
ssh -g -L <EXTERNAL_MODEL_PORT>:localhost:8080 user@remote-desktop
```
*(Alternatively, you can omit the `-g` flag by adding `GatewayPorts yes` under the corresponding `Host` block in your `~/.ssh/config` file to enable gateway ports automatically).*

#### Step 2: Configure your Firewall (if UFW is active)

By default, firewalls like UFW block incoming traffic from Docker bridge interfaces to the host. Since the `model-proxy` container must communicate with the host's forwarded port, you must explicitly allow the Docker subnet:
```bash
sudo ufw allow from 172.16.0.0/12 to any port <EXTERNAL_MODEL_PORT>
```

#### Step 3: Run the Services

* **Interactive CLI**:
  Run the agent interactively in your terminal:
  ```bash
  ./manage.sh run-proxy
  ```
* **Web Portal + Background Agent**:
  Start the stack in detached mode:
  ```bash
  ./manage.sh up-proxy
  ```
  Open http://localhost:7681 in your browser to access the interactive web shell.

### Advanced Configurations

#### Git Identity & Local Checkpoints

The agent utilizes Git locally inside the container to track its file changes, create rollback checkpoints, and commit completed tasks.

If the host's `./workspace` directory is initialized as a Git repository (e.g., via `git init` or by cloning a repository into it), the containerized agent will automatically detect it and use the Git identity configured in your `.env` to sign and attribute its commits.

Because the container runs in an air-gapped network, **remote Git operations (such as `git push`, `git pull`, or cloning external repositories) must be run from your host machine.**

#### Pushing a Squashed History to GitHub

If your upstream repository is a local Forgejo instance, but you also want to occasionally push a squashed version to GitHub (showing only a clean sequence of release commits on GitHub, never your granular development commits), you can use the included `squash-push.sh` helper script:

1. **Initial Remote Setup**:
   Add your GitHub repository as a remote named `github` on your host:
   ```bash
   git remote add github <github-repository-url>
   ```

2. **Execute the Script**:
   Run the script from the root of the repository:
   ```bash
   ./squash-push.sh "Your Release Commit Message"
   ```
   *(If you omit the message argument, the script will prompt you for one).*

The script automatically manages switching to a local `github-main` branch, performing a squash-merge from `main`, committing the changes as a single release commit, pushing to GitHub, and switching back to `main`.

---

## Model Files

### Why GGUF?

GGUF (GGML Unified Format) is a binary file format designed for fast loading and saving of models, and for ease of reading, for inference with GGML and GGML-based executors (such as `llama.cpp`). It is the easiest format to start with due to several key design features:
- **Single-File Deployment**: All information needed to load the model (including weights, tokenizer configurations, and hyperparameters) is self-contained in a single `.gguf` file, eliminating the need for external configuration files.
- **Key-Value Metadata Structure**: GGUF utilizes a key-value structure for metadata (hyperparameters). This makes it highly extensible, allowing new information/features to be annotated to the model without breaking compatibility with existing executors.
- **`mmap` Compatibility**: GGUF models can be loaded using memory mapping (`mmap`) for extremely fast loading.
- **Quantization Support**: GGUF natively supports quantized formats (such as 4-bit, 5-bit, or 8-bit precision). Quantization reduces the memory (RAM/VRAM) required to run the models by up to 70%+ while retaining almost all of their baseline intelligence and reasoning capability.

### Recommended Models

For agentic coding and reasoning tasks, **Gemma 4** (by Google) and **Qwen 3.6** (by Alibaba) are excellent starting points. They offer strong code-generation, tool-use, and instructions-following capabilities.

The list below shows the approximate file sizes for their `Q4_K_M` (4-bit quantized, medium) versions, which represent an excellent balance of inference speed, memory usage, and quality:

#### Gemma 4 Family
*   **Gemma 4 E2B** (~2.3B parameters): **~3.11 GB** (Ideal for low-end laptops/mobile devices)
*   **Gemma 4 E4B** (~4.5B parameters): **~4.98 GB** (Good for entry-level devices)
*   **Gemma 4 12B** (~12B parameters): **~7.00 GB** (Excellent balance for consumer GPUs)
*   **Gemma 4 26B A4B** (~25.2B parameters, MoE): **~16.9 GB** (Highly optimized Mixture-of-Experts)
*   **Gemma 4 31B** (~31B parameters, Dense): **~18.3 GB** (Flagship reasoning model for workstations/servers)

#### Qwen 3.6 Family
*   **Qwen 3.6-27B** (~27B parameters, Dense): **~16–17 GB** (Great sweet spot for high-end consumer GPUs)
*   **Qwen 3.6-35B-A3B** (~35B parameters, MoE): **~20–23 GB** (Highly capable Mixture-of-Experts)

> [!TIP]
> Ensure your system has enough combined RAM/VRAM to fit the selected GGUF file size, plus an additional 2–3 GB for context window overhead (KV cache) and general runtime overhead.
