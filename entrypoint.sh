#!/usr/bin/env bash
set -e

# Cleanup function to kill the tmux server when the container stops
cleanup() {
    echo "Shutting down tmux server..."
    tmux -S /shared/tmux.sock kill-server 2>/dev/null || true
    exit 0
}

# Trap signals for graceful shutdown
trap cleanup SIGINT SIGTERM

echo "Initializing pi-agent workspace..."

# Loop to keep the tmux session alive
while true; do
    if ! tmux -S /shared/tmux.sock has-session -t pi 2>/dev/null; then
        echo "Starting new tmux session for pi..."
        tmux -S /shared/tmux.sock new-session -d -s pi pi
    fi
    # Sleep in background and wait for signals
    sleep 2 &
    wait $!
done
