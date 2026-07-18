#!/usr/bin/env bash
set -e

# Automatically capture host credentials
export HOST_UID=$(id -u)
export HOST_GID=$(id -g)

setup() {
    mkdir -p .pi-data workspace models
    chmod 700 .pi-data workspace models
    
    # Load default pinned versions
    if [ -f pinned-versions.env ]; then
        set -a
        source pinned-versions.env
        set +a
    fi
    
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

init_network() {
    # Ensure Docker network and proxy container are created (for remote SSH tunnel gateway IP binding on first run)
    docker compose up --no-start model-proxy >/dev/null 2>&1 || true
}

stop_conflicting() {
    local target="$1"
    if [ "$target" = "local-model-runner" ]; then
        # If we are starting model-proxy, stop local-model-runner
        docker compose --profile runner --profile all stop local-model-runner >/dev/null 2>&1 || true
    elif [ "$target" = "model-proxy" ]; then
        # If we are starting local-model-runner (or pi-agent which starts it), stop model-proxy
        docker compose --profile proxy stop model-proxy >/dev/null 2>&1 || true
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

trivy_scan() {
    local label="$1"
    local img="$2"
    echo "Scanning $label image ($img)..."
    docker run --rm \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -v trivy-cache:/root/.cache \
      "${TRIVY_IMAGE}" \
      image --severity HIGH,CRITICAL --exit-code 1 "$img"
}

scan() {
    setup
    local failed=0
    trivy_scan "agent" "local/pi-coding-agent:latest" || failed=1
    trivy_scan "portal" "local/agent-web-portal:latest" || failed=1
    trivy_scan "model-proxy" "${MODEL_PROXY_IMAGE}" || failed=1
    
    local runner_img="${MODEL_RUNNER_IMAGE}"
    trivy_scan "local-model-runner" "$runner_img" || failed=1
    
    if [ "$failed" -ne 0 ]; then
        echo "Error: One or more images failed the security scan." >&2
        exit 1
    fi
}

run_with_spinner() {
    local tmp_file
    tmp_file=$(mktemp)
    
    # Hide cursor
    tput civis 2>/dev/null || echo -ne "\033[?25l"
    
    # Run the command in the background, redirecting stdout and stderr
    "$@" > "$tmp_file" 2>&1 &
    local pid=$!
    
    # Spinner setup
    local delay=0.1
    local spinstr='|/-\'
    echo -n "Pi is thinking...  "
    
    # Trap to ensure cleanup on exit/interrupt
    trap 'kill -9 "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; rm -f "$tmp_file"; tput cnorm 2>/dev/null || echo -ne "\033[?25h"; exit 1' INT TERM
    
    while kill -0 "$pid" 2>/dev/null; do
        local temp=${spinstr#?}
        printf "%c" "$spinstr"
        spinstr=$temp${spinstr%"$temp"}
        sleep $delay
        printf "\b"
    done
    
    # Wait for process and capture exit status
    wait "$pid"
    local exit_code=$?
    
    # Clear "Pi is thinking... "
    printf "\r\033[K"
    
    # Restore cursor
    tput cnorm 2>/dev/null || echo -ne "\033[?25h"
    
    # Print output
    cat "$tmp_file"
    rm -f "$tmp_file"
    
    # Reset trap
    trap - INT TERM
    
    return $exit_code
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
        init_network
        stop_conflicting "model-proxy"
        shift # Drop the "run" command from arguments

        compose_flags=""
        if [ ! -t 0 ]; then
            compose_flags="-T"
        fi

        interactive=0
        clean_args=()
        has_mode_or_print=0

        for arg in "$@"; do
            if [ "$arg" = "-i" ]; then
                interactive=1
            else
                clean_args+=("$arg")
                if [[ "$arg" == "-p" || "$arg" == "--print" || "$arg" == "--mode" || "$arg" == "-h" || "$arg" == "--help" || "$arg" == "--version" ]]; then
                    has_mode_or_print=1
                fi
            fi
        done

        if [ ${#clean_args[@]} -gt 0 ]; then
            if [ "$interactive" -eq 1 ]; then
                docker compose run $compose_flags --entrypoint pi --rm pi-agent "${clean_args[@]}"
            else
                use_spinner=0
                if [ "$has_mode_or_print" -eq 0 ]; then
                    use_spinner=1
                else
                    for arg in "${clean_args[@]}"; do
                        if [[ "$arg" == "-p" || "$arg" == "--print" ]]; then
                            use_spinner=1
                            break
                        fi
                    done
                fi

                if [ "$use_spinner" -eq 1 ]; then
                    if [ "$has_mode_or_print" -eq 0 ]; then
                        run_with_spinner docker compose run --quiet $compose_flags --entrypoint pi --rm pi-agent -p "${clean_args[@]}"
                    else
                        run_with_spinner docker compose run --quiet $compose_flags --entrypoint pi --rm pi-agent "${clean_args[@]}"
                    fi
                else
                    docker compose run $compose_flags --entrypoint pi --rm pi-agent "${clean_args[@]}"
                fi
            fi
        else
            docker compose run $compose_flags --entrypoint pi --rm pi-agent
        fi
        ;;
    run-proxy)
        setup
        init_network
        check_external_port
        shift # Drop the "run-proxy" command from arguments
        stop_conflicting "local-model-runner"
        # Starts the model proxy container and runs the agent interactively
        docker compose up -d model-proxy
        check_proxy_connectivity

        compose_flags=""
        if [ ! -t 0 ]; then
            compose_flags="-T"
        fi

        interactive=0
        clean_args=()
        has_mode_or_print=0

        for arg in "$@"; do
            if [ "$arg" = "-i" ]; then
                interactive=1
            else
                clean_args+=("$arg")
                if [[ "$arg" == "-p" || "$arg" == "--print" || "$arg" == "--mode" || "$arg" == "-h" || "$arg" == "--help" || "$arg" == "--version" ]]; then
                    has_mode_or_print=1
                fi
            fi
        done

        if [ ${#clean_args[@]} -gt 0 ]; then
            if [ "$interactive" -eq 1 ]; then
                docker compose run --no-deps $compose_flags --entrypoint pi --rm pi-agent "${clean_args[@]}"
            else
                use_spinner=0
                if [ "$has_mode_or_print" -eq 0 ]; then
                    use_spinner=1
                else
                    for arg in "${clean_args[@]}"; do
                        if [[ "$arg" == "-p" || "$arg" == "--print" ]]; then
                            use_spinner=1
                            break
                        fi
                    done
                fi

                if [ "$use_spinner" -eq 1 ]; then
                    if [ "$has_mode_or_print" -eq 0 ]; then
                        run_with_spinner docker compose run --no-deps --quiet $compose_flags --entrypoint pi --rm pi-agent -p "${clean_args[@]}"
                    else
                        run_with_spinner docker compose run --no-deps --quiet $compose_flags --entrypoint pi --rm pi-agent "${clean_args[@]}"
                    fi
                else
                    docker compose run --no-deps $compose_flags --entrypoint pi --rm pi-agent "${clean_args[@]}"
                fi
            fi
        else
            docker compose run --no-deps $compose_flags --entrypoint pi --rm pi-agent
        fi
        ;;
    up-proxy)
        setup
        init_network
        check_external_port
        stop_conflicting "local-model-runner"
        # Starts the model proxy container first to check connectivity
        docker compose up -d model-proxy
        check_proxy_connectivity
        # Starts the agent and web portal in the background
        docker compose up --no-deps -d pi-agent agent-web-portal
        ;;
    up-runner)
        setup
        init_network
        stop_conflicting "model-proxy"
        # Starts only the local model runner in the background
        docker compose up -d local-model-runner
        ;;
    shell)
        setup
        init_network
        stop_conflicting "model-proxy"
        docker compose run --entrypoint /bin/bash --rm pi-agent
        ;;
    clean)
        setup
        docker compose --profile all --profile proxy down --remove-orphans
        ;;
    pick)
        python3 pick.py
        ;;
    scan)
        scan
        ;;
    *)
        echo "Usage: $0 {build|update|pick|run [args...]|run-proxy [args...]|up-proxy|up-runner|shell|clean|scan}"
        echo ""
        echo "Options for run / run-proxy:"
        echo "  -i                           # Run interactively after processing initial prompt"
        echo "  -p, --print \"prompt\"         # Run prompt, print response, and exit (default behavior when prompt is passed without -i)"
        exit 1
        ;;
esac