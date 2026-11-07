import json
import re
import shlex
import subprocess
import sys
import urllib.request
from pathlib import Path
import typer

def parse_env_file(path: Path) -> dict[str, str]:
    res = {}
    if not path.exists():
        return res
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                parsed = parse_env_line(line)
                if parsed:
                    key, val = parsed
                    res[key] = val
    except Exception:
        pass
    return res

def parse_env_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "=" not in line:
        return None
    key, val = line.split("=", 1)
    key = key.strip()
    val = val.strip()
    
    if val.startswith('"'):
        idx = 1
        val_chars = []
        while idx < len(val):
            if val[idx] == '"':
                break
            elif val[idx] == '\\' and idx + 1 < len(val):
                val_chars.append(val[idx+1])
                idx += 2
            else:
                val_chars.append(val[idx])
                idx += 1
        val = "".join(val_chars)
    elif val.startswith("'"):
        idx = 1
        val_chars = []
        while idx < len(val):
            if val[idx] == "'":
                break
            elif val[idx] == '\\' and idx + 1 < len(val):
                val_chars.append(val[idx+1])
                idx += 2
            else:
                val_chars.append(val[idx])
                idx += 1
        val = "".join(val_chars)
    else:
        if " #" in val:
            val = val.split(" #", 1)[0].strip()
        elif val.startswith("#"):
            val = ""
    return key, val


def update_agent_config(base_dir: Path, selected_model: str):
    # Update agent configurations (.pi-data/agent/)
    agent_dir = base_dir / ".pi-data" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    models_json_path = agent_dir / "models.json"
    settings_json_path = agent_dir / "settings.json"
    
    # Update models.json
    try:
        models_data = {}
        if models_json_path.exists():
            try:
                models_data = json.loads(models_json_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        
        providers = models_data.setdefault("providers", {})
        llama_cpp = providers.setdefault("llama-cpp", {
            "baseUrl": "http://local-model-runner:8080/v1",
            "api": "openai-completions",
            "apiKey": "none"
        })
        llama_cpp["models"] = [{"id": selected_model}]
        models_json_path.write_text(json.dumps(models_data, indent=2), encoding="utf-8")
        typer.echo(f"\033[32mSuccessfully updated models config: {models_json_path.relative_to(base_dir)}\033[0m")
    except Exception as e:
        typer.echo(f"Warning: Could not update models.json: {e}", err=True)

    # Update settings.json
    try:
        settings_data = {}
        if settings_json_path.exists():
            try:
                settings_data = json.loads(settings_json_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        settings_data["defaultProvider"] = "llama-cpp"
        settings_data["defaultModel"] = selected_model
        settings_data.setdefault("autocompleteMaxVisible", 7)
        settings_data.setdefault("defaultThinkingLevel", "off")
        settings_json_path.write_text(json.dumps(settings_data, indent=2), encoding="utf-8")
        typer.echo(f"\033[32mSuccessfully updated settings config: {settings_json_path.relative_to(base_dir)}\033[0m")
    except Exception as e:
        typer.echo(f"Warning: Could not update settings.json: {e}", err=True)


def configure_wizard(env: dict[str, str], base_dir: Path):
    typer.echo("\n--- Pi Box Configuration Wizard ---")
    
    # Step 1: Model Family
    typer.echo("\nStep 1: Select Model Family")
    typer.echo(" [1] Gemma")
    typer.echo(" [2] Qwen")
    typer.echo(" [3] Other / Remote")
    typer.echo(" [4] Cancel")
    
    while True:
        choice = input("Enter choice (1-4): ").strip()
        if choice == "4" or not choice:
            typer.echo("Cancelled.")
            return
        if choice in ("1", "2", "3"):
            family_map = {"1": "Gemma", "2": "Qwen", "3": "Other"}
            selected_family = family_map[choice]
            break
        typer.echo("Invalid choice. Please select 1, 2, 3, or 4.")
        
    # Discover remote models
    external_model_port = int(env.get("EXTERNAL_MODEL_PORT", "8080"))
    remote_models = []
    for host in ("127.0.0.1", "172.28.0.1"):
        try:
            req = urllib.request.Request(f"http://{host}:{external_model_port}/v1/models")
            with urllib.request.urlopen(req, timeout=1.0) as response:
                data = json.loads(response.read().decode('utf-8'))
                if "data" in data:
                    remote_models = [m["id"] for m in data["data"] if isinstance(m, dict) and "id" in m]
                elif "models" in data:
                    remote_models = [m["name"] for m in data["models"] if isinstance(m, dict) and "name" in m]
                if remote_models:
                    break
        except Exception:
            pass

    # Find GGUF files locally
    models_dir = base_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    local_ggufs = sorted([
        p.relative_to(models_dir)
        for p in models_dir.rglob('*')
        if p.is_file() and p.suffix.lower() == '.gguf'
    ])
    
    family_lower = selected_family.lower()
    
    def matches_family(name: str) -> bool:
        if selected_family == "Other":
            return not ("gemma" in name.lower() or "qwen" in name.lower())
        return family_lower in name.lower()
        
    local_matches = [str(m) for m in local_ggufs if matches_family(str(m))]
    remote_matches = [str(m) for m in remote_models if matches_family(str(m))]
    
    choices = []
    for m in local_matches:
        choices.append({"type": "local", "name": m})
    for m in remote_matches:
        if m not in local_matches:
            choices.append({"type": "remote", "name": m})
            
    # Fallback to all local/remote models if no match was found for the chosen family
    if not choices and (local_ggufs or remote_models):
        typer.echo(f"\nNote: No models matched family '{selected_family}'. Showing all discovered models instead:")
        for m in local_ggufs:
            choices.append({"type": "local", "name": str(m)})
        for m in remote_models:
            if str(m) not in [c["name"] for c in choices]:
                choices.append({"type": "remote", "name": str(m)})
            
    # Step 2: Model Size / File
    typer.echo("\nStep 2: Select Model File / Size")
    current_model = env.get("MODEL_FILE", "")
    
    for idx, choice_item in enumerate(choices, start=1):
        type_str = "Local" if choice_item["type"] == "local" else "Remote Proxy"
        is_curr = " (currently selected)" if choice_item["name"] == current_model else ""
        typer.echo(f" [{idx}] {choice_item['name']} [{type_str}]{is_curr}")
        
    custom_local_idx = len(choices) + 1
    custom_remote_idx = len(choices) + 2
    cancel_idx = len(choices) + 3
    
    typer.echo(f" [{custom_local_idx}] Custom Local GGUF File (not in list)")
    typer.echo(f" [{custom_remote_idx}] Custom Remote API model endpoint")
    typer.echo(f" [{cancel_idx}] Cancel")
    
    selected_model = None
    is_selected_remote = False
    
    while True:
        choice = input(f"Enter choice (1-{cancel_idx}): ").strip()
        if not choice or choice == str(cancel_idx):
            typer.echo("Cancelled.")
            return
        if choice.isdigit():
            val = int(choice)
            if 1 <= val <= len(choices):
                chosen = choices[val - 1]
                selected_model = chosen["name"]
                is_selected_remote = chosen["type"] == "remote"
                break
            elif val == custom_local_idx:
                path_str = input("Enter path to custom local GGUF file relative to models/: ").strip()
                if path_str:
                    full_path = models_dir / path_str
                    if not full_path.exists():
                        typer.echo(f"Warning: file {full_path} not found, but setting it anyway.")
                    selected_model = path_str
                    break
                else:
                    typer.echo("Empty input. Cancelled.")
                    return
            elif val == custom_remote_idx:
                endpoint_str = input("Enter custom remote API model name: ").strip()
                if endpoint_str:
                    selected_model = endpoint_str
                    is_selected_remote = True
                    break
                else:
                    typer.echo("Empty input. Cancelled.")
                    return
        typer.echo(f"Invalid choice. Please enter a number between 1 and {cancel_idx}.")

    # Step 3: Execution Profile
    typer.echo("\nStep 3: Select Execution Profile")
    
    profiles_dir = base_dir / "profiles"
    all_profiles = []
    if profiles_dir.exists():
        all_profiles = sorted([p.stem for p in profiles_dir.glob("*.env")])
        
    model_name_lower = selected_model.lower()
    recommended_profiles = []
    
    if "gemma" in model_name_lower:
        if "12b" in model_name_lower:
            recommended_profiles = ["gemma-12b-gpu", "gemma-12b-cpu"]
        elif "4b" in model_name_lower or "e4b" in model_name_lower:
            recommended_profiles = ["gemma-4b-gpu"]
        elif "26b" in model_name_lower or "31b" in model_name_lower or "a4b" in model_name_lower:
            recommended_profiles = ["gemma-31b-gpu"]
    elif "qwen" in model_name_lower:
        if "27b" in model_name_lower:
            if "instruct" in model_name_lower:
                recommended_profiles = ["qwen-27b-instruct-gpu"]
            else:
                recommended_profiles = ["qwen-27b-gpu"]
            
    profile_options = []
    for p in all_profiles:
        label = p
        if p in recommended_profiles:
            label += " (Recommended)"
        profile_options.append({"name": p, "label": label})
        
    for idx, opt in enumerate(profile_options, start=1):
        typer.echo(f" [{idx}] {opt['label']}")
        
    none_idx = len(profile_options) + 1
    cancel_idx_p = len(profile_options) + 2
    typer.echo(f" [{none_idx}] None (Use default .env settings)")
    typer.echo(f" [{cancel_idx_p}] Cancel")
    
    selected_profile = None
    while True:
        p_choice = input(f"Enter choice (1-{cancel_idx_p}): ").strip()
        if not p_choice or p_choice == str(cancel_idx_p):
            typer.echo("Cancelled.")
            return
        if p_choice.isdigit():
            val = int(p_choice)
            if 1 <= val <= len(profile_options):
                selected_profile = profile_options[val - 1]["name"]
                break
            elif val == none_idx:
                selected_profile = ""
                break
        typer.echo(f"Invalid choice. Please enter a number between 1 and {cancel_idx_p}.")
        
    env_path = base_dir / ".env"
    content = ""
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        
    # Update MODEL_FILE
    pattern_model = r"^(MODEL_FILE\s*=).*$"
    replacement_model = f"MODEL_FILE={shlex.quote(selected_model)}"
    content, count_model = re.subn(pattern_model, replacement_model, content, flags=re.MULTILINE)
    if count_model == 0:
        content = content.rstrip() + f"\n{replacement_model}\n"
        
    # Update ACTIVE_PROFILE
    pattern_profile = r"^(ACTIVE_PROFILE\s*=).*$"
    profile_val = selected_profile if selected_profile else ""
    replacement_profile = f"ACTIVE_PROFILE={shlex.quote(profile_val)}"
    content, count_profile = re.subn(pattern_profile, replacement_profile, content, flags=re.MULTILINE)
    if count_profile == 0:
        content = content.rstrip() + f"\n{replacement_profile}\n"
        
    env_path.write_text(content, encoding="utf-8")
    typer.echo(f"\n\033[32mSuccessfully updated configuration!\033[0m")
    typer.echo(f" MODEL_FILE is now set to '{selected_model}'")
    if selected_profile:
        typer.echo(f" ACTIVE_PROFILE is now set to '{selected_profile}'")
    else:
        typer.echo(" ACTIVE_PROFILE has been cleared")

    # Update agent configurations (.pi-data/agent/)
    update_agent_config(base_dir, selected_model)

    # Check UFW firewall for remote models
    if is_selected_remote:
        try:
            ufw_status = subprocess.check_output(["systemctl", "is-active", "ufw"], stderr=subprocess.DEVNULL).decode().strip()
            if ufw_status == "active":
                typer.echo("\n\033[33m[Notice] UFW firewall is active on your host.\033[0m")
                typer.echo("If the agent container gets connection errors, you may need to run this on your host:")
                typer.echo(f"  \033[36msudo ufw allow from 172.28.0.0/24 to any port {external_model_port} proto tcp\033[0m")
        except Exception:
            pass
        typer.echo("")
