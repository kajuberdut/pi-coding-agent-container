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
    local ips=("127.0.0.1" "172.28.0.1")
    local success=0

    for ip in "${ips[@]}"; do
        local url="http://$ip:$port/v1/models"
        echo "Checking for OpenAI-compatible model server on $ip:$port..."
        
        if command -v curl >/dev/null 2>&1; then
            local status
            status=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 2 "$url" || true)
            if [ "$status" = "200" ] || [ "$status" = "401" ] || [ "$status" = "403" ]; then
                success=1
                break
            fi
        elif command -v wget >/dev/null 2>&1; then
            if wget -q --spider --timeout=2 "$url" >/dev/null 2>&1; then
                success=1
                break
            fi
        else
            if (echo > /dev/tcp/"$ip"/"$port") >/dev/null 2>&1; then
                success=1
                break
            fi
        fi
    done

    if [ "$success" -ne 1 ]; then
        echo "Error: No OpenAI-compatible model server detected on localhost:$port or 172.28.0.1:$port." >&2
        echo "Please ensure your remote model server or SSH tunnel is running." >&2
        echo "Example SSH tunnel command (binding only to the Docker bridge gateway):" >&2
        echo "  ssh -L 172.28.0.1:$port:localhost:8080 user@remote-machine" >&2
        exit 1
    fi
    echo "Model server detected."
}

check_proxy_connectivity() {
    echo "Testing proxy connectivity to model runner from inside the container network..."
    if ! docker compose run --rm --no-deps --entrypoint python3 pi-agent -c "
import urllib.request, sys
try:
    urllib.request.urlopen('http://local-model-runner:8080/v1/models', timeout=2.5)
    sys.exit(0)
except Exception as e:
    sys.stderr.write(f'Connection failed: {e}\n')
    sys.exit(1)
" >/dev/null 2>&1; then
        echo "Error: The agent container cannot reach the model runner via the proxy." >&2
        echo "This usually happens because:" >&2
        echo "  1. Your SSH tunnel is not running." >&2
        echo "  2. A host firewall (like UFW) is blocking traffic from the Docker bridge." >&2
        echo "     If UFW is active, ensure you ran:" >&2
        echo "       sudo ufw allow from 172.28.0.0/24 to any port ${EXTERNAL_MODEL_PORT:-8080} proto tcp" >&2
        exit 1
    fi
    echo "Proxy connectivity verified successfully."
}

case "$1" in
    build)
        setup
        docker compose --profile all build
        ;;
    update)
        setup
        docker compose --profile all build --no-cache
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
        # Stop the local runner if it is running to avoid port/alias collision
        docker compose --profile runner --profile all stop local-model-runner >/dev/null 2>&1 || true
        # Starts the model proxy container and runs the agent interactively
        docker compose up -d model-proxy
        check_proxy_connectivity
        docker compose run --no-deps --entrypoint pi --rm pi-agent "$@"
        ;;
    up-proxy)
        setup
        check_external_port
        # Stop the local runner if it is running to avoid port/alias collision
        docker compose --profile runner --profile all stop local-model-runner >/dev/null 2>&1 || true
        # Starts the model proxy container first to check connectivity
        docker compose up -d model-proxy
        check_proxy_connectivity
        # Starts the agent and web portal in the background
        docker compose up --no-deps -d pi-agent agent-web-portal
        ;;
    up-runner)
        setup
        # Stop the model proxy if it is running to avoid port/alias collision
        docker compose --profile proxy stop model-proxy >/dev/null 2>&1 || true
        # Starts only the local model runner in the background
        docker compose up -d local-model-runner
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
    scan)
        setup
        echo "Scanning agent image (local/pi-coding-agent:latest)..."
        docker run --rm \
          -v /var/run/docker.sock:/var/run/docker.sock \
          -v trivy-cache:/root/.cache \
          aquasec/trivy:0.72.0@sha256:cffe3f5161a47a6823fbd23d985795b3ed72a4c806da4c4df16266c02accdd6f \
          image --severity HIGH,CRITICAL --exit-code 1 local/pi-coding-agent:latest
        
        echo "Scanning portal image (local/agent-web-portal:latest)..."
        docker run --rm \
          -v /var/run/docker.sock:/var/run/docker.sock \
          -v trivy-cache:/root/.cache \
          aquasec/trivy:0.72.0@sha256:cffe3f5161a47a6823fbd23d985795b3ed72a4c806da4c4df16266c02accdd6f \
          image --severity HIGH,CRITICAL --exit-code 1 local/agent-web-portal:latest

        echo "Scanning model-proxy image (alpine/socat)..."
        docker run --rm \
          -v /var/run/docker.sock:/var/run/docker.sock \
          -v trivy-cache:/root/.cache \
          aquasec/trivy:0.72.0@sha256:cffe3f5161a47a6823fbd23d985795b3ed72a4c806da4c4df16266c02accdd6f \
          image --severity HIGH,CRITICAL --exit-code 1 alpine/socat:1.8.0-r3@sha256:d85531a29ef5ba99dfb4717485c239307e2902d522a1bc010992a2728c92cfad

        local runner_img="${MODEL_RUNNER_IMAGE:-ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:93b36c099c8fc978b79fec5a09dedf6573e5e3cef4f2223453dad93e6a273305}"
        echo "Scanning local-model-runner image ($runner_img)..."
        docker run --rm \
          -v /var/run/docker.sock:/var/run/docker.sock \
          -v trivy-cache:/root/.cache \
          aquasec/trivy:0.72.0@sha256:cffe3f5161a47a6823fbd23d985795b3ed72a4c806da4c4df16266c02accdd6f \
          image --severity HIGH,CRITICAL --exit-code 1 "$runner_img"
        ;;
    *)
        echo "Usage: $0 {build|update|pick|run [args...]|run-proxy [args...]|up-proxy|up-runner|shell|clean|scan}"
        exit 1
        ;;
esac