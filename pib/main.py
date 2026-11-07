import getpass
import os
import pathlib
import platform
import re
import shlex
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
import typer

from pib.cli_pick import parse_env_file, configure_wizard, update_agent_config

app = typer.Typer(
    help="Pi Coding Agent container management CLI",
    add_completion=True,
)

def find_base_dir() -> Path:
    # Walk up from the current working directory to find the project root
    curr = Path.cwd().resolve()
    for parent in [curr] + list(curr.parents):
        # We look for indicators that this is the pi-coding-agent-container root
        if (parent / "pinned-versions.env").exists() and (parent / "pib").is_dir():
            return parent
            
    # Check the directory structure relative to the installed package file
    pkg_base = Path(__file__).resolve().parent.parent
    if (pkg_base / "pinned-versions.env").exists() and (pkg_base / "pib").is_dir():
        return pkg_base
        
    typer.echo("Error: Could not find the project root directory.", err=True)
    typer.echo("Please run this command from within your pi-coding-agent-container repository.", err=True)
    raise typer.Exit(1)

def setup_env() -> tuple[Path, dict[str, str]]:
    base_dir = find_base_dir()
    os.chdir(base_dir)
    
    # 1. Ensure directories exist
    for d in (".pi-data", "workspace", "models"):
        p = base_dir / d
        p.mkdir(parents=True, exist_ok=True)
        try:
            p.chmod(0o700)
        except Exception:
            pass
            
    # 2. Auto-initialize .env if missing
    env_path = base_dir / ".env"
    env_example_path = base_dir / ".env.example"
    if not env_path.exists() and env_example_path.exists():
        typer.echo("Creating .env from .env.example...")
        shutil.copyfile(env_example_path, env_path)
        
    # 3. Load host GID/UID
    try:
        host_uid = os.getuid()
        host_gid = os.getgid()
    except AttributeError:
        host_uid = 1000
        host_gid = 1000
        
    # Build merged environment
    merged_env = os.environ.copy()
    merged_env.setdefault("HOST_UID", str(host_uid))
    merged_env.setdefault("HOST_GID", str(host_gid))
    
    # Load pinned versions
    pinned_path = base_dir / "pinned-versions.env"
    if pinned_path.exists():
        pinned_vals = parse_env_file(pinned_path)
        for k, v in pinned_vals.items():
            merged_env.setdefault(k, v)
            
    # Load .env variables
    if env_path.exists():
        dotenv_vals = parse_env_file(env_path)
        for k, v in dotenv_vals.items():
            merged_env[k] = v
            
    # Load active profile
    active_profile = merged_env.get("ACTIVE_PROFILE", "").strip()
    if active_profile:
        profile_path = base_dir / "profiles" / f"{active_profile}.env"
        if profile_path.exists():
            profile_vals = parse_env_file(profile_path)
            for k, v in profile_vals.items():
                merged_env[k] = v
        else:
            typer.echo(f"Warning: ACTIVE_PROFILE '{active_profile}' is configured, but '{profile_path}' does not exist.", err=True)
            
    # Set default MODEL_PROXY_HOST
    if not merged_env.get("MODEL_PROXY_HOST"):
        if platform.system() == "Linux":
            merged_env["MODEL_PROXY_HOST"] = "172.28.0.1"
        else:
            merged_env["MODEL_PROXY_HOST"] = "host.docker.internal"
            
    return base_dir, merged_env

def get_compose_cmd(env: dict[str, str]) -> list[str]:
    cmd = ["docker", "compose", "-f", "docker-compose.yml"]
    use_gpu = env.get("USE_GPU", "").lower() in ("true", "1")
    if use_gpu:
        cmd.extend(["-f", "docker-compose.gpu.yml"])
    return cmd

def get_password(prompt: str) -> str:
    try:
        import termios
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        new_settings = termios.tcgetattr(fd)
        new_settings[3] = new_settings[3] & ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSADRAIN, new_settings)
        try:
            sys.stdout.write(prompt)
            sys.stdout.flush()
            passwd = sys.stdin.readline().rstrip('\n')
            sys.stdout.write("\n")
            sys.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except Exception:
        import getpass
        passwd = getpass.getpass(prompt)
    return passwd

def ensure_portal_password(env: dict[str, str], base_dir: Path):
    portal_password = env.get("PORTAL_PASSWORD", "").strip()
    if not portal_password:
        if not sys.stdin.isatty():
            typer.echo("Error: PORTAL_PASSWORD is not set in .env.", err=True)
            typer.echo("Please set PORTAL_PASSWORD in .env to run the web portal.", err=True)
            raise typer.Exit(1)
        
        typer.echo("The web portal requires a password to secure your session (username: pi).")
        while True:
            pwd1 = get_password("Enter new web portal password: ")
            if not pwd1:
                typer.echo("Password cannot be empty. Please try again.", err=True)
                continue
            pwd2 = get_password("Confirm password: ")
            if pwd1 != pwd2:
                typer.echo("Passwords do not match. Please try again.", err=True)
                continue
            break
            
        # Save password to .env file
        env_path = base_dir / ".env"
        content = ""
        if env_path.exists():
            content = env_path.read_text(encoding="utf-8")
            
        pattern = r"^(PORTAL_PASSWORD\s*=).*$"
        replacement = f"PORTAL_PASSWORD={shlex.quote(pwd1)}"
        new_content, count = re.subn(pattern, replacement, content, flags=re.MULTILINE)
        if count == 0:
            new_content = content.rstrip() + f"\n{replacement}\n"
            
        env_path.write_text(new_content, encoding="utf-8")
        env["PORTAL_PASSWORD"] = pwd1
        typer.echo("Password saved to .env and configured.")

def stop_conflicting(target: str, env: dict[str, str]):
    base_cmd = get_compose_cmd(env)
    if target == "local-model-runner":
        cmd = base_cmd + ["--profile", "runner", "--profile", "all", "stop", "local-model-runner"]
    elif target == "model-proxy":
        cmd = base_cmd + ["--profile", "proxy", "stop", "model-proxy"]
    else:
        return
    subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def check_external_port(env: dict[str, str]):
    if os.environ.get("PIB_TESTING") == "1":
        typer.echo("Testing mode: stubbing model server detection.")
        return
        
    port = env.get("EXTERNAL_MODEL_PORT", "8080")
    ips = ["127.0.0.1", "172.28.0.1"]
    success = False
    
    for ip in ips:
        url = f"http://{ip}:{port}/v1/models"
        typer.echo(f"Checking for OpenAI-compatible model server on {ip}:{port}...")
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=2.0) as response:
                if response.status in (200, 401, 403):
                    success = True
                    break
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                success = True
                break
        except Exception:
            pass
            
    if not success:
        typer.echo(f"Error: No OpenAI-compatible model server detected on localhost:{port} or 172.28.0.1:{port}.", err=True)
        typer.echo("Please ensure your remote model server or SSH tunnel is running.", err=True)
        typer.echo("Example SSH tunnel command (binding only to the Docker bridge gateway):", err=True)
        typer.echo(f"  ssh -L 172.28.0.1:{port}:localhost:8080 user@remote-machine", err=True)
        raise typer.Exit(1)
    typer.echo("Model server detected.")

def check_runner_health_and_logs(is_pre_running: bool, env: dict[str, str]) -> int:
    try:
        container_names = subprocess.check_output(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            stderr=subprocess.DEVNULL
        ).decode("utf-8").splitlines()
    except Exception:
        return 0
        
    if "llama_cpp_server" not in [name.strip() for name in container_names]:
        return 0
        
    if is_pre_running:
        try:
            status = subprocess.check_output(
                ["docker", "inspect", "-f", "{{.State.Status}}", "llama_cpp_server"],
                stderr=subprocess.DEVNULL
            ).decode("utf-8").strip()
        except Exception:
            status = ""
            
        if status in ("exited", "restarting", "dead"):
            typer.echo("", err=True)
            typer.echo("========================================================================", err=True)
            typer.echo("ERROR: The local model runner (llama_cpp_server) is not running!", err=True)
            typer.echo(f"Current Status: {status}", err=True)
            typer.echo("------------------------------------------------------------------------", err=True)
            typer.echo("Recent container logs:", err=True)
            subprocess.run(["docker", "logs", "--tail", "25", "llama_cpp_server"], env=env)
            typer.echo("========================================================================", err=True)
            typer.echo("Please check your .env settings (e.g., GPU_LAYERS, MODEL_FILE, LLAMA_ARGS).", err=True)
            typer.echo("========================================================================", err=True)
            return 1
        return 0
        
    import time
    for _ in range(12):
        try:
            status = subprocess.check_output(
                ["docker", "inspect", "-f", "{{.State.Status}}", "llama_cpp_server"],
                stderr=subprocess.DEVNULL
            ).decode("utf-8").strip()
            restart_count_str = subprocess.check_output(
                ["docker", "inspect", "-f", "{{.RestartCount}}", "llama_cpp_server"],
                stderr=subprocess.DEVNULL
            ).decode("utf-8").strip()
            restart_count = int(restart_count_str) if restart_count_str.isdigit() else 0
        except Exception:
            status = ""
            restart_count = 0
            
        if status in ("exited", "restarting", "dead") or restart_count > 0:
            typer.echo("", err=True)
            typer.echo("========================================================================", err=True)
            typer.echo("ERROR: The local model runner (llama_cpp_server) is failing to start!", err=True)
            typer.echo(f"Current Status: {status} | Restart Count: {restart_count}", err=True)
            typer.echo("------------------------------------------------------------------------", err=True)
            typer.echo("Recent container logs:", err=True)
            subprocess.run(["docker", "logs", "--tail", "25", "llama_cpp_server"], env=env)
            typer.echo("========================================================================", err=True)
            typer.echo("Please check your .env settings (e.g., GPU_LAYERS, MODEL_FILE, LLAMA_ARGS).", err=True)
            typer.echo("========================================================================", err=True)
            return 1
        time.sleep(0.5)
    return 0

def start_and_check_runner(env: dict[str, str]) -> int:
    runner_running = False
    try:
        status = subprocess.check_output(
            ["docker", "inspect", "-f", "{{.State.Status}}", "llama_cpp_server"],
            stderr=subprocess.DEVNULL
        ).decode("utf-8").strip()
        if status == "running":
            runner_running = True
    except Exception:
        pass
        
    cmd = get_compose_cmd(env) + ["--progress", "quiet", "up", "-d", "local-model-runner"]
    subprocess.run(cmd, env=env)
    
    return check_runner_health_and_logs(runner_running, env)

def check_proxy_connectivity(env: dict[str, str]):
    if os.environ.get("PIB_TESTING") == "1":
        typer.echo("Testing mode: stubbing proxy connectivity verification.")
        return
        
    typer.echo("Testing proxy connectivity to model runner from inside the container network...")
    code = """
import urllib.request, sys
try:
    urllib.request.urlopen('http://local-model-runner:8080/v1/models', timeout=2.5)
    sys.exit(0)
except Exception as e:
    sys.stderr.write(f'Connection failed: {e}\\n')
    sys.exit(1)
"""
    cmd = get_compose_cmd(env) + [
        "run", "--rm", "--no-deps", "--entrypoint", "python3", "pi-agent", "-c", code
    ]
    res = subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode != 0:
        port = env.get("EXTERNAL_MODEL_PORT", "8080")
        typer.echo("Error: The agent container cannot reach the model runner via the proxy.", err=True)
        typer.echo("This usually happens because:", err=True)
        typer.echo("  1. Your SSH tunnel is not running.", err=True)
        typer.echo("  2. A host firewall (like UFW) is blocking traffic from the Docker bridge.", err=True)
        typer.echo("     If UFW is active, ensure you ran:", err=True)
        typer.echo(f"       sudo ufw allow from 172.28.0.0/24 to any port {port} proto tcp", err=True)
        raise typer.Exit(1)
    typer.echo("Proxy connectivity verified successfully.")

def init_network(env: dict[str, str]):
    cmd = get_compose_cmd(env) + ["up", "--no-start", "model-proxy"]
    subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def start_model_proxy(env: dict[str, str]):
    init_network(env)
    check_external_port(env)
    stop_conflicting("local-model-runner", env)
    
    cmd = get_compose_cmd(env) + ["--progress", "quiet", "up", "-d", "model-proxy"]
    subprocess.run(cmd, env=env)
    check_proxy_connectivity(env)

def run_interactive(cmd: list[str], env: dict[str, str], stdin=None):
    try:
        res = subprocess.run(cmd, env=env, check=False, stdin=stdin)
        if res.returncode != 0:
            raise typer.Exit(res.returncode)
    except KeyboardInterrupt:
        raise typer.Exit(130)

def run_with_spinner(cmd: list[str], env: dict[str, str], stdin=None):
    import tempfile
    import time
    
    # Hide cursor
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()
    
    tmp_file = tempfile.NamedTemporaryFile(delete=False)
    tmp_name = tmp_file.name
    
    try:
        proc = subprocess.Popen(cmd, env=env, stdout=tmp_file, stderr=tmp_file, stdin=stdin)
        
        spinstr = "|/-\\"
        idx = 0
        sys.stdout.write("Pi is thinking...  ")
        sys.stdout.flush()
        
        while proc.poll() is None:
            sys.stdout.write(spinstr[idx % 4])
            sys.stdout.flush()
            time.sleep(0.1)
            sys.stdout.write("\b")
            sys.stdout.flush()
            idx += 1
            
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()
        
        # Restore cursor
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
        
        tmp_file.close()
        with open(tmp_name, "r", encoding="utf-8", errors="replace") as f:
            sys.stdout.write(f.read())
            sys.stdout.flush()
            
        if proc.returncode != 0:
            raise typer.Exit(proc.returncode)
            
    except KeyboardInterrupt:
        if 'proc' in locals() and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        sys.stdout.write("\r\033[K\033[?25h")
        sys.stdout.flush()
        raise typer.Exit(130)
    finally:
        try:
            os.unlink(tmp_name)
        except Exception:
            pass

def run_agent_container(no_deps: bool, args: list[str], env: dict[str, str]):
    interactive = False
    clean_args = []
    has_mode_or_print = False
    for arg in args:
        if arg in ("-i", "-c", "--continue", "-r", "--resume"):
            interactive = True
        if arg != "-i":
            clean_args.append(arg)
            if arg in ("-p", "--print", "--mode", "-h", "--help", "--version"):
                has_mode_or_print = True
                
    compose_cmd = get_compose_cmd(env) + ["run"]
    if no_deps:
        compose_cmd.append("--no-deps")
    if not interactive or not sys.stdin.isatty():
        compose_cmd.append("-T")
        
    stdin_val = None
    if not interactive and sys.stdin.isatty():
        stdin_val = subprocess.DEVNULL
        
    if clean_args:
        if interactive:
            cmd = compose_cmd + ["--entrypoint", "pi", "--rm", "pi-agent"] + clean_args
            run_interactive(cmd, env, stdin=stdin_val)
        else:
            use_spinner = False
            if not has_mode_or_print:
                use_spinner = True
            else:
                for arg in clean_args:
                    if arg in ("-p", "--print"):
                        use_spinner = True
                        break
            
            if use_spinner:
                if not has_mode_or_print:
                    cmd = compose_cmd + ["--quiet", "--entrypoint", "pi", "--rm", "pi-agent", "-p"] + clean_args
                else:
                    cmd = compose_cmd + ["--quiet", "--entrypoint", "pi", "--rm", "pi-agent"] + clean_args
                run_with_spinner(cmd, env, stdin=stdin_val)
            else:
                cmd = compose_cmd + ["--entrypoint", "pi", "--rm", "pi-agent"] + clean_args
                run_interactive(cmd, env, stdin=stdin_val)
    else:
        cmd = compose_cmd + ["--entrypoint", "pi", "--rm", "pi-agent"]
        run_interactive(cmd, env, stdin=stdin_val)


def trivy_scan(label: str, img: str, env: dict[str, str]) -> int:
    trivy_image = env.get("TRIVY_IMAGE", "aquasec/trivy:latest")
    typer.echo(f"Scanning {label} image ({img})...")
    cmd = [
        "docker", "run", "--rm",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", "trivy-cache:/root/.cache",
        trivy_image,
        "image", "--severity", "HIGH,CRITICAL", "--exit-code", "1", img
    ]
    res = subprocess.run(cmd, env=env)
    return res.returncode

def scan_images(env: dict[str, str]):
    failed = False
    if trivy_scan("agent", "local/pi-coding-agent:latest", env) != 0:
        failed = True
    if trivy_scan("portal", "local/agent-web-portal:latest", env) != 0:
        failed = True
    if trivy_scan("model-proxy", env.get("MODEL_PROXY_IMAGE", ""), env) != 0:
        failed = True
    if trivy_scan("local-model-runner", env.get("MODEL_RUNNER_IMAGE", ""), env) != 0:
        failed = True
        
    if failed:
        typer.echo("Error: One or more images failed the security scan.", err=True)
        raise typer.Exit(1)

def manage_profile_impl(target_profile: str | None, env: dict[str, str], base_dir: Path):
    profiles_dir = base_dir / "profiles"
    if not target_profile:
        typer.echo("Available profiles:")
        if not profiles_dir.exists():
            typer.echo("  No profiles directory found.")
            return
            
        active_profile = ""
        env_path = base_dir / ".env"
        if env_path.exists():
            dotenv_vals = parse_env_file(env_path)
            active_profile = dotenv_vals.get("ACTIVE_PROFILE", "").strip()
            
        has_profiles = False
        for p_file in sorted(profiles_dir.glob("*.env")):
            has_profiles = True
            name = p_file.stem
            if name == active_profile:
                typer.echo(f"  * {name} (active)")
            else:
                typer.echo(f"    {name}")
                
        if not has_profiles:
            typer.echo("  No profile files found in profiles/.")
        return
        
    env_path = base_dir / ".env"
    if target_profile in ("clear", "none", "off"):
        content = ""
        if env_path.exists():
            content = env_path.read_text(encoding="utf-8")
        pattern = r"^(ACTIVE_PROFILE\s*=).*$"
        new_content = re.sub(pattern, "ACTIVE_PROFILE=", content, flags=re.MULTILINE)
        env_path.write_text(new_content, encoding="utf-8")
        typer.echo("Active profile cleared. Using default .env settings.")
        
        # Sync agent config to the base .env model file
        base_dotenv_vals = parse_env_file(env_path)
        base_model = base_dotenv_vals.get("MODEL_FILE", "").strip()
        if base_model:
            update_agent_config(base_dir, base_model)
        return
        
    profile_file = profiles_dir / f"{target_profile}.env"
    if not profile_file.exists():
        typer.echo(f"Error: Profile '{target_profile}' not found in profiles/ directory.", err=True)
        raise typer.Exit(1)
        
    content = ""
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
    pattern = r"^(ACTIVE_PROFILE\s*=).*$"
    replacement = f"ACTIVE_PROFILE={shlex.quote(target_profile)}"
    new_content, count = re.subn(pattern, replacement, content, flags=re.MULTILINE)
    if count == 0:
        new_content = content.rstrip() + f"\n{replacement}\n"
    env_path.write_text(new_content, encoding="utf-8")
    typer.echo(f"Profile '{target_profile}' is now active.")
    
    # Sync agent config to the model file defined in active profile or fallback
    profile_vals = parse_env_file(profile_file)
    active_model = profile_vals.get("MODEL_FILE", "").strip()
    if not active_model:
        # Fall back to base .env
        base_dotenv_vals = parse_env_file(env_path)
        active_model = base_dotenv_vals.get("MODEL_FILE", "").strip()
    if active_model:
        update_agent_config(base_dir, active_model)


@app.command()
def build():
    """Compiles the container images using local Dockerfiles."""
    base_dir, env = setup_env()
    cmd = get_compose_cmd(env) + ["--profile", "all", "build"]
    run_interactive(cmd, env)

@app.command()
def update():
    """Rebuilds the container images without using cache."""
    base_dir, env = setup_env()
    cmd = get_compose_cmd(env) + ["--profile", "all", "build", "--no-cache"]
    run_interactive(cmd, env)

@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def run(ctx: typer.Context):
    """Launches the Pi Agent container."""
    base_dir, env = setup_env()
    init_network(env)
    stop_conflicting("model-proxy", env)
    if start_and_check_runner(env) != 0:
        raise typer.Exit(1)
    run_agent_container(False, ctx.args, env)

@app.command(
    "run-proxy",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def run_proxy(ctx: typer.Context):
    """Starts the model-proxy and launches the interactive agent."""
    base_dir, env = setup_env()
    start_model_proxy(env)
    run_agent_container(True, ctx.args, env)

@app.command("up-proxy")
def up_proxy():
    """Starts the proxy, portal, and agent services in the background."""
    base_dir, env = setup_env()
    ensure_portal_password(env, base_dir)
    start_model_proxy(env)
    cmd = get_compose_cmd(env) + ["up", "--no-deps", "-d", "pi-agent", "agent-web-portal"]
    run_interactive(cmd, env)

@app.command("up-runner")
def up_runner():
    """Starts the local model runner container in the background."""
    base_dir, env = setup_env()
    init_network(env)
    stop_conflicting("model-proxy", env)
    if start_and_check_runner(env) != 0:
        raise typer.Exit(1)

@app.command("configure")
def configure():
    """Consolidates model selection and hardware profile configuration into a single interactive wizard."""
    base_dir, env = setup_env()
    configure_wizard(env, base_dir)

@app.command("pick")
def pick():
    """Alias for configure wizard."""
    configure()

@app.command("shell")
def shell():
    """Spawns an interactive bash terminal inside the running agent."""
    base_dir, env = setup_env()
    init_network(env)
    stop_conflicting("model-proxy", env)
    if start_and_check_runner(env) != 0:
        raise typer.Exit(1)
    cmd = get_compose_cmd(env) + ["run", "--entrypoint", "/bin/bash", "--rm", "pi-agent"]
    run_interactive(cmd, env)

@app.command("profile")
def profile(name: str = typer.Argument(None, help="The profile name to activate, or 'clear' to reset.")):
    """Lists available profiles, switches active profile, or clears it."""
    base_dir, env = setup_env()
    manage_profile_impl(name, env, base_dir)

@app.command("scan")
def scan():
    """Audits the stack's containers for security vulnerabilities via Trivy."""
    base_dir, env = setup_env()
    scan_images(env)

@app.command("clean")
def clean():
    """Stops and removes all container layers and network bridges."""
    base_dir, env = setup_env()
    cmd = get_compose_cmd(env) + ["--profile", "all", "--profile", "proxy", "down", "--remove-orphans"]
    run_interactive(cmd, env)

@app.command("test")
def test():
    """Runs the unit test suite."""
    base_dir, env = setup_env()
    cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
    run_interactive(cmd, env)

if __name__ == "__main__":
    app()
