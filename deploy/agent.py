"""zorc deploy agent -- hand it a git repo, it decides how to deploy it."""
import json
import re
import secrets
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import yaml

from config import config

ZORC_DIR = Path(__file__).parent.parent
REGISTRY_PATH = ZORC_DIR / "registry.yaml"
SECRETS = Path(__file__).parent / "secrets"
COOLIFY_TOKEN_PATH = SECRETS / "coolify.json"
COOLIFY_URL = config.coolify_url

RESOURCE_MAP_PATH = Path(__file__).parent / "resource_map.json"


def _load_resource_map() -> dict:
    if RESOURCE_MAP_PATH.exists():
        return json.loads(RESOURCE_MAP_PATH.read_text())
    return {}


def record_resource(name: str, *, kind: str, coolify_uuid: str | None = None,
                     domains: list[str] | None = None, coolify_postgres_uuid: str | None = None,
                     container_name: str | None = None, postgres_container_name: str | None = None,
                     node: str | None = None) -> None:
    """kind: 'coolify' | 'coolify-service' | 'pages' | 'zorc-agent'."""
    m = _load_resource_map()
    entry = {"kind": kind, "coolify_uuid": coolify_uuid, "domains": domains or []}
    if coolify_postgres_uuid:
        entry["coolify_postgres_uuid"] = coolify_postgres_uuid
    if container_name:
        entry["container_name"] = container_name
    if postgres_container_name:
        entry["postgres_container_name"] = postgres_container_name
    if node:
        entry["node"] = node
    m[name] = entry
    RESOURCE_MAP_PATH.write_text(json.dumps(m, indent=2))

COOLIFY_PROJECT_UUID = config.coolify_project_uuid
COOLIFY_ENVIRONMENT_NAME = config.coolify_environment_name
COOLIFY_ENVIRONMENT_UUID = config.coolify_environment_uuid
PLATFORM_ROOT_DOMAIN = config.root_domain

CLOUDFLARE_TOKEN_PATH = SECRETS / "cloudflare.json"
CLOUDFLARE_ACCOUNT_ID = config.cloudflare_account_id
CLOUDFLARE_ZONE_ID = config.cloudflare_zone_id
CLOUDFLARE_TUNNEL_ID = config.cloudflare_tunnel_id
CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"

FRAMEWORK_MEMORY_MB = {
    "static": 0,
    "node": 384,
    "python": 384,
    "go": 256,
    "dockerfile": 384,
}

BACKEND_MANIFESTS = {"package.json", "requirements.txt", "pyproject.toml", "go.mod", "Cargo.toml", "Gemfile"}
FRONTEND_ONLY_DEPS = ("vite", "next", "react-scripts", "@angular/core", "svelte", "astro")


def _coolify_headers() -> dict:
    token = json.loads(COOLIFY_TOKEN_PATH.read_text())["token"]
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _cloudflare_token() -> str:
    return json.loads(CLOUDFLARE_TOKEN_PATH.read_text())["token"]


def _cloudflare_headers() -> dict:
    return {"Authorization": f"Bearer {_cloudflare_token()}", "Content-Type": "application/json"}


def create_dns_record(subdomain: str, target: str | None = None, record_type: str = "CNAME") -> None:
    """<subdomain>.<root_domain> -> target."""
    hostname = f"{subdomain}.{PLATFORM_ROOT_DOMAIN}"
    target = target or f"{CLOUDFLARE_TUNNEL_ID}.cfargotunnel.com"
    with httpx.Client(timeout=15) as client:
        existing = client.get(
            f"{CLOUDFLARE_API}/zones/{CLOUDFLARE_ZONE_ID}/dns_records",
            headers=_cloudflare_headers(), params={"name": hostname},
        )
        existing.raise_for_status()
        if existing.json()["result"]:
            return  # already exists, idempotent
        r = client.post(
            f"{CLOUDFLARE_API}/zones/{CLOUDFLARE_ZONE_ID}/dns_records",
            headers=_cloudflare_headers(),
            json={
                "type": record_type,
                "name": hostname,
                "content": target,
                "proxied": True,
            },
        )
        r.raise_for_status()


def deploy_to_pages(*, project_name: str, repo_dir: Path, build_command: str | None) -> str:
    """Creates the Pages project if needed, builds (if there's a build
    command), and uploads via wrangler (handles Cloudflare's content-hash
    upload protocol correctly -- not worth hand-rolling)."""
    with httpx.Client(timeout=20) as client:
        r = client.get(
            f"{CLOUDFLARE_API}/accounts/{CLOUDFLARE_ACCOUNT_ID}/pages/projects/{project_name}",
            headers=_cloudflare_headers(),
        )
        if r.status_code == 404:
            create = client.post(
                f"{CLOUDFLARE_API}/accounts/{CLOUDFLARE_ACCOUNT_ID}/pages/projects",
                headers=_cloudflare_headers(),
                json={"name": project_name, "production_branch": "main"},
            )
            create.raise_for_status()

    publish_dir = repo_dir
    if build_command:
        subprocess.run(build_command, shell=True, cwd=repo_dir, check=True, capture_output=True, timeout=180)
        for candidate in ("dist", "build", "public", "out"):
            if (repo_dir / candidate).is_dir():
                publish_dir = repo_dir / candidate
                break

    wrangler_home = Path(tempfile.mkdtemp(prefix="wrangler-home-"))
    env = {"CLOUDFLARE_API_TOKEN": _cloudflare_token(), "CLOUDFLARE_ACCOUNT_ID": CLOUDFLARE_ACCOUNT_ID,
           "PATH": "/usr/bin:/usr/local/bin", "HOME": str(wrangler_home)}
    result = subprocess.run(
        ["wrangler", "pages", "deploy", str(publish_dir), "--project-name", project_name,
         "--branch", "main", "--commit-dirty=true"],
        env=env, cwd=str(wrangler_home), check=True, capture_output=True, text=True, timeout=180,
    )
    match = re.search(r"https://[a-z0-9.-]+\.pages\.dev", result.stdout)
    return match.group(0) if match else f"https://{project_name}.pages.dev"


def add_pages_custom_domain(project_name: str, domain: str) -> None:
    with httpx.Client(timeout=15) as client:
        r = client.post(
            f"{CLOUDFLARE_API}/accounts/{CLOUDFLARE_ACCOUNT_ID}/pages/projects/{project_name}/domains",
            headers=_cloudflare_headers(), json={"name": domain},
        )
        if r.status_code == 200:
            return
        already_attached = any(e.get("code") == 8000018 for e in r.json().get("errors", []))
        if r.status_code == 400 and already_attached:
            return
        r.raise_for_status()


def load_registry() -> dict:
    reg = yaml.safe_load(REGISTRY_PATH.read_text()) or {}
    # A bare "apps:" line with nothing under it (the state of a freshly
    # generated registry.yaml, before the first app exists) parses as
    # None, not []. Normalize here once so every .get("apps", []) call
    # elsewhere actually gets a list, not a crash.
    if reg.get("apps") is None:
        reg["apps"] = []
    return reg


def node_config(node_name: str) -> dict:
    """Raises KeyError with the valid options listed if node_name isn't a"""
    reg = load_registry()
    nodes = reg["nodes"]
    if node_name not in nodes:
        raise KeyError(f"{node_name!r} is not a known node (valid: {sorted(nodes)})")
    return nodes[node_name]


LOCAL_NODE = config.local_node
REMOTE_DEPLOY_KEY = SECRETS / "node_bootstrap_key"


def _remote_host_memory_mb(tailscale_ip: str, ssh_key: Path = REMOTE_DEPLOY_KEY, user: str = "root") -> tuple[float, float]:
    """Same as _host_memory_mb() (defined further down) but for a node
    this process isn't running on, over SSH."""
    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
         "-i", str(ssh_key), f"{user}@{tailscale_ip}", "cat", "/proc/meminfo"],
        capture_output=True, text=True, timeout=15, check=True,
    )
    info = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("MemTotal:", "MemAvailable:"):
            info[parts[0]] = int(parts[1]) / 1024  # kB -> MB
    return info.get("MemTotal:", 0.0), info.get("MemAvailable:", 0.0)


def live_headroom_mb(node_name: str) -> float:
    """Real, right-now available memory on the target node -- independent"""
    node = node_config(node_name)
    if node_name == LOCAL_NODE:
        _, available_mb = _host_memory_mb()
    else:
        tailscale_ip = node.get("tailscale_ip")
        if not tailscale_ip:
            raise RuntimeError(f"{node_name!r} has no tailscale_ip in registry.yaml -- cannot live-check it")
        if node.get("backend") == "zorc-agent":
            ssh_key = ZORC_DIR / node["ssh_key"]
            user = node.get("ssh_user", "root")
            _, available_mb = _remote_host_memory_mb(tailscale_ip, ssh_key, user)
        else:
            _, available_mb = _remote_host_memory_mb(tailscale_ip)
    return available_mb


def _probe_hardware_over_ssh(tailscale_ip: str, ssh_key: Path, user: str = "root") -> dict:
    """Runs the same arch/cpu/ram/power/accelerator detection"""
    import inspect
    import sys as _sys
    monitoring_dir = str(ZORC_DIR / "monitoring")
    if monitoring_dir not in _sys.path:
        _sys.path.insert(0, monitoring_dir)
    import checks  # deploy/ and monitoring/ import each other; deferred so this only pays the circular-import cost when actually called

    funcs_src = "\n\n".join(
        inspect.getsource(getattr(checks, fn_name))
        for fn_name in ("_run", "_detect_accelerator", "_detect_power_source", "_detect_cpu")
    )
    probe_script = f'''
import json, os, subprocess, platform
from pathlib import Path

{funcs_src}

def _meminfo_total_mb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    return None

print(json.dumps({{
    "arch": platform.machine(),
    "accelerator": _detect_accelerator(),
    "cpu": _detect_cpu(),
    "ram_mb": _meminfo_total_mb(),
    "power": _detect_power_source(),
}}))
'''
    proc = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
         "-i", str(ssh_key), f"{user}@{tailscale_ip}", "python3", "-"],
        input=probe_script, capture_output=True, text=True, timeout=20,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"remote probe on {tailscale_ip!r} failed: {(proc.stderr or proc.stdout)[-500:]}")
    return json.loads(proc.stdout)


def _ssh_run(tailscale_ip: str, ssh_key: Path, remote_cmd: list[str], user: str = "root",
              timeout: int = 15) -> tuple[int, str, str]:
    """One-off remote command, same connection conventions as"""
    proc = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
         "-i", str(ssh_key), f"{user}@{tailscale_ip}", *remote_cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def remote_node_probe(node_name: str) -> dict:
    """_probe_hardware_over_ssh() for an already-registered node -- looks"""
    node = node_config(node_name)
    tailscale_ip = node.get("tailscale_ip")
    if not tailscale_ip:
        raise RuntimeError(f"{node_name!r} has no tailscale_ip in registry.yaml -- cannot probe it remotely")
    if node.get("backend") == "zorc-agent":
        ssh_key = ZORC_DIR / node["ssh_key"]
        user = node.get("ssh_user", "root")
        return _probe_hardware_over_ssh(tailscale_ip, ssh_key, user)
    return _probe_hardware_over_ssh(tailscale_ip, REMOTE_DEPLOY_KEY)


CANDIDATES_PATH = ZORC_DIR / "nodes" / "candidates.yaml"


def load_candidates() -> list[dict]:
    if not CANDIDATES_PATH.exists():
        return []
    return (yaml.safe_load(CANDIDATES_PATH.read_text()) or {}).get("candidates", [])


def propose_node(hostname: str) -> dict:
    """Read-only capability report for a node NOT yet in registry.yaml --"""
    match = next((c for c in load_candidates() if c["hostname"] == hostname), None)
    if not match:
        known = [c["hostname"] for c in load_candidates()]
        return {
            "status": "refused",
            "reason": (
                f"{hostname!r} is not in nodes/candidates.yaml -- propose_node() only inspects "
                f"pre-approved candidates, never an arbitrary host a caller names. "
                f"Known candidates: {known or '(none yet)'}. A human needs to add it there first."
            ),
        }

    tailscale_ip = match["tailscale_ip"]
    ssh_key = ZORC_DIR / match["ssh_key"]
    user = match.get("user", "root")

    try:
        hardware = _probe_hardware_over_ssh(tailscale_ip, ssh_key, user)
    except Exception as e:
        return {"status": "unreachable", "hostname": hostname, "reason": str(e)}

    docker_rc, _, _ = _ssh_run(tailscale_ip, ssh_key, ["docker", "--version"], user)
    coolify_rc, _, _ = _ssh_run(tailscale_ip, ssh_key, ["test", "-d", "/data/coolify"], user)
    has_docker = docker_rc == 0
    has_coolify = coolify_rc == 0
    coolify_check_note = (
        None if user == "root" else
        "checked as a non-root user -- a false negative is possible if /data/coolify exists "
        "but isn't readable by this account"
    )

    if has_docker and has_coolify:
        suggested_backend = "coolify"
    elif has_docker:
        suggested_backend = "zorc-agent"  # not yet implemented -- see registry.yaml's node schema comment
    else:
        suggested_backend = None  # neither present; needs a human decision, not a guess

    return {
        "status": "ready_for_review",
        "hostname": hostname,
        "hardware": hardware,
        "has_docker": has_docker,
        "has_coolify": has_coolify,
        "coolify_check_note": coolify_check_note,
        "suggested_backend": suggested_backend,
        "suggested_is_control_plane": False,  # never auto-suggest true -- control-plane trust is always a deliberate human call
        "note": (
            "Read-only report. Nothing was staged or installed on this host. To actually onboard: run "
            "the appropriate process yourself (bootstrap/*.sh for a new control-plane-capable node, or "
            "Coolify's own server-add flow for a lighter worker node), then add this node to "
            "registry.yaml's nodes section yourself."
        ),
    }


def budget_headroom_mb(node_name: str = LOCAL_NODE) -> float:
    reg = load_registry()
    node = node_config(node_name)
    ceiling = node["usable_mb"] * node["max_utilisation"]
    allocated = sum(
        a.get("memory_mb", 0) for a in reg.get("apps", []) if a.get("target") == node_name
    )
    return ceiling - allocated


def name_taken(name: str) -> bool:
    reg = load_registry()
    return any(a["name"] == name for a in reg.get("apps", []))


def owner_memory_total_mb(owner: str) -> int:
    """Sum of memory_mb across every app this owner currently has"""
    reg = load_registry()
    return sum(a.get("memory_mb", 0) for a in reg.get("apps", []) if a.get("owner") == owner)


def owner_budget_mb(owner: str) -> int:
    """This owner's soft memory cap, platform-wide -- registry.yaml's
    owner_budgets.overrides[owner] if set to a real number, else
    owner_budgets.default_mb."""
    reg = load_registry()
    budgets = reg.get("owner_budgets") or {}
    overrides = budgets.get("overrides") or {}
    override = overrides.get(owner)
    return override if override is not None else budgets.get("default_mb", 8192)


def clone_repo(owner_repo: str, git_branch: str = "main") -> Path:
    """owner_repo like 'someorg/some-app'."""
    workdir = Path(tempfile.mkdtemp(prefix="deploy-"))
    subprocess.run(
        ["gh", "repo", "clone", owner_repo, str(workdir), "--", "--depth", "1", "--branch", git_branch],
        check=True, capture_output=True, timeout=60,
    )
    return workdir


def classify(repo_dir: Path) -> dict:
    """No LLM: deterministic detection, same order Nixpacks uses internally."""
    files = {p.name for p in repo_dir.iterdir() if p.is_file()}
    has_backend_manifest = bool(BACKEND_MANIFESTS & files)
    has_dockerfile = "Dockerfile" in files
    has_index_html = "index.html" in files

    if has_index_html and not has_backend_manifest and not has_dockerfile:
        return {"kind": "static", "language": None, "memory_mb": 0,
                "reason": "index.html with no backend manifest — static site"}

    if "package.json" in files:
        try:
            pkg = json.loads((repo_dir / "package.json").read_text())
        except (json.JSONDecodeError, OSError):
            pkg = {}
        scripts = pkg.get("scripts", {})
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        looks_frontend_only = any(fw in deps for fw in FRONTEND_ONLY_DEPS) and "start" not in scripts
        if looks_frontend_only and "build" in scripts:
            return {"kind": "static", "language": "node", "memory_mb": 0,
                    "build_command": scripts["build"],
                    "reason": "frontend build tool present, no start script — static site"}

        result = {"kind": "app", "language": "node", "memory_mb": FRAMEWORK_MEMORY_MB["node"],
                  "reason": "package.json with a server script"}

        has_frontend_tooling = any(fw in deps for fw in FRONTEND_ONLY_DEPS)
        if has_frontend_tooling and "start" in scripts:
            result["start_command"] = "npm start"
            if "build" in scripts:
                result["build_command"] = "npm run build"
            result["reason"] += (" (frontend build tool + server script both present -- explicit "
                                  "build/start commands set to avoid Nixpacks static-site misdetection)")
        return result

    if "requirements.txt" in files or "pyproject.toml" in files:
        return {"kind": "app", "language": "python", "memory_mb": FRAMEWORK_MEMORY_MB["python"],
                "reason": "python manifest present"}

    if "go.mod" in files:
        return {"kind": "app", "language": "go", "memory_mb": FRAMEWORK_MEMORY_MB["go"],
                "reason": "go.mod present"}

    if has_dockerfile:
        return {"kind": "app", "language": "dockerfile", "memory_mb": FRAMEWORK_MEMORY_MB["dockerfile"],
                "reason": "Dockerfile present, no other manifest recognized"}

    return {"kind": "unknown", "language": None, "memory_mb": None,
            "reason": "no recognizable manifest — needs a human decision"}


_ENV_GENERATE_STRATEGIES = {"hex": lambda: secrets.token_hex(32)}


def parse_app_yaml(repo_dir: Path) -> dict:
    """Reads app.yaml's optional env: section and database: flag."""
    app_yaml_path = repo_dir / "app.yaml"
    if not app_yaml_path.exists():
        return {"env": {}, "database": False, "persistent_storage": None}
    try:
        parsed = yaml.safe_load(app_yaml_path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"app.yaml is not valid YAML: {e}")
    env = parsed.get("env") or {}
    for key, spec in env.items():
        if not isinstance(spec, dict) or ("generate" not in spec and "required" not in spec):
            raise ValueError(
                f"app.yaml env.{key} must be either {{generate: hex}} or {{required: true}}, got {spec!r}"
            )
        if "generate" in spec and spec["generate"] not in _ENV_GENERATE_STRATEGIES:
            raise ValueError(
                f"app.yaml env.{key}.generate={spec['generate']!r} is not supported "
                f"(supported: {sorted(_ENV_GENERATE_STRATEGIES)})"
            )
    database = parsed.get("database", False)
    if not isinstance(database, bool):
        raise ValueError(f"app.yaml database must be true or false, got {database!r}")
    persistent_storage = parsed.get("persistent_storage")
    if persistent_storage is not None:
        if not isinstance(persistent_storage, dict) or "mount_path" not in persistent_storage:
            raise ValueError(
                f"app.yaml persistent_storage must be {{mount_path: /some/path}}, got {persistent_storage!r}"
            )
    return {"env": env, "database": database, "persistent_storage": persistent_storage}


def resolve_env_vars(repo_dir: Path, env_overrides: dict[str, str] | None) -> tuple[dict[str, str], list[str]]:
    """Cross-references app.yaml's env: section against env_overrides
    (values the caller already has, e.g."""
    env_overrides = env_overrides or {}
    declared = parse_app_yaml(repo_dir)["env"]
    vars_to_set: dict[str, str] = {}
    missing_required: list[str] = []
    for key, spec in declared.items():
        if "generate" in spec:
            vars_to_set[key] = _ENV_GENERATE_STRATEGIES[spec["generate"]]()
        elif key in env_overrides:
            vars_to_set[key] = env_overrides[key]
        else:
            missing_required.append(key)
    return vars_to_set, missing_required


DEDICATED_POSTGRES_MEMORY_MB = 150


def _provision_postgres_coolify(app_name: str, node: dict, target_node: str,
                                 image: str = "postgres:18-alpine") -> tuple[str, list[str]]:
    """Backend-specific head #1: creates a Coolify-managed dedicated
    Postgres, waits (bounded, 90s) for it to report healthy."""
    with httpx.Client(timeout=30) as client:
        r = client.post(f"{COOLIFY_URL}/databases/postgresql", headers=_coolify_headers(), json={
            "project_uuid": COOLIFY_PROJECT_UUID,
            "server_uuid": node["server_uuid"],
            "environment_name": COOLIFY_ENVIRONMENT_NAME,
            "name": f"{app_name}-postgres",
            "image": image,
        })
        r.raise_for_status()
        db_uuid = r.json()["uuid"]

        r = client.get(f"{COOLIFY_URL}/databases/{db_uuid}/start", headers=_coolify_headers())
        r.raise_for_status()

    deadline = time.time() + 90
    healthy = False
    while time.time() < deadline:
        with httpx.Client(timeout=15) as client:
            r = client.get(f"{COOLIFY_URL}/databases/{db_uuid}", headers=_coolify_headers())
            r.raise_for_status()
            if r.json().get("status") == "running:healthy":
                healthy = True
                break
        time.sleep(5)
    if not healthy:
        raise RuntimeError(f"dedicated postgres {db_uuid} for {app_name!r} did not become healthy within 90s")

    container_name = db_uuid
    if target_node == LOCAL_NODE:
        exec_prefix = ["docker", "exec", "-i", container_name, "psql", "-U", "postgres"]
    else:
        tailscale_ip = node.get("tailscale_ip")
        if not tailscale_ip:
            raise RuntimeError("node has no tailscale_ip in registry.yaml -- cannot reach it")
        exec_prefix = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
                        "-i", str(REMOTE_DEPLOY_KEY), f"root@{tailscale_ip}",
                        "docker", "exec", "-i", container_name, "psql", "-U", "postgres"]
    return container_name, exec_prefix


def _provision_postgres_zorc_agent(app_name: str, node: dict,
                                    image: str = "postgres:18-alpine") -> tuple[str, list[str]]:
    """Backend-specific head #2: a direct `docker run` on the zorc-agent
    network -- no Coolify API involved at all, matches everything else
    this backend does."""
    tailscale_ip = node.get("tailscale_ip")
    if not tailscale_ip:
        raise RuntimeError("node has no tailscale_ip in registry.yaml -- cannot reach it")
    ssh_key = ZORC_DIR / node["ssh_key"]
    user = node.get("ssh_user", "root")

    _zorc_agent_ensure_network(tailscale_ip, ssh_key, user)
    container_name = f"{app_name}-postgres"
    superuser_password = secrets.token_hex(24)
    rc, out, err = _ssh_run(
        tailscale_ip, ssh_key,
        ["docker", "run", "-d", "--name", container_name, "--network", ZORC_AGENT_NETWORK,
         "--restart", "unless-stopped", "--label", "managed-by=zorc", "--label", f"zorc-app={app_name}",
         "-e", f"POSTGRES_PASSWORD={superuser_password}", image],
        user, timeout=30,
    )
    if rc != 0:
        raise RuntimeError(f"failed to start dedicated postgres for {app_name!r}: {err[-500:]}")
    container_id = out.strip()
    _zorc_agent_wait_healthy(tailscale_ip, ssh_key, user, container_id, timeout_sec=60)

    ready_deadline = time.time() + 30
    ready = False
    while time.time() < ready_deadline:
        rc, _, _ = _ssh_run(tailscale_ip, ssh_key,
                             ["docker", "exec", container_name, "pg_isready", "-U", "postgres"], user)
        if rc == 0:
            ready = True
            break
        time.sleep(2)
    if not ready:
        raise RuntimeError(f"dedicated postgres for {app_name!r} did not become ready within 30s")

    exec_prefix = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
                    "-i", str(ssh_key), f"{user}@{tailscale_ip}",
                    "docker", "exec", "-i", container_name, "psql", "-U", "postgres"]
    return container_name, exec_prefix


def provision_dedicated_postgres(app_name: str, target_node: str,
                                  image: str = "postgres:18-alpine",
                                  post_create_sql: str | None = None) -> tuple[str, str]:
    """Creates a new, single-app-dedicated Postgres instance on target_node"""
    node = node_config(target_node)
    if node.get("backend") == "zorc-agent":
        container_name, exec_prefix = _provision_postgres_zorc_agent(app_name, node, image=image)
    else:
        container_name, exec_prefix = _provision_postgres_coolify(app_name, node, target_node, image=image)

    db_role = re.sub(r"[^a-z0-9_]", "_", app_name.lower())
    db_password = secrets.token_hex(24)
    sql = f"CREATE ROLE {db_role} WITH LOGIN PASSWORD '{db_password}'; CREATE DATABASE {db_role} OWNER {db_role};"
    if post_create_sql:
        sql += f"\n\\c {db_role}\n{post_create_sql}"

    proc = subprocess.run(exec_prefix, input=sql, capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        raise RuntimeError(f"failed to create role/database for {app_name!r}: {proc.stderr[-500:]}")

    database_url = f"postgres://{db_role}:{db_password}@{container_name}:5432/{db_role}"
    return container_name, database_url


def check_deploy_budget(name: str, memory_mb: int, node_name: str = LOCAL_NODE) -> tuple[bool, str]:
    if name_taken(name):
        return False, f"'{name}' is already registered in registry.yaml"
    headroom = budget_headroom_mb(node_name)
    if memory_mb > headroom:
        return False, (f"needs {memory_mb} MB but only {headroom:.0f} MB of headroom left on "
                        f"{node_name} — does not fit without retiring something or using the other node")
    return True, f"fits on {node_name} — {headroom:.0f} MB headroom, {memory_mb} MB requested"


def build_pack_for(language: str) -> str:
    return "dockerfile" if language == "dockerfile" else "nixpacks"


def create_coolify_app(*, name: str, git_repository: str, git_branch: str,
                        build_pack: str, memory_mb: int, domain: str, server_uuid: str,
                        instant_deploy: bool = True, install_command: str | None = None,
                        build_command: str | None = None, start_command: str | None = None) -> dict:
    """instant_deploy=False when the app declares env vars that must be set"""
    payload = {
        "project_uuid": COOLIFY_PROJECT_UUID,
        "server_uuid": server_uuid,
        "environment_name": COOLIFY_ENVIRONMENT_NAME,
        "git_repository": git_repository,
        "git_branch": git_branch,
        "build_pack": build_pack,
        "name": name,
        "domains": f"https://{domain}",
        "ports_exposes": "8080",  # AGENTS.md's app contract convention
        "limits_memory": f"{memory_mb}m",
        "is_auto_deploy_enabled": True,
        "is_force_https_enabled": True,
        "health_check_enabled": True,
        "health_check_path": "/health",
        "instant_deploy": instant_deploy,
    }
    if install_command:
        payload["install_command"] = install_command
    if build_command:
        payload["build_command"] = build_command
    if start_command:
        payload["start_command"] = start_command
    with httpx.Client(timeout=30) as client:
        r = client.post(f"{COOLIFY_URL}/applications/public", headers=_coolify_headers(), json=payload)
        r.raise_for_status()
        return r.json()


def set_coolify_env_vars(coolify_uuid: str, vars_to_set: dict[str, str]) -> None:
    """Upserts each {key: value} as a runtime env var on the given
    application. Coolify 409s a POST for a key that already exists (must
    PATCH instead), and keeps a separate preview-environment copy of
    every var -- POST creates both copies from one call, PATCH only
    touches the is_preview value in the request, so an update issues two
    PATCH calls or the copies drift apart."""
    with httpx.Client(timeout=30) as client:
        existing = client.get(f"{COOLIFY_URL}/applications/{coolify_uuid}/envs", headers=_coolify_headers())
        existing.raise_for_status()
        existing_keys = {e["key"] for e in existing.json()}
        for key, value in vars_to_set.items():
            if key in existing_keys:
                for is_preview in (False, True):
                    r = client.patch(
                        f"{COOLIFY_URL}/applications/{coolify_uuid}/envs",
                        headers=_coolify_headers(),
                        json={"key": key, "value": value, "is_preview": is_preview},
                    )
                    r.raise_for_status()
            else:
                r = client.post(
                    f"{COOLIFY_URL}/applications/{coolify_uuid}/envs",
                    headers=_coolify_headers(),
                    json={"key": key, "value": value, "is_preview": False},
                )
                r.raise_for_status()


def add_coolify_persistent_storage(coolify_uuid: str, name: str, mount_path: str) -> None:
    """Attaches a Coolify-managed named volume to the application, mounted"""
    with httpx.Client(timeout=30) as client:
        r = client.post(
            f"{COOLIFY_URL}/applications/{coolify_uuid}/storages",
            headers=_coolify_headers(),
            json={"name": name, "mount_path": mount_path, "type": "persistent"},
        )
        r.raise_for_status()


def trigger_coolify_deploy(coolify_uuid: str) -> None:
    """Explicitly starts the first real build+deploy -- the counterpart to
    create_coolify_app(instant_deploy=False)."""
    with httpx.Client(timeout=30) as client:
        r = client.get(f"{COOLIFY_URL}/deploy", headers=_coolify_headers(), params={"uuid": coolify_uuid})
        r.raise_for_status()



ZORC_AGENT_NETWORK = "zorc-agent"
ZORC_AGENT_CONTAINER_PORT = 8080  # same "apps listen on 8080" convention as the Coolify path
ZORC_AGENT_PORT_RANGE = (20000, 20100)


def _zorc_agent_ensure_network(tailscale_ip: str, ssh_key: Path, user: str) -> None:
    """Idempotent."""
    rc, _, _ = _ssh_run(tailscale_ip, ssh_key, ["docker", "network", "inspect", ZORC_AGENT_NETWORK], user)
    if rc == 0:
        return
    create_rc, _, err = _ssh_run(tailscale_ip, ssh_key, ["docker", "network", "create", ZORC_AGENT_NETWORK], user)
    if create_rc != 0:
        raise RuntimeError(f"failed to create {ZORC_AGENT_NETWORK!r} network: {err[-500:]}")


def _zorc_agent_allocate_port(tailscale_ip: str, ssh_key: Path, user: str) -> int:
    """Live-probes for the first free port in ZORC_AGENT_PORT_RANGE rather
    than trusting a static claimed-list -- this node's other 40+ services
    are unknown to zorc, so a stale table would drift immediately."""
    rc, out, err = _ssh_run(tailscale_ip, ssh_key, ["ss", "-Htln"], user)
    if rc != 0:
        rc, out, err = _ssh_run(tailscale_ip, ssh_key, ["netstat", "-tln"], user)
        if rc != 0:
            raise RuntimeError(f"could not list listening ports on {tailscale_ip!r} "
                                f"(neither ss nor netstat available): {err[-300:]}")

    used_ports = set()
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        local_addr = cols[3]  # State Recv-Q Send-Q <Local Address:Port> Peer...
        if ":" in local_addr:
            port_str = local_addr.rsplit(":", 1)[-1]
            if port_str.isdigit():
                used_ports.add(int(port_str))

    for port in range(*ZORC_AGENT_PORT_RANGE):
        if port not in used_ports:
            return port
    raise RuntimeError(f"no free port in {ZORC_AGENT_PORT_RANGE[0]}-{ZORC_AGENT_PORT_RANGE[1]} on {tailscale_ip!r}")


def _zorc_agent_upload_repo(tailscale_ip: str, ssh_key: Path, user: str, repo_dir: Path, remote_dir: str) -> None:
    """Copies repo_dir's contents to remote_dir on the target via a tar"""
    mkdir_rc, _, mkdir_err = _ssh_run(tailscale_ip, ssh_key, ["mkdir", "-p", remote_dir], user)
    if mkdir_rc != 0:
        raise RuntimeError(f"failed to create {remote_dir!r} on target: {mkdir_err[-300:]}")

    tar_proc = subprocess.Popen(["tar", "czf", "-", "-C", str(repo_dir), "."], stdout=subprocess.PIPE)
    try:
        ssh_proc = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
             "-i", str(ssh_key), f"{user}@{tailscale_ip}", "tar", "xzf", "-", "-C", remote_dir],
            stdin=tar_proc.stdout, capture_output=True, text=True, timeout=120,
        )
    finally:
        tar_proc.stdout.close()
        tar_proc.wait()
    if ssh_proc.returncode != 0:
        raise RuntimeError(f"failed to upload repo to {remote_dir!r}: {(ssh_proc.stderr or ssh_proc.stdout)[-500:]}")


def _zorc_agent_build_image(tailscale_ip: str, ssh_key: Path, user: str, remote_dir: str, image_tag: str) -> None:
    rc, _, err = _ssh_run(tailscale_ip, ssh_key, ["docker", "build", "-t", image_tag, remote_dir],
                           user, timeout=600)
    if rc != 0:
        raise RuntimeError(f"docker build failed: {err[-1500:]}")


def _zorc_agent_preflight_gpu(tailscale_ip: str, ssh_key: Path, user: str) -> None:
    """Checked before attempting --gpus all, so a missing nvidia runtime
    surfaces as a clear rejection here rather than an opaque docker error
    after the image is already built."""
    rc, out, _ = _ssh_run(tailscale_ip, ssh_key, ["docker", "info"], user)
    runtimes_line = next((line for line in out.splitlines() if line.strip().startswith("Runtimes:")), "")
    if rc != 0 or "nvidia" not in runtimes_line:
        raise RuntimeError(
            f"needs_gpu=True but {tailscale_ip!r} has no 'nvidia' Docker runtime configured "
            f"(docker info Runtimes line: {runtimes_line.strip() or '(unavailable)'})"
        )


def _zorc_agent_run_container(tailscale_ip: str, ssh_key: Path, user: str, *, name: str, image_tag: str,
                               port: int, memory_mb: int, env_vars: dict[str, str], needs_gpu: bool,
                               gpu_legacy_runtime: bool = False, gpu_cdi: bool = False,
                               volumes: list[str] | None = None) -> str:
    """Starts the container, labeled for safe rollback identification."""
    cmd = [
        "docker", "run", "-d",
        "--name", name,
        "--network", ZORC_AGENT_NETWORK,
        "--memory", f"{memory_mb}m",
        "--restart", "unless-stopped",
        "--label", "managed-by=zorc",
        "--label", f"zorc-app={name}",
        "-p", f"{port}:{ZORC_AGENT_CONTAINER_PORT}",
    ]
    for v in (volumes or []):
        cmd += ["-v", v]
    if needs_gpu:
        if gpu_legacy_runtime:
            cmd += ["--runtime", "nvidia",
                    "-e", "NVIDIA_VISIBLE_DEVICES=all", "-e", "NVIDIA_DRIVER_CAPABILITIES=all"]
        elif gpu_cdi:
            cmd += ["--device", "nvidia.com/gpu=all"]
        else:
            cmd += ["--gpus", "all"]
        cmd += ["--shm-size", "16g"]
    for key, value in env_vars.items():
        cmd += ["-e", f"{key}={value}"]
    cmd.append(image_tag)

    rc, out, err = _ssh_run(tailscale_ip, ssh_key, cmd, user, timeout=30)
    if rc != 0:
        raise RuntimeError(f"docker run failed: {err[-500:]}")
    return out.strip()


def _zorc_agent_inspect(tailscale_ip: str, ssh_key: Path, user: str, container: str) -> dict | None:
    """`docker inspect` with no -f, JSON parsed locally, rather than a Go"""
    rc, out, _ = _ssh_run(tailscale_ip, ssh_key, ["docker", "inspect", container], user)
    if rc != 0:
        return None
    try:
        parsed = json.loads(out)
        return parsed[0] if parsed else None
    except (json.JSONDecodeError, IndexError):
        return None


def _zorc_agent_wait_healthy(tailscale_ip: str, ssh_key: Path, user: str, container_id: str,
                              timeout_sec: int = 30) -> None:
    """Bounded wait confirming the container is still running after
    startup, not immediately crash-looped -- without this, "deploy
    succeeded" could mean nothing more than "the container was created."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        info = _zorc_agent_inspect(tailscale_ip, ssh_key, user, container_id)
        status = ((info or {}).get("State") or {}).get("Status")
        if status == "running":
            return
        if status in ("exited", "dead"):
            _, logs, _ = _ssh_run(tailscale_ip, ssh_key, ["docker", "logs", "--tail", "50", container_id], user)
            raise RuntimeError(f"container exited immediately after start (status={status}): {logs[-1000:]}")
        time.sleep(2)
    raise RuntimeError(f"container did not reach 'running' state within {timeout_sec}s")


def _zorc_agent_rollback(tailscale_ip: str, ssh_key: Path, user: str, container_names: list[str]) -> list[dict]:
    """Best-effort teardown of zorc-created containers on a later-step
    failure."""
    results = []
    for cname in container_names:
        info = _zorc_agent_inspect(tailscale_ip, ssh_key, user, cname)
        labels = ((info or {}).get("Config") or {}).get("Labels") or {}
        if labels.get("managed-by") != "zorc":
            results.append({"container": cname, "ok": False,
                             "error": "not found, or missing managed-by=zorc label -- refused to touch"})
            continue
        _ssh_run(tailscale_ip, ssh_key, ["docker", "stop", cname], user, timeout=20)
        rm_rc, _, rm_err = _ssh_run(tailscale_ip, ssh_key, ["docker", "rm", "-f", cname], user, timeout=20)
        results.append({"container": cname, "ok": rm_rc == 0, "error": None if rm_rc == 0 else rm_err[-300:]})
    return results


def register_app(*, name: str, memory_mb: int, subdomain: str, repo: str, owner: str, target: str = LOCAL_NODE,
                  database: bool = False, redis: bool = False, critical: bool = False) -> None:
    """Appends a new entry to registry.yaml and commits it."""
    if not owner:
        raise ValueError("register_app() requires a non-empty owner -- refusing to create another unowned entry")
    if name_taken(name):
        return  # idempotent -- a repeated deploy of the same app shouldn't double-register it
    text = REGISTRY_PATH.read_text()
    entry = f"""
  - name: {name}
    target: {target}
    memory_mb: {memory_mb}
    subdomain: {subdomain}
    database: {"true" if database else "null"}
    redis_db: null
    storage_prefix: null
    repo: "{repo}"
    owner: "{owner}"
    critical: {"true" if critical else "false"}
    depends_on: []
"""
    marker = "# Applications. Add new entries at the end. Keep alphabetical within groups.\n# ---------------------------------------------------------------------------\napps:"
    if marker not in text:
        raise RuntimeError("registry.yaml marker not found — format changed, update register_app()")
    text = text.replace(marker, marker + entry, 1)
    REGISTRY_PATH.write_text(text)


def add_tunnel_route(hostname: str, service: str = "https://localhost:443") -> None:
    """Every Coolify-managed app routes through the same Traefik hop --
    Traefik dispatches to the right container by Host() header, using the
    domains we set on the Coolify app resource."""
    config_path = Path("/etc/cloudflared/config.yml")
    repo_config_path = ZORC_DIR / "cloudflared" / "config.yml"

    live_cfg = yaml.safe_load(config_path.read_text())
    if any(r.get("hostname") == hostname for r in live_cfg["ingress"]):
        return  # genuinely already serving this route -- nothing to do

    cfg = yaml.safe_load(repo_config_path.read_text())
    if not any(r.get("hostname") == hostname for r in cfg["ingress"]):
        new_rule = {"hostname": hostname, "service": service}
        if service.startswith("https://"):
            new_rule["originRequest"] = {"noTLSVerify": True}
        cfg["ingress"].insert(-1, new_rule)  # keep the catch-all 404 rule last
        repo_config_path.write_text(yaml.dump(cfg, sort_keys=False))
    subprocess.run(["cp", str(repo_config_path), str(config_path)], check=True)
    subprocess.run(["systemctl", "reload-or-restart", "cloudflared"], check=True)


def git_commit_and_push(message: str) -> None:
    subprocess.run(["git", "-C", str(ZORC_DIR), "add", "registry.yaml", "cloudflared/config.yml"], check=True)
    subprocess.run(["git", "-C", str(ZORC_DIR), "commit", "-m", message], check=True)
    subprocess.run(["git", "-C", str(ZORC_DIR), "push", "origin", "main"], check=True)


def _deploy_zorc_agent(*, step, log: list, node: dict, target_node: str, name: str, owner_repo: str, owner: str,
                        repo_dir: Path, classification: dict, memory_mb: int, env_vars_to_set: dict[str, str],
                        needs_gpu: bool, needs_database: bool, postgres_container_name: str | None,
                        domain: str, volumes: list[str] | None = None) -> dict:
    """The zorc-agent equivalent of deploy()'s Coolify branch below --
    everything from here down runs entirely over SSH against a non-root
    Docker daemon, no Coolify API involved."""
    tailscale_ip = node["tailscale_ip"]
    ssh_key = ZORC_DIR / node["ssh_key"]
    user = node.get("ssh_user", "root")

    step("ensure_network", _zorc_agent_ensure_network, tailscale_ip, ssh_key, user)
    port = step("allocate_port", _zorc_agent_allocate_port, tailscale_ip, ssh_key, user)

    remote_dir = f"/home/{user}/zorc-agent-apps/{name}"
    step("upload_repo", _zorc_agent_upload_repo, tailscale_ip, ssh_key, user, repo_dir, remote_dir)

    image_tag = f"zorc-{name}:latest"
    step("build_image", _zorc_agent_build_image, tailscale_ip, ssh_key, user, remote_dir, image_tag)

    if needs_gpu:
        step("preflight_gpu", _zorc_agent_preflight_gpu, tailscale_ip, ssh_key, user)

    container_id = step(
        "run_container", _zorc_agent_run_container, tailscale_ip, ssh_key, user,
        name=name, image_tag=image_tag, port=port, memory_mb=memory_mb,
        env_vars=env_vars_to_set, needs_gpu=needs_gpu,
        gpu_legacy_runtime=node.get("gpu_runtime") == "legacy",
        gpu_cdi=node.get("gpu_runtime") == "cdi", volumes=volumes,
    )

    try:
        step("wait_healthy", _zorc_agent_wait_healthy, tailscale_ip, ssh_key, user, container_id)

        service_url = f"http://{tailscale_ip}:{port}"
        step("create_dns_record", create_dns_record, name)
        step("add_tunnel_route", add_tunnel_route, domain, service=service_url)

        step("register_app", register_app, name=name, memory_mb=memory_mb, subdomain=name,
             repo=f"github.com/{owner_repo}", owner=owner, target=target_node, database=needs_database)
        step("record_resource", record_resource, name, kind="zorc-agent", container_name=name,
             postgres_container_name=postgres_container_name, node=target_node)
        step("commit_and_push", git_commit_and_push, f"registry: add {name} (deployed via zorc-agent)")
    except DeployError:
        rollback_targets = [name]
        if postgres_container_name:
            rollback_targets.append(postgres_container_name)
        rollback_results = _zorc_agent_rollback(tailscale_ip, ssh_key, user, rollback_targets)
        log.append({"step": "rollback_zorc_agent", "ok": all(r["ok"] for r in rollback_results),
                    "detail": rollback_results})
        raise

    return {
        "log": log,
        "classification": classification,
        "status": "deployed",
        "domain": domain,
        "target_node": target_node,
        "container_name": name,
        "postgres_container_name": postgres_container_name,
        "message": f"{name} created on {target_node} (zorc-agent) at port {port}, routed at "
                    f"https://{domain} via Cloudflare Tunnel, registered in registry.yaml.",
    }


class DeployError(Exception):
    def __init__(self, step: str, reason: str):
        self.step = step
        self.reason = reason
        super().__init__(f"{step}: {reason}")


def deploy(*, owner_repo: str, name: str, owner: str, git_branch: str = "main", target_node: str = LOCAL_NODE,
           memory_mb_override: int | None = None, env_overrides: dict[str, str] | None = None,
           needs_gpu: bool = False, volumes: list[str] | None = None) -> dict:
    """Full pipeline: clone -> classify -> budget check -> live resource
    check -> either Cloudflare Pages (static) or Coolify (real app), DNS +
    registration either way."""
    log = []
    node = node_config(target_node)  # raises KeyError immediately on a bad name

    if needs_gpu and node.get("backend") != "zorc-agent":
        raise DeployError(
            "gpu_backend_check",
            f"{target_node!r} has backend={node.get('backend')!r} -- GPU passthrough is only implemented "
            "for backend: zorc-agent nodes, regardless of what hardware the target reports",
        )

    def step(name_, fn, *a, **kw):
        try:
            result = fn(*a, **kw)
            log.append({"step": name_, "ok": True})
            return result
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or e.stdout or str(e))
            if isinstance(detail, bytes):
                detail = detail.decode(errors="replace")
            detail = detail.strip()[-1500:]  # tail -- most useful part of a CLI error is usually the end
            log.append({"step": name_, "ok": False, "error": detail})
            raise DeployError(name_, detail) from e
        except Exception as e:
            log.append({"step": name_, "ok": False, "error": str(e)})
            raise DeployError(name_, str(e)) from e

    repo_dir = step("clone", clone_repo, owner_repo, git_branch)
    classification = step("classify", classify, repo_dir)

    if classification["kind"] == "unknown":
        raise DeployError("classify", classification["reason"] + " — cannot proceed automatically")

    if node.get("backend") == "zorc-agent" and not (repo_dir / "Dockerfile").exists():
        raise DeployError(
            "backend_build_check",
            f"{target_node!r} (backend: zorc-agent) only supports repos with a Dockerfile at root -- "
            "none found. No buildpack auto-detection on this backend; add a Dockerfile or target a "
            "backend: coolify node instead.",
        )

    needs_database = classification["kind"] != "static" and parse_app_yaml(repo_dir).get("database", False)
    persistent_storage = parse_app_yaml(repo_dir).get("persistent_storage") if classification["kind"] != "static" else None

    memory_mb = memory_mb_override if memory_mb_override is not None else classification["memory_mb"]
    if needs_database:
        memory_mb += DEDICATED_POSTGRES_MEMORY_MB

    ok, reason = step("budget_check", check_deploy_budget, name, memory_mb, target_node)
    if not ok:
        raise DeployError("budget_check", reason)

    domain = f"{name}.{PLATFORM_ROOT_DOMAIN}"

    if classification["kind"] == "static":
        pages_url = step("deploy_to_pages", deploy_to_pages, project_name=name,
                          repo_dir=repo_dir, build_command=classification.get("build_command"))
        step("add_pages_custom_domain", add_pages_custom_domain, name, domain)
        step("create_dns_record", create_dns_record, name, f"{name}.pages.dev")
        step("register_app", register_app, name=name, memory_mb=0, subdomain=name,
             repo=f"github.com/{owner_repo}", owner=owner, target="pages")
        step("record_resource", record_resource, name, kind="pages")
        step("commit_and_push", git_commit_and_push, f"registry: add {name} (static, via deploy agent)")
        return {
            "log": log, "classification": classification, "status": "deployed",
            "domain": domain, "pages_url": pages_url,
            "message": f"{name} deployed to Cloudflare Pages (zero node memory used), "
                       f"custom domain https://{domain} attached, registered in registry.yaml.",
        }

    build_pack = build_pack_for(classification["language"])

    env_vars_to_set, missing_required_env = step("resolve_env_vars", resolve_env_vars, repo_dir, env_overrides)
    if missing_required_env:
        raise DeployError(
            "resolve_env_vars",
            f"app.yaml requires {missing_required_env} but no value was supplied for "
            f"{'it' if len(missing_required_env) == 1 else 'them'} -- pass via deploy()'s env_overrides "
            "(these are tied to an external account/service, zorc cannot generate them itself)",
        )

    postgres_uuid = None
    if needs_database:
        postgres_uuid, database_url = step("provision_database", provision_dedicated_postgres, name, target_node)
        env_vars_to_set["DATABASE_URL"] = database_url

    def _check_live_headroom():
        live = live_headroom_mb(target_node)
        if memory_mb > live:
            raise RuntimeError(
                f"needs {memory_mb} MB but only {live:.0f} MB is actually free on "
                f"{target_node} right now (static budget said this would fit -- "
                f"something is using more memory than registry.yaml accounts for)"
            )
    step("live_resource_check", _check_live_headroom)

    if node.get("backend") == "zorc-agent":
        return _deploy_zorc_agent(
            step=step, log=log, node=node, target_node=target_node, name=name, owner_repo=owner_repo,
            owner=owner, repo_dir=repo_dir, classification=classification, memory_mb=memory_mb,
            env_vars_to_set=env_vars_to_set, needs_gpu=needs_gpu, needs_database=needs_database,
            postgres_container_name=postgres_uuid, domain=domain, volumes=volumes,
        )

    coolify_result = step(
        "create_coolify_app", create_coolify_app,
        name=name, git_repository=f"https://github.com/{owner_repo}",
        git_branch=git_branch, build_pack=build_pack, memory_mb=memory_mb, domain=domain,
        server_uuid=node["server_uuid"], instant_deploy=not env_vars_to_set and not persistent_storage,
        build_command=classification.get("build_command"),
        start_command=classification.get("start_command"),
    )

    try:
        if env_vars_to_set or persistent_storage:
            if env_vars_to_set:
                step("set_env_vars", set_coolify_env_vars, coolify_result["uuid"], env_vars_to_set)
            if persistent_storage:
                step("add_persistent_storage", add_coolify_persistent_storage, coolify_result["uuid"],
                     name=f"{name}-data", mount_path=persistent_storage["mount_path"])
            step("trigger_coolify_deploy", trigger_coolify_deploy, coolify_result["uuid"])

        if node.get("has_public_ip"):
            step("create_dns_record", create_dns_record, name, target=node["ip"], record_type="A")
        else:
            step("create_dns_record", create_dns_record, name)
            step("add_tunnel_route", add_tunnel_route, domain)

        step("register_app", register_app, name=name, memory_mb=memory_mb, subdomain=name,
             repo=f"github.com/{owner_repo}", owner=owner, target=target_node, database=needs_database)
        step("record_resource", record_resource, name, kind="coolify", coolify_uuid=coolify_result.get("uuid"),
             coolify_postgres_uuid=postgres_uuid)
        step("commit_and_push", git_commit_and_push, f"registry: add {name} (deployed via deploy agent)")
    except DeployError:
        coolify_uuid = coolify_result.get("uuid")
        try:
            if coolify_uuid:
                with httpx.Client(timeout=30) as client:
                    r = client.delete(f"{COOLIFY_URL}/applications/{coolify_uuid}", headers=_coolify_headers())
                    if r.status_code not in (200, 404):
                        log.append({"step": "rollback_coolify_app", "ok": False,
                                    "error": f"cleanup returned HTTP {r.status_code}"})
                    else:
                        log.append({"step": "rollback_coolify_app", "ok": True})
        except Exception as cleanup_err:
            log.append({"step": "rollback_coolify_app", "ok": False, "error": str(cleanup_err)})
        if postgres_uuid:
            try:
                with httpx.Client(timeout=30) as client:
                    r = client.delete(f"{COOLIFY_URL}/databases/{postgres_uuid}", headers=_coolify_headers())
                    if r.status_code not in (200, 404):
                        log.append({"step": "rollback_postgres", "ok": False,
                                    "error": f"cleanup returned HTTP {r.status_code}"})
                    else:
                        log.append({"step": "rollback_postgres", "ok": True})
            except Exception as cleanup_err:
                log.append({"step": "rollback_postgres", "ok": False, "error": str(cleanup_err)})
        raise

    return {
        "log": log,
        "classification": classification,
        "status": "deployed",
        "domain": domain,
        "target_node": target_node,
        "coolify_uuid": coolify_result.get("uuid"),
        "coolify_postgres_uuid": postgres_uuid,
        "message": f"{name} created in Coolify on {target_node}, routed at https://{domain}, "
                    f"registered in registry.yaml. First build is running in Coolify now.",
    }



def _find_coolify_uuid(name: str) -> str | None:
    """Prefer the recorded mapping; fall back to a name-prefix search
    against Coolify's own application list for apps deployed before this
    mapping existed (e.g."""
    mapped = _load_resource_map().get(name)
    if mapped and mapped.get("coolify_uuid"):
        return mapped["coolify_uuid"]
    with httpx.Client(timeout=15) as client:
        r = client.get(f"{COOLIFY_URL}/applications", headers=_coolify_headers())
        r.raise_for_status()
        candidates = [a for a in r.json() if a.get("name", "").startswith(name)]
    candidates.sort(key=lambda a: 0 if str(a.get("status", "")).startswith("running") else 1)
    return candidates[0]["uuid"] if candidates else None


_MEM_UNIT_TO_MB = {"b": 1 / (1024 * 1024), "kib": 1 / 1024, "mib": 1, "gib": 1024, "tib": 1024 * 1024}


def _parse_mem_to_mb(s: str) -> float:
    """docker stats formats sizes like '86.2MiB', '1.2GiB', '512kB'."""
    m = re.match(r"([\d.]+)\s*([a-zA-Z]+)", s.strip())
    if not m:
        return 0.0
    value, unit = float(m.group(1)), m.group(2).lower().rstrip("b") + "b"
    return value * _MEM_UNIT_TO_MB.get(unit, 1.0)


def _docker_stats() -> list[dict]:
    """One live snapshot of every running container's CPU/memory."""
    result = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
        capture_output=True, text=True, timeout=15, check=True,
    )
    out = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        d = json.loads(line)
        used_str, limit_str = (d.get("MemUsage") or "0 / 0").split(" / ")
        out.append({
            "name": d.get("Name", ""),
            "cpu_percent": float((d.get("CPUPerc") or "0%").rstrip("%") or 0),
            "mem_used_mb": _parse_mem_to_mb(used_str),
            "mem_limit_mb": _parse_mem_to_mb(limit_str),
        })
    return out


def _docker_stats_remote(tailscale_ip: str, ssh_key: Path, user: str) -> list[dict]:
    """Same as _docker_stats() but for a zorc-agent node this process
    isn't running on, over SSH -- the live CPU/memory counterpart to
    _zorc_agent_inspect's container-state view."""
    code, out, err = _ssh_run(tailscale_ip, ssh_key, ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                               user=user, timeout=15)
    if code != 0:
        return []
    result = []
    for line in out.strip().splitlines():
        if not line:
            continue
        d = json.loads(line)
        used_str, limit_str = (d.get("MemUsage") or "0 / 0").split(" / ")
        result.append({
            "name": d.get("Name", ""),
            "cpu_percent": float((d.get("CPUPerc") or "0%").rstrip("%") or 0),
            "mem_used_mb": _parse_mem_to_mb(used_str),
            "mem_limit_mb": _parse_mem_to_mb(limit_str),
        })
    return result


def _host_memory_mb() -> tuple[float, float]:
    """(total, available) in MB."""
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("MemTotal:", "MemAvailable:"):
            info[parts[0]] = int(parts[1]) / 1024  # kB -> MB
    return info.get("MemTotal:", 0.0), info.get("MemAvailable:", 0.0)


def app_resources(name: str) -> dict:
    """Live CPU/memory usage for one app, summed across all of its
    containers (a coolify-service has several; a coolify app has one)."""
    mapped = _load_resource_map().get(name, {})
    kind = mapped.get("kind")
    if kind == "pages":
        return {"kind": "pages", "cpu_percent": 0, "mem_used_mb": 0, "containers": 0}
    if kind == "zorc-agent":
        target_node = mapped.get("node")
        container_name = mapped.get("container_name") or name
        if not target_node:
            return {"kind": "zorc-agent", "cpu_percent": 0, "mem_used_mb": 0, "containers": 0}
        node = node_config(target_node)
        stats = [s for s in _docker_stats_remote(node["tailscale_ip"], ZORC_DIR / node["ssh_key"],
                                                  node.get("ssh_user", "root"))
                 if s["name"] == container_name]
        return {
            "kind": "zorc-agent",
            "cpu_percent": round(sum(s["cpu_percent"] for s in stats), 1),
            "mem_used_mb": round(sum(s["mem_used_mb"] for s in stats), 1),
            "containers": len(stats),
        }
    coolify_uuid = mapped.get("coolify_uuid") or _find_coolify_uuid(name)
    if not coolify_uuid:
        return {"kind": kind, "cpu_percent": 0, "mem_used_mb": 0, "containers": 0}
    stats = [s for s in _docker_stats() if coolify_uuid in s["name"]]
    return {
        "kind": kind,
        "cpu_percent": round(sum(s["cpu_percent"] for s in stats), 1),
        "mem_used_mb": round(sum(s["mem_used_mb"] for s in stats), 1),
        "containers": len(stats),
    }


_PLATFORM_CONTAINER_PREFIXES = (
    "coolify", "coolify-db", "coolify-redis", "coolify-proxy",
    "coolify-realtime", "coolify-sentinel",
)


def resource_overview() -> dict:
    """Host-wide memory picture for the /apps pie chart: total RAM, how"""
    stats = _docker_stats()
    resource_map = _load_resource_map()
    reg_apps = {a["name"] for a in load_registry().get("apps", [])}

    slices = []
    attributed_mb = 0.0
    matched_container_names = set()

    for name in reg_apps:
        mapped = resource_map.get(name, {})
        coolify_uuid = mapped.get("coolify_uuid") or _find_coolify_uuid(name)
        if not coolify_uuid:
            slices.append({"name": name, "mem_used_mb": 0.0})
            continue
        app_stats = [s for s in stats if coolify_uuid in s["name"]]
        used = sum(s["mem_used_mb"] for s in app_stats)
        matched_container_names.update(s["name"] for s in app_stats)
        slices.append({"name": name, "mem_used_mb": round(used, 1)})
        attributed_mb += used

    platform_stats = [s for s in stats if s["name"] not in matched_container_names
                       and any(s["name"].startswith(p) for p in _PLATFORM_CONTAINER_PREFIXES)]
    platform_mb = sum(s["mem_used_mb"] for s in platform_stats)
    matched_container_names.update(s["name"] for s in platform_stats)
    slices.append({"name": "platform (Coolify)", "mem_used_mb": round(platform_mb, 1)})
    attributed_mb += platform_mb

    other_stats = [s for s in stats if s["name"] not in matched_container_names]
    other_mb = sum(s["mem_used_mb"] for s in other_stats)
    slices.append({"name": "other containers", "mem_used_mb": round(other_mb, 1)})
    attributed_mb += other_mb

    total_mb, available_mb = _host_memory_mb()
    real_used_mb = max(0.0, total_mb - available_mb)  # everything actually in use, containers + OS
    os_mb = max(0.0, real_used_mb - attributed_mb)
    slices.append({"name": "OS / system", "mem_used_mb": round(os_mb, 1)})
    slices.append({"name": "free", "mem_used_mb": round(available_mb, 1)})

    return {
        "total_mb": round(total_mb, 1),
        "used_mb": round(real_used_mb, 1),
        "available_mb": round(available_mb, 1),
        "slices": [s for s in slices if s["mem_used_mb"] > 0],
    }


def list_apps() -> list[dict]:
    reg = load_registry()
    resource_map = _load_resource_map()
    out = []
    for a in reg.get("apps", []):
        entry = {
            "name": a["name"], "target": a.get("target", "node"),
            "memory_mb": a.get("memory_mb", 0), "subdomain": a.get("subdomain"),
            "repo": a.get("repo"), "kind": resource_map.get(a["name"], {}).get("kind"),
        }
        if not entry["kind"]:
            entry["kind"] = "pages" if entry["target"] == "pages" else "coolify"
        out.append(entry)
    return out


def app_status(name: str) -> dict:
    reg_entry = next((a for a in load_registry().get("apps", []) if a["name"] == name), None)
    if not reg_entry:
        raise ValueError(f"{name} is not registered")
    kind = _load_resource_map().get(name, {}).get("kind") or \
        ("pages" if reg_entry.get("target") == "pages" else "coolify")

    if kind == "pages":
        with httpx.Client(timeout=15) as client:
            r = client.get(
                f"{CLOUDFLARE_API}/accounts/{CLOUDFLARE_ACCOUNT_ID}/pages/projects/{name}",
                headers=_cloudflare_headers(),
            )
            r.raise_for_status()
            proj = r.json()["result"]
        deployment = proj.get("latest_deployment") or {}
        return {
            "kind": "pages", "name": name,
            "status": (deployment.get("latest_stage") or {}).get("status", "unknown"),
            "url": deployment.get("url"),
            "domains": proj.get("domains", []),
            "memory_mb": 0,
        }

    if kind == "zorc-agent":
        mapped = _load_resource_map().get(name, {})
        target_node = mapped.get("node")
        container_name = mapped.get("container_name") or name
        if not target_node:
            return {"kind": "zorc-agent", "name": name, "status": "not_found",
                    "memory_mb": reg_entry.get("memory_mb", 0)}
        node = node_config(target_node)
        info = _zorc_agent_inspect(node["tailscale_ip"], ZORC_DIR / node["ssh_key"],
                                    node.get("ssh_user", "root"), container_name)
        if info is None:
            return {"kind": "zorc-agent", "name": name, "status": "not_found",
                    "node": target_node, "memory_mb": reg_entry.get("memory_mb", 0)}
        state = info.get("State") or {}
        usage = app_resources(name)
        return {
            "kind": "zorc-agent", "name": name, "node": target_node, "container": container_name,
            "status": state.get("Status", "unknown"),
            "restart_count": info.get("RestartCount", 0),
            "started_at": state.get("StartedAt"),
            "exit_code": state.get("ExitCode") if state.get("Status") != "running" else None,
            "memory_mb": reg_entry.get("memory_mb", 0),
            "cpu_percent": usage["cpu_percent"], "mem_used_mb": usage["mem_used_mb"],
        }

    if kind == "coolify-service":
        service_uuid = _load_resource_map().get(name, {}).get("coolify_uuid")
        if not service_uuid:
            return {"kind": "coolify-service", "name": name, "status": "not_found",
                    "memory_mb": reg_entry.get("memory_mb", 0)}
        with httpx.Client(timeout=15) as client:
            r = client.get(f"{COOLIFY_URL}/services/{service_uuid}", headers=_coolify_headers())
            r.raise_for_status()
            svc = r.json()
        containers = [{"name": a.get("name"), "status": a.get("status", "unknown")}
                      for a in svc.get("applications", []) + svc.get("databases", [])]
        overall = "running:healthy" if containers and all(
            str(c["status"]).startswith("running") for c in containers
        ) else "degraded" if any(str(c["status"]).startswith("running") for c in containers) else "exited"
        usage = app_resources(name)
        return {
            "kind": "coolify-service", "name": name, "coolify_uuid": service_uuid,
            "status": overall, "containers": containers,
            "memory_mb": reg_entry.get("memory_mb", 0),
            "cpu_percent": usage["cpu_percent"], "mem_used_mb": usage["mem_used_mb"],
        }

    coolify_uuid = _find_coolify_uuid(name)
    if not coolify_uuid:
        return {"kind": "coolify", "name": name, "status": "not_found", "memory_mb": reg_entry.get("memory_mb", 0)}
    with httpx.Client(timeout=15) as client:
        r = client.get(f"{COOLIFY_URL}/applications/{coolify_uuid}", headers=_coolify_headers())
        r.raise_for_status()
        app = r.json()
    usage = app_resources(name)
    return {
        "kind": "coolify", "name": name, "coolify_uuid": coolify_uuid,
        "status": app.get("status", "unknown"),
        "memory_mb": reg_entry.get("memory_mb", 0),
        "fqdn": app.get("fqdn"),
        "cpu_percent": usage["cpu_percent"], "mem_used_mb": usage["mem_used_mb"],
    }


def app_logs(name: str, lines: int = 200, since: str | None = None, grep: str | None = None) -> str:
    """Recent logs for an existing, already-deployed app -- read-only, no
    side effects."""
    kind = _load_resource_map().get(name, {}).get("kind")
    if kind == "zorc-agent":
        mapped = _load_resource_map().get(name, {})
        target_node = mapped.get("node")
        container_name = mapped.get("container_name") or name
        if not target_node:
            return f"(no recorded node for zorc-agent app {name!r} -- cannot fetch logs)"
        node = node_config(target_node)
        cmd = ["docker", "logs", "--tail", str(lines)]
        if since:
            cmd += ["--since", since]
        cmd.append(container_name)
        code, out, err = _ssh_run(node["tailscale_ip"], ZORC_DIR / node["ssh_key"], cmd,
                                   user=node.get("ssh_user", "root"), timeout=20)
        logs = out if code == 0 else f"(docker logs failed: {(err or out).strip()[-500:]})"
    elif kind == "coolify-service":
        logs = ("(this is a multi-container service -- open it in Coolify directly "
                "to see per-container logs, e.g. wordpress/db/typesense/n8n each "
                "have their own log stream)")
    else:
        coolify_uuid = _find_coolify_uuid(name)
        if not coolify_uuid:
            logs = "(no Coolify resource found -- static/Pages apps don't have server logs here; check the Cloudflare Pages dashboard)"
        else:
            with httpx.Client(timeout=20) as client:
                r = client.get(
                    f"{COOLIFY_URL}/applications/{coolify_uuid}/logs",
                    headers=_coolify_headers(), params={"lines": lines},
                )
                r.raise_for_status()
                logs = r.json().get("logs", "")

    if grep and logs and not logs.startswith("("):
        matched = [line for line in logs.splitlines() if grep.lower() in line.lower()]
        logs = "\n".join(matched) if matched else f"(no lines matched grep={grep!r} out of {len(logs.splitlines())} fetched)"
    return logs


def app_action(name: str, action: str) -> dict:
    """action: start | stop | restart."""
    if action not in ("start", "stop", "restart"):
        raise ValueError(f"unknown action {action!r}")
    mapped = _load_resource_map().get(name, {})
    kind = mapped.get("kind")
    if kind == "zorc-agent":
        target_node = mapped.get("node")
        container_name = mapped.get("container_name") or name
        if not target_node:
            raise ValueError(f"no recorded node for zorc-agent app {name!r} -- cannot {action} it")
        node = node_config(target_node)
        tailscale_ip = node["tailscale_ip"]
        ssh_key = ZORC_DIR / node["ssh_key"]
        user = node.get("ssh_user", "root")
        code, out, err = _ssh_run(tailscale_ip, ssh_key, ["docker", action, container_name], user=user, timeout=30)
        if code != 0:
            raise RuntimeError(f"docker {action} {container_name} on {target_node} failed: "
                                f"{(err or out).strip()[-500:]}")
        return {"action": action, "name": name, "node": target_node, "container": container_name}
    if kind == "coolify-service":
        service_uuid = mapped.get("coolify_uuid")
        if not service_uuid:
            raise ValueError(f"no Coolify service found for {name}")
        with httpx.Client(timeout=30) as client:
            r = client.post(f"{COOLIFY_URL}/services/{service_uuid}/{action}", headers=_coolify_headers())
            r.raise_for_status()
            return r.json()
    coolify_uuid = _find_coolify_uuid(name)
    if not coolify_uuid:
        raise ValueError(f"no Coolify resource found for {name} (static sites can't be start/stopped)")
    with httpx.Client(timeout=30) as client:
        r = client.post(f"{COOLIFY_URL}/applications/{coolify_uuid}/{action}", headers=_coolify_headers())
        r.raise_for_status()
        return r.json()


def redeploy(name: str) -> dict:
    """Re-triggers a build+deploy of an EXISTING, already-registered app,"""
    if not name_taken(name):
        raise ValueError(f"{name!r} is not a registered app -- nothing to redeploy")
    mapped = _load_resource_map().get(name, {})
    kind = mapped.get("kind")
    if kind != "coolify":
        raise ValueError(
            f"{name!r} is a {kind!r} app -- redeploy() only supports single-container Coolify "
            "apps (kind 'coolify') right now; coolify-service, static/Pages, and zorc-agent "
            "apps aren't supported by this tool yet"
        )
    coolify_uuid = mapped.get("coolify_uuid")
    if not coolify_uuid:
        raise ValueError(f"{name!r} has no recorded coolify_uuid in resource_map.json -- cannot redeploy")
    trigger_coolify_deploy(coolify_uuid)
    return {"redeployed": name, "kind": kind, "coolify_uuid": coolify_uuid}


def resize_app_memory(name: str, new_memory_mb: int) -> dict:
    """Applies a new memory_mb limit to an EXISTING, already-registered
    app -- updates Coolify's declared limit, updates registry.yaml, and
    triggers a redeploy so the change actually reaches the running
    container."""
    if not name_taken(name):
        raise ValueError(f"{name!r} is not a registered app -- nothing to resize")
    mapped = _load_resource_map().get(name, {})
    kind = mapped.get("kind")
    if kind != "coolify":
        raise ValueError(
            f"{name!r} is a {kind!r} app -- resize_app_memory() only supports single-container "
            "Coolify apps (kind 'coolify') right now"
        )
    coolify_uuid = mapped.get("coolify_uuid")
    if not coolify_uuid:
        raise ValueError(f"{name!r} has no recorded coolify_uuid in resource_map.json -- cannot resize")

    reg = load_registry()
    app_entry = next((a for a in reg.get("apps", []) if a["name"] == name), None)
    if app_entry is None:
        raise ValueError(f"{name!r} has a resource_map.json entry but no registry.yaml entry -- inconsistent state")
    old_memory_mb = app_entry["memory_mb"]
    target_node = app_entry["target"]

    delta = new_memory_mb - old_memory_mb
    if delta > 0:
        live = live_headroom_mb(target_node)
        if delta > live:
            raise RuntimeError(
                f"{name!r} wants +{delta}MB (from {old_memory_mb}MB to {new_memory_mb}MB) but only "
                f"{live:.0f}MB is actually free on {target_node!r} right now"
            )

    with httpx.Client(timeout=30) as client:
        r = client.patch(f"{COOLIFY_URL}/applications/{coolify_uuid}",
                          headers=_coolify_headers(), json={"limits_memory": f"{new_memory_mb}m"})
        r.raise_for_status()

    text = REGISTRY_PATH.read_text()
    pattern = re.compile(
        r"(- name: " + re.escape(name) + r"\n    target: \S+\n    memory_mb: )" + str(old_memory_mb) + r"\b"
    )
    new_text, n = pattern.subn(r"\g<1>" + str(new_memory_mb), text, count=1)
    if n != 1:
        raise RuntimeError(
            f"could not find {name!r}'s memory_mb: {old_memory_mb} line in registry.yaml in the expected "
            "shape -- refusing to guess at a blind replace; the Coolify limit was already updated above, "
            "so registry.yaml needs a manual fix to match"
        )
    REGISTRY_PATH.write_text(new_text)

    trigger_coolify_deploy(coolify_uuid)

    return {"resized": name, "old_memory_mb": old_memory_mb, "new_memory_mb": new_memory_mb,
            "target_node": target_node, "coolify_uuid": coolify_uuid}


def set_app_env_vars(name: str, env_vars: dict[str, str]) -> dict:
    """Sets or updates env vars on an EXISTING, already-registered app,
    then redeploys so it actually reaches the running container. Not
    restricted to keys app.yaml declares -- an operational tool, not a
    re-run of deploy()'s declared-env resolution. Coolify apps only,
    same scope as redeploy()/resize_app_memory()."""
    if not name_taken(name):
        raise ValueError(f"{name!r} is not a registered app -- nothing to update")
    if not env_vars:
        raise ValueError("env_vars must not be empty")
    mapped = _load_resource_map().get(name, {})
    kind = mapped.get("kind")
    if kind != "coolify":
        raise ValueError(
            f"{name!r} is a {kind!r} app -- set_app_env_vars() only supports single-container "
            "Coolify apps (kind 'coolify') right now"
        )
    coolify_uuid = mapped.get("coolify_uuid")
    if not coolify_uuid:
        raise ValueError(f"{name!r} has no recorded coolify_uuid in resource_map.json -- cannot update")

    set_coolify_env_vars(coolify_uuid, env_vars)
    trigger_coolify_deploy(coolify_uuid)

    return {"updated": name, "keys": sorted(env_vars.keys()), "coolify_uuid": coolify_uuid}


def remove_registry_entry(name: str) -> None:
    text = REGISTRY_PATH.read_text()
    lines = text.split("\n")
    out, i, removed = [], 0, False
    while i < len(lines):
        if lines[i].strip() == f"- name: {name}":
            removed = True
            i += 1
            while i < len(lines) and (lines[i].startswith("    ") or lines[i].strip() == ""):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    if not removed:
        raise ValueError(f"{name} not found in registry.yaml")
    REGISTRY_PATH.write_text("\n".join(out))


def delete_app(name: str) -> dict:
    """Tears down everything the deploy agent created for this app:
    Coolify resource or Pages project, DNS record, tunnel route (Coolify
    only), the resource_map entry, and the registry.yaml entry -- then
    commits."""
    reg_entry = next((a for a in load_registry().get("apps", []) if a["name"] == name), None)
    if not reg_entry:
        raise ValueError(f"{name} is not registered")
    mapped = _load_resource_map().get(name, {})
    kind = mapped.get("kind") or ("pages" if reg_entry.get("target") == "pages" else "coolify")
    hostname = f"{name}.{PLATFORM_ROOT_DOMAIN}"

    if kind == "zorc-agent":
        target_node = mapped.get("node") or reg_entry.get("target")
        node = node_config(target_node)
        tailscale_ip = node["tailscale_ip"]
        ssh_key = ZORC_DIR / node["ssh_key"]
        user = node.get("ssh_user", "root")

        rollback_targets = [mapped.get("container_name") or name]
        if mapped.get("postgres_container_name"):
            rollback_targets.append(mapped["postgres_container_name"])
        rollback_results = _zorc_agent_rollback(tailscale_ip, ssh_key, user, rollback_targets)

        repo_config_path = ZORC_DIR / "cloudflared" / "config.yml"
        cfg = yaml.safe_load(repo_config_path.read_text())
        cfg["ingress"] = [r_ for r_ in cfg["ingress"] if r_.get("hostname") != hostname]
        repo_config_path.write_text(yaml.dump(cfg, sort_keys=False))
        subprocess.run(["cp", str(repo_config_path), "/etc/cloudflared/config.yml"], check=True)
        subprocess.run(["systemctl", "reload-or-restart", "cloudflared"], check=True)

        with httpx.Client(timeout=15) as client:
            existing = client.get(
                f"{CLOUDFLARE_API}/zones/{CLOUDFLARE_ZONE_ID}/dns_records",
                headers=_cloudflare_headers(), params={"name": hostname},
            )
            existing.raise_for_status()
            for rec in existing.json()["result"]:
                client.delete(f"{CLOUDFLARE_API}/zones/{CLOUDFLARE_ZONE_ID}/dns_records/{rec['id']}",
                               headers=_cloudflare_headers())

        m = _load_resource_map()
        m.pop(name, None)
        RESOURCE_MAP_PATH.write_text(json.dumps(m, indent=2))
        remove_registry_entry(name)
        git_commit_and_push(f"registry: remove {name} (deleted via deploy agent)")
        return {"deleted": name, "kind": kind, "rollback": rollback_results}

    if kind == "coolify-service":
        service_uuid = mapped.get("coolify_uuid")
        if service_uuid:
            with httpx.Client(timeout=30) as client:
                r = client.delete(f"{COOLIFY_URL}/services/{service_uuid}", headers=_coolify_headers())
                if r.status_code not in (200, 404):
                    r.raise_for_status()
        domains = mapped.get("domains") or []
        repo_config_path = ZORC_DIR / "cloudflared" / "config.yml"
        cfg = yaml.safe_load(repo_config_path.read_text())
        service_hostnames = {f"{d}.{PLATFORM_ROOT_DOMAIN}" for d in domains}
        cfg["ingress"] = [r_ for r_ in cfg["ingress"] if r_.get("hostname") not in service_hostnames]
        repo_config_path.write_text(yaml.dump(cfg, sort_keys=False))
        subprocess.run(["cp", str(repo_config_path), "/etc/cloudflared/config.yml"], check=True)
        subprocess.run(["systemctl", "reload-or-restart", "cloudflared"], check=True)
        with httpx.Client(timeout=15) as client:
            for d in domains:
                h = f"{d}.{PLATFORM_ROOT_DOMAIN}"
                existing = client.get(
                    f"{CLOUDFLARE_API}/zones/{CLOUDFLARE_ZONE_ID}/dns_records",
                    headers=_cloudflare_headers(), params={"name": h},
                )
                existing.raise_for_status()
                for rec in existing.json()["result"]:
                    client.delete(f"{CLOUDFLARE_API}/zones/{CLOUDFLARE_ZONE_ID}/dns_records/{rec['id']}",
                                   headers=_cloudflare_headers())
        m = _load_resource_map()
        m.pop(name, None)
        RESOURCE_MAP_PATH.write_text(json.dumps(m, indent=2))
        remove_registry_entry(name)
        git_commit_and_push(f"registry: remove {name} (deleted via deploy agent)")
        return {"deleted": name, "kind": kind}

    if kind == "pages":
        with httpx.Client(timeout=20) as client:
            dr = client.delete(
                f"{CLOUDFLARE_API}/accounts/{CLOUDFLARE_ACCOUNT_ID}/pages/projects/{name}/domains/{hostname}",
                headers=_cloudflare_headers(),
            )
            if dr.status_code not in (200, 404):
                dr.raise_for_status()
            r = client.delete(
                f"{CLOUDFLARE_API}/accounts/{CLOUDFLARE_ACCOUNT_ID}/pages/projects/{name}",
                headers=_cloudflare_headers(),
            )
            if r.status_code not in (200, 404):
                r.raise_for_status()
    else:
        coolify_uuid = _find_coolify_uuid(name)
        if coolify_uuid:
            with httpx.Client(timeout=30) as client:
                r = client.delete(f"{COOLIFY_URL}/applications/{coolify_uuid}", headers=_coolify_headers())
                if r.status_code not in (200, 404):
                    r.raise_for_status()
        repo_config_path = ZORC_DIR / "cloudflared" / "config.yml"
        cfg = yaml.safe_load(repo_config_path.read_text())
        cfg["ingress"] = [r_ for r_ in cfg["ingress"] if r_.get("hostname") != hostname]
        repo_config_path.write_text(yaml.dump(cfg, sort_keys=False))
        subprocess.run(["cp", str(repo_config_path), "/etc/cloudflared/config.yml"], check=True)
        subprocess.run(["systemctl", "reload-or-restart", "cloudflared"], check=True)

    with httpx.Client(timeout=15) as client:
        existing = client.get(
            f"{CLOUDFLARE_API}/zones/{CLOUDFLARE_ZONE_ID}/dns_records",
            headers=_cloudflare_headers(), params={"name": hostname},
        )
        existing.raise_for_status()
        for rec in existing.json()["result"]:
            client.delete(f"{CLOUDFLARE_API}/zones/{CLOUDFLARE_ZONE_ID}/dns_records/{rec['id']}",
                           headers=_cloudflare_headers())

    m = _load_resource_map()
    m.pop(name, None)
    RESOURCE_MAP_PATH.write_text(json.dumps(m, indent=2))

    remove_registry_entry(name)
    git_commit_and_push(f"registry: remove {name} (deleted via deploy agent)")
    return {"deleted": name, "kind": kind}
