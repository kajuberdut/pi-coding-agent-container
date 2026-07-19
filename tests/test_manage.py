import os
import sys
import unittest
import shutil
import subprocess
import tempfile
import stat

class TestManageScript(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory to act as the test workspace
        self.test_dir = tempfile.mkdtemp()
        self.workspace_dir = os.path.join(self.test_dir, "workspace")
        self.pidata_dir = os.path.join(self.test_dir, ".pi-data")
        self.models_dir = os.path.join(self.test_dir, "models")
        
        # Copy files needed for the test to the temp directory
        self.script_path = os.path.abspath("manage.sh")
        self.env_example_path = os.path.abspath(".env.example")
        self.pinned_versions_path = os.path.abspath("pinned-versions.env")
        
        shutil.copy(self.script_path, os.path.join(self.test_dir, "manage.sh"))
        shutil.copy(self.env_example_path, os.path.join(self.test_dir, ".env.example"))
        shutil.copy(self.pinned_versions_path, os.path.join(self.test_dir, "pinned-versions.env"))
        
        self.profiles_src_dir = os.path.abspath("profiles")
        if os.path.exists(self.profiles_src_dir):
            shutil.copytree(self.profiles_src_dir, os.path.join(self.test_dir, "profiles"))
        else:
            os.makedirs(os.path.join(self.test_dir, "profiles"))
        
        # Create mock bin directory for docker
        self.bin_dir = os.path.join(self.test_dir, "bin")
        os.makedirs(self.bin_dir)
        self.docker_log = os.path.join(self.test_dir, "docker_calls.log")
        
        # Write mock docker script
        self.mock_docker_path = os.path.join(self.bin_dir, "docker")
        with open(self.mock_docker_path, "w") as f:
            f.write(f"""#!/usr/bin/env python3
import sys
with open({repr(self.docker_log)}, "a") as log:
    log.write(" ".join(sys.argv) + "\\n")
""")
        os.chmod(self.mock_docker_path, os.stat(self.mock_docker_path).st_mode | stat.S_IEXEC)
        
        # Write mock curl script
        self.mock_curl_path = os.path.join(self.bin_dir, "curl")
        with open(self.mock_curl_path, "w") as f:
            f.write(f"""#!/usr/bin/env python3
import sys
if "-w" in sys.argv:
    print("200")
""")
        os.chmod(self.mock_curl_path, os.stat(self.mock_curl_path).st_mode | stat.S_IEXEC)
        
        # Setup env variables for the subprocess call
        self.env = os.environ.copy()
        # Prepend mock bin to PATH
        self.env["PATH"] = self.bin_dir + os.pathsep + self.env["PATH"]
        
    def tearDown(self):
        shutil.rmtree(self.test_dir)
        
    def run_manage(self, args, stdin=None):
        cmd = ["./manage.sh"] + args
        if stdin is not None:
            # stdin parameter is the string value passed to input
            return subprocess.run(
                cmd,
                cwd=self.test_dir,
                env=self.env,
                input=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
        else:
            return subprocess.run(
                cmd,
                cwd=self.test_dir,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

    def get_docker_calls(self):
        if not os.path.exists(self.docker_log):
            return []
        with open(self.docker_log, "r") as f:
            return [line.strip() for line in f.readlines()]

    def test_setup_directories_and_env(self):
        """
        What is tested: Initial setup of configuration files and target workspace directories.
        What setup is done: Isolated copy of the orchestrator script and configuration templates are prepared.
        What pass/fail criteria is evaluated: Verifies required folders are created with restricted owner-only access permissions and default configuration file templates are copied.
        What cleanup is done: Temporary directory structure is entirely deleted.
        """
        # Run clean to trigger setup()
        res = self.run_manage(["clean"])
        self.assertEqual(res.returncode, 0)
        
        # Verify directories are created
        self.assertTrue(os.path.exists(os.path.join(self.test_dir, ".pi-data")))
        self.assertTrue(os.path.exists(os.path.join(self.test_dir, "workspace")))
        self.assertTrue(os.path.exists(os.path.join(self.test_dir, "models")))
        
        # Verify directory permissions (700)
        for d in [".pi-data", "workspace", "models"]:
            mode = os.stat(os.path.join(self.test_dir, d)).st_mode
            self.assertEqual(oct(mode & 0o777), "0o700")
            
        # Verify .env is copied from .env.example
        self.assertTrue(os.path.exists(os.path.join(self.test_dir, ".env")))
        with open(os.path.join(self.test_dir, ".env"), "r") as f:
            env_content = f.read()
        with open(os.path.join(self.test_dir, ".env.example"), "r") as f:
            example_content = f.read()
        self.assertEqual(env_content, example_content)

    def test_portal_password_validation_fails_non_interactive(self):
        """
        What is tested: Password configuration check inside non-interactive automated environments.
        What setup is done: Isolated configuration is prepared with a blank password value.
        What pass/fail criteria is evaluated: Verifies that launching the portal fails with a password configuration error.
        What cleanup is done: Temporary directory structure is entirely deleted.
        """
        # PORTAL_PASSWORD is empty by default in copied .env.
        # Running up-proxy non-interactively (without tty/stdin) should fail.
        res = self.run_manage(["up-proxy"])
        self.assertEqual(res.returncode, 1)
        self.assertIn("Error: PORTAL_PASSWORD is not set in .env", res.stderr)
        
    def test_portal_password_validation_succeeds_when_configured(self):
        """
        What is tested: Portal startup flow when a password is pre-configured.
        What setup is done: Isolated configuration is prepared with a pre-configured password entry.
        What pass/fail criteria is evaluated: Verifies that starting the portal succeeds and triggers the container orchestration commands.
        What cleanup is done: Temporary directory structure is entirely deleted.
        """
        # Run clean to initialize .env first
        self.run_manage(["clean"])
        env_path = os.path.join(self.test_dir, ".env")
        
        with open(env_path, "a") as f:
            f.write("\nPORTAL_PASSWORD=securepassword123\n")
            
        # Running up-proxy should now succeed
        res = self.run_manage(["up-proxy"])
        self.assertEqual(res.returncode, 0)
        
        # Verify docker-compose commands run
        calls = self.get_docker_calls()
        self.assertTrue(any("docker compose" in c and "up -d" in c and "model-proxy" in c for c in calls))
        self.assertTrue(any("docker compose up --no-deps -d pi-agent agent-web-portal" in c for c in calls))

    def test_portal_password_interactive_prompt(self):
        """
        What is tested: Interactive prompt for setting up the session password.
        What setup is done: Isolated configuration is prepared and a mock virtual terminal session is opened to write inputs.
        What pass/fail criteria is evaluated: Verifies that typing the password in the terminal successfully saves the input to the configuration file.
        What cleanup is done: Mock terminal handles are closed and temporary directory structure is entirely deleted.
        """
        # Initialize .env first
        self.run_manage(["clean"])
        
        import pty
        master_fd, slave_fd = pty.openpty()
        
        # Start manage.sh up-proxy with slave_fd as stdin/stdout/stderr
        proc = subprocess.Popen(
            ["./manage.sh", "up-proxy"],
            cwd=self.test_dir,
            env=self.env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=True
        )
        
        # Close the slave descriptor in the parent process
        os.close(slave_fd)
        
        # Write the password and confirmation to the master descriptor
        import time
        time.sleep(0.5)
        os.write(master_fd, b"interactivepass123\n")
        time.sleep(0.5)
        os.write(master_fd, b"interactivepass123\n")
        
        # Wait for the process to exit
        proc.wait(timeout=5)
        os.close(master_fd)
        
        self.assertEqual(proc.returncode, 0)
        
        # Verify the password was saved to .env
        with open(os.path.join(self.test_dir, ".env"), "r") as f:
            env_content = f.read()
        self.assertIn("PORTAL_PASSWORD=interactivepass123", env_content)

    def test_clean_command(self):
        """
        What is tested: The cleaning sequence target.
        What setup is done: Isolated configuration is prepared.
        What pass/fail criteria is evaluated: Verifies that the clean command calls the docker-compose down subcommand.
        What cleanup is done: Temporary directory structure is entirely deleted.
        """
        res = self.run_manage(["clean"])
        self.assertEqual(res.returncode, 0)
        calls = self.get_docker_calls()
        # Verify it runs down with all/proxy profiles
        self.assertTrue(any("docker compose --profile all --profile proxy down --remove-orphans" in c for c in calls))

    def test_build_command(self):
        """
        What is tested: The building sequence target.
        What setup is done: Isolated configuration is prepared.
        What pass/fail criteria is evaluated: Verifies that the build command calls the docker-compose build subcommand.
        What cleanup is done: Temporary directory structure is entirely deleted.
        """
        res = self.run_manage(["build"])
        self.assertEqual(res.returncode, 0)
        calls = self.get_docker_calls()
        self.assertTrue(any("docker compose --profile all build" in c for c in calls))

    def test_up_runner_command(self):
        """
        What is tested: The model runner startup target.
        What setup is done: Isolated configuration is prepared.
        What pass/fail criteria is evaluated: Verifies that the runner startup command launches the model service container.
        What cleanup is done: Temporary directory structure is entirely deleted.
        """
        res = self.run_manage(["up-runner"])
        self.assertEqual(res.returncode, 0)
        calls = self.get_docker_calls()
        self.assertTrue(any("docker compose" in c and "up -d" in c and "local-model-runner" in c for c in calls))

    def test_shell_command(self):
        """
        What is tested: The interactive container shell target.
        What setup is done: Isolated configuration is prepared.
        What pass/fail criteria is evaluated: Verifies that the shell target runs the bash environment inside the container.
        What cleanup is done: Temporary directory structure is entirely deleted.
        """
        res = self.run_manage(["shell"])
        self.assertEqual(res.returncode, 0)
        calls = self.get_docker_calls()
        self.assertTrue(any("docker compose run --entrypoint /bin/bash --rm pi-agent" in c for c in calls))

    def test_profile_commands(self):
        """
        What is tested: The profile subcommand targets (list, select, clear).
        What setup is done: Dummy profile files are written to profiles/ directory.
        What pass/fail criteria is evaluated: Verifies profile list prints available profiles, select writes ACTIVE_PROFILE to .env, and clear clears it.
        What cleanup is done: Temporary directory structure is entirely deleted.
        """
        # 1. Test listing available profiles
        res = self.run_manage(["profile"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("gemma-12b-gpu", res.stdout)
        
        # 2. Test selecting a valid profile
        res = self.run_manage(["profile", "gemma-12b-cpu"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("Profile 'gemma-12b-cpu' is now active.", res.stdout)
        
        # Verify it was written to .env
        env_path = os.path.join(self.test_dir, ".env")
        with open(env_path, "r") as f:
            env_content = f.read()
        self.assertIn("ACTIVE_PROFILE=gemma-12b-cpu", env_content)
        
        # 3. Test listing again shows active profile
        res = self.run_manage(["profile"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("* gemma-12b-cpu (active)", res.stdout)
        
        # 4. Test selecting an invalid profile
        res = self.run_manage(["profile", "non-existent-profile"])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Error: Profile 'non-existent-profile' not found in profiles/ directory.", res.stderr)
        
        # 5. Test clearing profile
        res = self.run_manage(["profile", "clear"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("Active profile cleared. Using default .env settings.", res.stdout)
        
        # Verify it was cleared in .env
        with open(env_path, "r") as f:
            env_content = f.read()
        self.assertIn("ACTIVE_PROFILE=", env_content)
        self.assertNotIn("ACTIVE_PROFILE=gemma-12b-cpu", env_content)

if __name__ == "__main__":
    unittest.main()
