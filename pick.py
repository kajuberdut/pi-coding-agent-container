#!/usr/bin/env python3
import json
import shutil
import sys
from pathlib import Path

def main():
    # 1. Determine paths using pathlib
    base_dir = Path(__file__).resolve().parent
    env_path = base_dir / '.env'
    env_example_path = base_dir / '.env.example'
    models_dir = base_dir / 'models'
    
    agent_dir = base_dir / '.pi-data' / 'agent'
    models_json_path = agent_dir / 'models.json'
    settings_json_path = agent_dir / 'settings.json'

    # 2. Check and copy .env if missing
    if not env_path.exists():
        if env_example_path.exists():
            print(f"Creating .env from .env.example...")
            shutil.copyfile(env_example_path, env_path)
        else:
            print("Error: Neither .env nor .env.example exists in the root directory.", file=sys.stderr)
            sys.exit(1)

    # 3. Ensure models directory exists
    models_dir.mkdir(parents=True, exist_ok=True)

    # Find GGUF files recursively using pathlib
    local_models = sorted([
        str(p.relative_to(models_dir))
        for p in models_dir.rglob('*')
        if p.is_file() and p.suffix.lower() == '.gguf'
    ])

    # 3. Get configurations from .env
    current_model = None
    external_model_port = 8080
    if env_path.exists():
        try:
            with open(env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('MODEL_FILE='):
                        parts = line.split('=', 1)
                        if len(parts) == 2:
                            current_model = parts[1].strip().strip('"').strip("'")
                    elif line.startswith('EXTERNAL_MODEL_PORT='):
                        parts = line.split('=', 1)
                        if len(parts) == 2:
                            try:
                                external_model_port = int(parts[1].strip().strip('"').strip("'"))
                            except ValueError:
                                pass
        except Exception as e:
            print(f"Warning: Could not read configurations from .env: {e}", file=sys.stderr)

    # Try to fetch remote models from the localhost SSH tunnel port
    remote_models = []
    try:
        import urllib.request
        req = urllib.request.Request(f"http://localhost:{external_model_port}/v1/models")
        with urllib.request.urlopen(req, timeout=1.5) as response:
            data = json.loads(response.read().decode('utf-8'))
            # Handle standard OpenAI /v1/models format
            if "data" in data:
                remote_models = [m["id"] for m in data["data"] if isinstance(m, dict) and "id" in m]
            elif "models" in data:
                remote_models = [m["name"] for m in data["models"] if isinstance(m, dict) and "name" in m]
    except Exception:
        # Remote model runner not active or unreachable
        pass

    # Combine local and remote models into a single indexed list
    all_choices = []
    local_choices_count = len(local_models)
    
    for m in local_models:
        all_choices.append({"type": "local", "name": m})
    for m in remote_models:
        # Avoid duplicate listing if same name is remote and local
        if m not in local_models:
            all_choices.append({"type": "remote", "name": m})

    if not all_choices:
        print(f"\n\033[33mNo GGUF models found in the './models' directory, and no remote model server was detected on localhost:{external_model_port}.\033[0m")
        print("\nTo download a model, you can place a .gguf file inside the './models/' directory.")
        print("Alternatively, start your SSH tunnel to a remote model server first:")
        print(f"  ssh -L {external_model_port}:localhost:8080 user@remote-machine")
        print()
        sys.exit(0)

    # 5. Present menu
    print("\nAvailable Models:")
    print("-" * 60)
    
    if local_models:
        print(" Local Models (in ./models):")
        for idx, item in enumerate(all_choices[:local_choices_count], start=1):
            is_current = " \033[32m(currently selected)\033[0m" if item["name"] == current_model else ""
            print(f"  [{idx}] {item['name']}{is_current}")
            
    if remote_models:
        if local_models:
            print()
        print(f" Remote Models (via localhost:{external_model_port}):")
        for idx, item in enumerate(all_choices[local_choices_count:], start=local_choices_count + 1):
            is_current = " \033[32m(currently selected)\033[0m" if item["name"] == current_model else ""
            print(f"  [{idx}] {item['name']}{is_current} \033[34m[Remote Proxy]\033[0m")
            
    print("-" * 60)

    if current_model and current_model not in [item["name"] for item in all_choices]:
        print(f"Current model from .env: '{current_model}' (not found locally or remotely)")

    # 6. Read choice
    while True:
        try:
            choice = input(f"Select a model (1-{len(all_choices)}) [or press Enter to cancel]: ").strip()
            if not choice:
                print("Cancelled. No changes made.")
                return
            choice_idx = int(choice) - 1
            if 0 <= choice_idx < len(all_choices):
                selected_item = all_choices[choice_idx]
                selected_model = selected_item["name"]
                is_selected_remote = selected_item["type"] == "remote"
                break
            else:
                print(f"Please enter a number between 1 and {len(all_choices)}.")
        except ValueError:
            print("Invalid input. Please enter a valid number.")

    # 7. Update .env
    print(f"\nSetting model to: {selected_model}")
    
    updated = False
    new_lines = []
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            
        for line in lines:
            if line.strip().startswith('MODEL_FILE='):
                new_lines.append(f"MODEL_FILE={selected_model}\n")
                updated = True
            else:
                new_lines.append(line)
                
        if not updated:
            new_lines.append(f"\n# Added by pick.py\nMODEL_FILE={selected_model}\n")

        with open(env_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
            
        print(f"\033[32mSuccessfully updated .env! MODEL_FILE is now set to '{selected_model}'.\033[0m")
    except Exception as e:
        print(f"Error updating .env: {e}", file=sys.stderr)
        sys.exit(1)

    # 8. Update/Create agent configurations (.pi-data/agent/)
    if not agent_dir.exists():
        print(f"Creating missing agent data directory: {agent_dir}")
        agent_dir.mkdir(parents=True, exist_ok=True)

    # Update models.json
    try:
        if models_json_path.exists():
            with open(models_json_path, 'r', encoding='utf-8') as f:
                models_data = json.load(f)
        else:
            models_data = {}

        # Set/update the structure
        providers = models_data.setdefault("providers", {})
        llama_cpp = providers.setdefault("llama-cpp", {
            "baseUrl": "http://local-model-runner:8080/v1",
            "api": "openai-completions",
            "apiKey": "none"
        })
        llama_cpp["models"] = [{"id": selected_model}]

        with open(models_json_path, 'w', encoding='utf-8') as f:
            json.dump(models_data, f, indent=2)
        print(f"\033[32mSuccessfully updated models config: {models_json_path.relative_to(base_dir)}\033[0m")
    except Exception as e:
        print(f"Warning: Could not update models.json: {e}", file=sys.stderr)

    # Update settings.json
    try:
        if settings_json_path.exists():
            with open(settings_json_path, 'r', encoding='utf-8') as f:
                settings_data = json.load(f)
        else:
            settings_data = {}

        settings_data["defaultProvider"] = "llama-cpp"
        settings_data["defaultModel"] = selected_model
        settings_data.setdefault("autocompleteMaxVisible", 7)
        settings_data.setdefault("defaultThinkingLevel", "off")

        with open(settings_json_path, 'w', encoding='utf-8') as f:
            json.dump(settings_data, f, indent=2)
        print(f"\033[32mSuccessfully updated settings config: {settings_json_path.relative_to(base_dir)}\033[0m")
    except Exception as e:
        print(f"Warning: Could not update settings.json: {e}", file=sys.stderr)

    # Check if UFW is active and selected model is remote
    if is_selected_remote:
        try:
            import subprocess
            ufw_status = subprocess.check_output(["systemctl", "is-active", "ufw"], stderr=subprocess.DEVNULL).decode().strip()
            if ufw_status == "active":
                print("\n\033[33m[Notice] UFW firewall is active on your host.\033[0m")
                print("If the agent container gets connection errors, you may need to run this on your host:")
                print(f"  \033[36msudo ufw allow from 172.16.0.0/12 to any port {external_model_port}\033[0m")
        except Exception:
            pass
        print()

if __name__ == '__main__':
    main()
