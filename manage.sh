#!/usr/bin/env bash
set -e

# Automatically capture host credentials
export HOST_UID=$(id -u)
export HOST_GID=$(id -g)

setup() {
    mkdir -p .pi-data workspace models
    chmod 700 .pi-data workspace models
    
    # Auto-initialize .env if missing
    if [ ! -f .env ] && [ -f .env.example ]; then
        echo "Creating .env from .env.example..."
        cp .env.example .env
    fi

    # Load environment variables
    if [ -f .env ]; then
        set -a
        source .env
        set +a
    fi
}

check_external_port() {
    local port="${EXTERNAL_MODEL_PORT:-8080}"
    local url="http://127.0.0.1:$port/v1/models"
    echo "Checking for OpenAI-compatible model server on host port $port..."
    
    if command -v curl >/dev/null 2>&1; then
        local status
        status=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 2 "$url" || true)
        if [ "$status" != "200" ] && [ "$status" != "401" ] && [ "$status" != "403" ]; then
            echo "Error: No OpenAI-compatible model server detected on localhost:$port (HTTP Status: $status)." >&2
            echo "Please ensure your remote model server or SSH tunnel is running and exposing an API at $url" >&2
            exit 1
        fi
    elif command -v wget >/dev/null 2>&1; then
        # Try reaching via wget (timeout 2s)
        if ! wget -q --spider --timeout=2 "$url" >/dev/null 2>&1; then
            echo "Error: No OpenAI-compatible model server detected on localhost:$port." >&2
            echo "Please ensure your remote model server or SSH tunnel is running and exposing an API at $url" >&2
            exit 1
        fi
    else
        # Fallback to simple socket check if neither curl nor wget is available
        if ! (echo > /dev/tcp/127.0.0.1/"$port") >/dev/null 2>&1; then
            echo "Error: No active listener detected on localhost:$port." >&2
            echo "Please ensure your remote model server or SSH tunnel is running." >&2
            exit 1
        fi
    fi
    echo "Model server detected on port $port."
}

case "$1" in
    build)
        setup
        docker compose build
        ;;
    update)
        setup
        docker compose build --no-cache
        ;;
    run)
        setup
        shift # Drop the "run" command from arguments
        # Forces the container to run interactively, forwarding all remaining flags directly to the agent.
        # Explicitly overrides the background keep-alive entrypoint (tail -f /dev/null).
        docker compose run --entrypoint pi --rm pi-agent "$@"
        ;;
    run-proxy)
        setup
        check_external_port
        shift # Drop the "run-proxy" command from arguments
        # Starts the model proxy container and runs the agent interactively
        docker compose up -d model-proxy
        docker compose run --no-deps --entrypoint pi --rm pi-agent "$@"
        ;;
    up-proxy)
        setup
        check_external_port
        # Starts the model proxy, agent, and web portal in the background
        docker compose up --no-deps -d model-proxy pi-agent agent-web-portal
        ;;
    shell)
        setup
        docker compose run --entrypoint /bin/bash --rm pi-agent
        ;;
    clean)
        docker compose down
        ;;
    pick)
        python3 pick.py
        ;;
    *)
        echo "Usage: $0 {build|update|pick|run [args...]|run-proxy [args...]|up-proxy|shell|clean}"
        exit 1
        ;;
esac