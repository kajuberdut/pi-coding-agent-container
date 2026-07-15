#!/usr/bin/env bash
# squash-push.sh - Squashes local development commits and pushes a single release commit to GitHub.

set -e

# Configuration
DEV_BRANCH="main"
RELEASE_BRANCH="github-main"
REMOTE_NAME="github"

# 1. Verify we are in a git repository
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "Error: Not in a git repository." >&2
    exit 1
fi

# 2. Check if GitHub remote is configured
if ! git remote | grep -q "^${REMOTE_NAME}$"; then
    echo "Error: Remote '${REMOTE_NAME}' is not configured." >&2
    echo "Please add it first using:" >&2
    echo "  git remote add ${REMOTE_NAME} <github-repository-url>" >&2
    exit 1
fi

# Get current branch to return to it later
ORIGINAL_BRANCH=$(git branch --show-current)

# Ensure dev branch is clean
if [ -n "$(git status --porcelain)" ]; then
    echo "Error: You have unstaged or uncommitted changes. Please commit or stash them first." >&2
    exit 1
fi

# Fetch remote updates
echo "Fetching from ${REMOTE_NAME}..."
git fetch ${REMOTE_NAME} || echo "Warning: Could not fetch from ${REMOTE_NAME}. Proceeding anyway..."

# Ensure the release branch exists locally
if ! git show-ref --verify --quiet "refs/heads/${RELEASE_BRANCH}"; then
    echo "Local branch '${RELEASE_BRANCH}' not found."
    # Check if it exists on the remote
    if git show-ref --verify --quiet "refs/remotes/${REMOTE_NAME}/main"; then
        echo "Creating '${RELEASE_BRANCH}' tracking '${REMOTE_NAME}/main'..."
        git checkout -b "${RELEASE_BRANCH}" "${REMOTE_NAME}/main"
    else
        echo "Creating new orphan release branch '${RELEASE_BRANCH}'..."
        git checkout --orphan "${RELEASE_BRANCH}"
        # Remove any staged files if it's a new orphan branch
        git rm -rf . --quiet || true
    fi
else
    # Switch to release branch
    git checkout "${RELEASE_BRANCH}"
fi

# Pull latest state from GitHub release branch
if git ls-remote --exit-code ${REMOTE_NAME} main >/dev/null 2>&1; then
    echo "Pulling latest changes from ${REMOTE_NAME}/main..."
    git pull ${REMOTE_NAME} main --rebase || echo "Warning: Pull failed. Proceeding with local history..."
fi

# Squash-merge changes from dev branch
echo "Squashing changes from '${DEV_BRANCH}' into '${RELEASE_BRANCH}'..."
if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
    # Empty head (orphan branch with no commits yet)
    git checkout "${DEV_BRANCH}" -- .
    git add -A
else
    if ! git merge --squash "${DEV_BRANCH}"; then
        echo "Error: Squash merge encountered conflicts. Please resolve them on '${RELEASE_BRANCH}'." >&2
        exit 1
    fi
fi


# Check if there are actually changes to commit
if git diff --quiet --cached; then
    echo "No new changes detected. '${RELEASE_BRANCH}' is already up-to-date with '${DEV_BRANCH}'."
    git checkout "${ORIGINAL_BRANCH}"
    exit 0
fi

# Get commit message from argument, or prompt user
COMMIT_MSG="$1"
if [ -z "$COMMIT_MSG" ]; then
    echo "Enter a commit message for the squashed release:"
    read -r COMMIT_MSG
fi

if [ -z "$COMMIT_MSG" ]; then
    COMMIT_MSG="Release: Squashed update from ${DEV_BRANCH} on $(date +'%Y-%m-%d %H:%M:%S')"
fi

# Commit the squashed changes
git commit -m "$COMMIT_MSG"

# Push to GitHub
echo "Pushing squashed commit to ${REMOTE_NAME}..."
git push ${REMOTE_NAME} "${RELEASE_BRANCH}":main

# Switch back to development branch
echo "Switching back to '${ORIGINAL_BRANCH}'..."
git checkout "${ORIGINAL_BRANCH}"

echo "Successfully squashed and pushed to GitHub!"
