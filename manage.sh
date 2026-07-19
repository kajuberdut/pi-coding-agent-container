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

    # Load active profile configuration if specified
    if [ -n "${ACTIVE_PROFILE}" ]; then
        local profile_path="profiles/${ACTIVE_PROFILE}.env"
        if [ -f "$profile_path" ]; then
            set -a
            source "$profile_path"
            set +a
        else
            echo "Warning: ACTIVE_PROFILE '${ACTIVE_PROFILE}' is configured, but '${profile_path}' does not exist." >&2
        fi
    fi

    # Configure COMPOSE_FILE based on USE_GPU setting and OS
    if [ "$USE_GPU" = "true" ] || [ "$USE_GPU" = "1" ]; then
        if [ "$(uname)" = "Linux" ] || [ "$(uname)" = "Darwin" ]; then
            export COMPOSE_FILE="docker-compose.yml:docker-compose.gpu.yml"
        else
            export COMPOSE_FILE="docker-compose.yml;docker-compose.gpu.yml"
        fi
    else
        export COMPOSE_FILE="docker-compose.yml"
    fi

    # Set default MODEL_PROXY_HOST based on OS if not already specified
    if [ -z "${MODEL_PROXY_HOST}" ]; then
        if [ "$(uname)" = "Linux" ]; then
            export MODEL_PROXY_HOST="172.28.0.1"
        else
            export MODEL_PROXY_HOST="host.docker.internal"
        fi
    fi
}

ensure_portal_password() {
    # Check if PORTAL_PASSWORD is set and not empty
    if [ -z "${PORTAL_PASSWORD}" ]; then
        # If stdin is not a terminal, we cannot prompt interactively
        if [ ! -t 0 ]; then
            echo "Error: PORTAL_PASSWORD is not set in .env." >&2
            echo "Please set PORTAL_PASSWORD in .env to run the web portal." >&2
            exit 1
        fi
        
        echo "The web portal requires a password to secure your session (username: pi)."
        local pwd1 pwd2
        while true; do
            read -rsp "Enter new web portal password: " pwd1
            echo ""
            if [ -z "$pwd1" ]; then
                echo "Password cannot be empty. Please try again." >&2
                continue
            fi
            read -rsp "Confirm password: " pwd2
            echo ""
            if [ "$pwd1" != "$pwd2" ]; then
                echo "Passwords do not match. Please try again." >&2
                continue
            fi
            break
        done
        
        # Save the password to .env file
        python3 -c '
import sys, re, shlex
content = ""
try:
    with open(".env", "r") as f:
        content = f.read()
except FileNotFoundError:
    pass

pattern = r"^(PORTAL_PASSWORD\s*=).*$"
replacement = f"PORTAL_PASSWORD={shlex.quote(sys.argv[1])}"

new_content, count = re.subn(pattern, replacement, content, flags=re.MULTILINE)
if count == 0:
    new_content = content.rstrip() + f"\n{replacement}\n"

with open(".env", "w") as f:
    f.write(new_content)
' "$pwd1"
        
        # Export it to the current shell environment
        export PORTAL_PASSWORD="$pwd1"
        echo "Password saved to .env and configured."
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

check_runner_health_and_logs() {
    # Check if the container exists
    if ! docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^llama_cpp_server$'; then
        return 0
    fi
    
    local is_pre_running="${1:-0}"
    local status restart_count
    
    # If the container was already running, just do a single instantaneous health check
    if [ "$is_pre_running" -eq 1 ]; then
        status=$(docker inspect -f '{{.State.Status}}' llama_cpp_server 2>/dev/null || true)
        if [ "$status" = "exited" ] || [ "$status" = "restarting" ] || [ "$status" = "dead" ]; then
            echo "" >&2
            echo "========================================================================" >&2
            echo "ERROR: The local model runner (llama_cpp_server) is not running!" >&2
            echo "Current Status: $status" >&2
            echo "------------------------------------------------------------------------" >&2
            echo "Recent container logs:" >&2
            docker logs --tail 25 llama_cpp_server >&2
            echo "========================================================================" >&2
            echo "Please check your .env settings (e.g., GPU_LAYERS, MODEL_FILE, LLAMA_ARGS)." >&2
            echo "========================================================================" >&2
            return 1
        fi
        return 0
    fi
    
    # If the container was newly started, poll for up to 6 seconds to capture early crash loops
    local i
    for i in {1..12}; do
        status=$(docker inspect -f '{{.State.Status}}' llama_cpp_server 2>/dev/null || true)
        restart_count=$(docker inspect -f '{{.RestartCount}}' llama_cpp_server 2>/dev/null || echo 0)
        
        if [ "$status" = "exited" ] || [ "$status" = "restarting" ] || [ "$status" = "dead" ] || [ "$restart_count" -gt 0 ]; then
            echo "" >&2
            echo "========================================================================" >&2
            echo "ERROR: The local model runner (llama_cpp_server) is failing to start!" >&2
            echo "Current Status: $status | Restart Count: $restart_count" >&2
            echo "------------------------------------------------------------------------" >&2
            echo "Recent container logs:" >&2
            docker logs --tail 25 llama_cpp_server >&2
            echo "========================================================================" >&2
            echo "Please check your .env settings (e.g., GPU_LAYERS, MODEL_FILE, LLAMA_ARGS)." >&2
            echo "========================================================================" >&2
            return 1
        fi
        sleep 0.5
    done
    return 0
}

start_and_check_runner() {
    local runner_running=0
    if docker inspect -f '{{.State.Status}}' llama_cpp_server 2>/dev/null | grep -q "^running$"; then
        runner_running=1
    fi
    
    docker compose --progress quiet up -d local-model-runner
    
    check_runner_health_and_logs "$runner_running"
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

start_model_proxy() {
    init_network
    check_external_port
    stop_conflicting "local-model-runner"
    # Starts the model proxy container first to check connectivity
    docker compose --progress quiet up -d model-proxy
    check_proxy_connectivity
}

run_agent_container() {
    local no_deps="$1"
    shift
    
    local compose_flags=""
    if [ ! -t 0 ]; then
        compose_flags="-T"
    fi
    
    local no_deps_flag=""
    if [ "$no_deps" -eq 1 ]; then
        no_deps_flag="--no-deps"
    fi

    local interactive=0
    local clean_args=()
    local has_mode_or_print=0

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
            docker compose run $no_deps_flag $compose_flags --entrypoint pi --rm pi-agent "${clean_args[@]}"
        else
            local use_spinner=0
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
                    run_with_spinner docker compose run $no_deps_flag --quiet $compose_flags --entrypoint pi --rm pi-agent -p "${clean_args[@]}"
                else
                    run_with_spinner docker compose run $no_deps_flag --quiet $compose_flags --entrypoint pi --rm pi-agent "${clean_args[@]}"
                fi
            else
                docker compose run $no_deps_flag $compose_flags --entrypoint pi --rm pi-agent "${clean_args[@]}"
            fi
        fi
    else
        docker compose run $no_deps_flag $compose_flags --entrypoint pi --rm pi-agent
    fi
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

manage_profile() {
    local target_profile="$1"
    
    if [ -z "$target_profile" ]; then
        echo "Available profiles:"
        if [ ! -d "profiles" ]; then
            echo "  No profiles directory found."
            return 0
        fi
        
        # Load .env to get the active profile
        local active=""
        if [ -f .env ]; then
            active=$(grep -E '^ACTIVE_PROFILE=' .env | cut -d'=' -f2- | tr -d '"'\'' ')
        fi
        
        local has_profiles=0
        for f in profiles/*.env; do
            [ -e "$f" ] || continue
            has_profiles=1
            local name
            name=$(basename "$f" .env)
            if [ "$name" = "$active" ]; then
                echo "  * $name (active)"
            else
                echo "    $name"
            fi
        done
        
        if [ $has_profiles -eq 0 ]; then
            echo "  No profile files found in profiles/."
        fi
        return 0
    fi
    
    if [ "$target_profile" = "clear" ] || [ "$target_profile" = "none" ] || [ "$target_profile" = "off" ]; then
        # Clear ACTIVE_PROFILE in .env
        python3 -c '
import sys, re
content = ""
try:
    with open(".env", "r") as f:
        content = f.read()
except FileNotFoundError:
    pass

pattern = r"^(ACTIVE_PROFILE\s*=).*$"
new_content, count = re.subn(pattern, "ACTIVE_PROFILE=", content, flags=re.MULTILINE)

with open(".env", "w") as f:
    f.write(new_content)
'
        echo "Active profile cleared. Using default .env settings."
        return 0
    fi
    
    # Enable profile
    local profile_file="profiles/${target_profile}.env"
    if [ ! -f "$profile_file" ]; then
        echo "Error: Profile '${target_profile}' not found in profiles/ directory." >&2
        return 1
    fi
    
    # Write ACTIVE_PROFILE to .env
    python3 -c '
import sys, re, shlex
content = ""
try:
    with open(".env", "r") as f:
        content = f.read()
except FileNotFoundError:
    pass

pattern = r"^(ACTIVE_PROFILE\s*=).*$"
replacement = f"ACTIVE_PROFILE={shlex.quote(sys.argv[1])}"

new_content, count = re.subn(pattern, replacement, content, flags=re.MULTILINE)
if count == 0:
    new_content = content.rstrip() + f"\n{replacement}\n"

with open(".env", "w") as f:
    f.write(new_content)
' "$target_profile"

    echo "Profile '${target_profile}' is now active."
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
        if ! start_and_check_runner; then
            exit 1
        fi
        run_agent_container 0 "$@"
        ;;
    run-proxy)
        setup
        start_model_proxy
        shift # Drop the "run-proxy" command from arguments
        run_agent_container 1 "$@"
        ;;
    up-proxy)
        setup
        ensure_portal_password
        start_model_proxy
        # Starts the agent and web portal in the background
        docker compose up --no-deps -d pi-agent agent-web-portal
        ;;
    up-runner)
        setup
        init_network
        stop_conflicting "model-proxy"
        # Starts only the local model runner in the background
        if ! start_and_check_runner; then
            exit 1
        fi
        ;;
    shell)
        setup
        init_network
        stop_conflicting "model-proxy"
        if ! start_and_check_runner; then
            exit 1
        fi
        docker compose run --entrypoint /bin/bash --rm pi-agent
        ;;
    clean)
        setup
        docker compose --profile all --profile proxy down --remove-orphans
        ;;
    profile)
        setup
        shift
        manage_profile "$@"
        ;;
    pick)
        python3 pick.py
        ;;
    scan)
        scan
        ;;
    test)
        python3 -m unittest discover -s tests
        ;;
    *)
        echo "Usage: $0 {build|update|pick|profile [name|clear]|run [args...]|run-proxy [args...]|up-proxy|up-runner|shell|clean|scan|test}"
        echo ""
        echo "Options for run / run-proxy:"
        echo "  -i                           # Run interactively after processing initial prompt"
        echo "  -p, --print \"prompt\"         # Run prompt, print response, and exit (default behavior when prompt is passed without -i)"
        exit 1
        ;;
esac