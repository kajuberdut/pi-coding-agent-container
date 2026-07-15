
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