#!/usr/bin/env python3
"""Interactive setup wizard. Installs and configures the whole stack:
Coolify, cloudflared, and zorc-mcp itself. Linux only -- run as the user
that will own the zorc install, with sudo available for the few steps
that genuinely need root (installing services, writing to /etc).

Safe to re-run: every step checks whether it's already done before
doing it again.

Bootstraps its own virtualenv on first run (this script needs httpx and
PyYAML, which a bare system Python won't have yet) and re-execs itself
inside it -- nothing below this point may import a third-party package
before that happens.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
VENV_PYTHON = VENV / "bin" / "python3"


def _bootstrap_venv() -> None:
    if sys.executable == str(VENV_PYTHON):
        return
    if not VENV.exists():
        print("Setting up a virtualenv for zorc...")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    subprocess.run([str(VENV / "bin" / "pip"), "install", "-q", "-r",
                     str(ROOT / "deploy" / "requirements.txt")], check=True)
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), __file__, *sys.argv[1:]])


_bootstrap_venv()

import json
import platform
import shutil

import httpx
import yaml

SECRETS = ROOT / "deploy" / "secrets"
CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or (default or "")


def confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N]: ").strip().lower() == "y"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, **kw)


def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


def check_linux() -> None:
    if platform.system() != "Linux":
        print("This wizard installs system services and only supports Linux.")
        print("On another OS, follow docs/setup.md by hand instead.")
        sys.exit(1)


def install_coolify() -> str:
    step(1, "Coolify")
    try:
        r = httpx.get("http://localhost:8000/api/v1/servers", timeout=3)
        if r.status_code in (200, 401):
            print("Coolify is already running on localhost:8000.")
            return "http://localhost:8000/api/v1"
    except httpx.RequestError:
        pass

    if not confirm("Coolify not found. Install it now (runs Coolify's official installer)?"):
        print("Install Coolify yourself, then re-run this wizard.")
        sys.exit(1)
    run(["bash", "-c", "curl -fsSL https://cdn.coollabs.io/coolify/install.sh | bash"], check=True)
    print("Coolify installed. Open http://<this-machine>:8000 in a browser and finish the first-run setup"
          " (create your admin account) before continuing.")
    input("Press enter once you've done that: ")
    return "http://localhost:8000/api/v1"


def coolify_token(coolify_url: str) -> tuple[str, dict]:
    step(2, "Coolify API token")
    print("Coolify UI -> Keys & Tokens -> create a token with API access.")
    while True:
        token = ask("Paste the token")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            r = httpx.get(f"{coolify_url}/servers", headers=headers, timeout=10)
            r.raise_for_status()
            return token, headers
        except httpx.HTTPError as e:
            print(f"That didn't work ({e}). Try again.")


def find_local_server(coolify_url: str, headers: dict) -> str:
    r = httpx.get(f"{coolify_url}/servers", headers=headers, timeout=10)
    r.raise_for_status()
    for s in r.json():
        if s.get("is_coolify_host"):
            return s["uuid"]
    raise RuntimeError("no is_coolify_host server found -- Coolify install may be incomplete")


def setup_project(coolify_url: str, headers: dict) -> tuple[str, str]:
    step(3, "Coolify project")
    r = httpx.get(f"{coolify_url}/projects", headers=headers, timeout=10)
    r.raise_for_status()
    projects = r.json()
    proj_uuid = None
    if projects:
        print("Existing projects:")
        for p in projects:
            print(f"  {p['name']}  ({p['uuid']})")
        if confirm(f"Use '{projects[0]['name']}'?"):
            proj_uuid = projects[0]["uuid"]

    if proj_uuid is None:
        try:
            r = httpx.post(f"{coolify_url}/projects", headers=headers,
                            json={"name": "zorc", "description": "created by zorc setup"}, timeout=10)
            r.raise_for_status()
            proj_uuid = r.json()["uuid"]
        except httpx.HTTPError as e:
            print(f"Couldn't auto-create a project ({e}).")
            print("Create one by hand in the Coolify UI, then paste its UUID and its environment's UUID.")
            proj_uuid = ask("Project UUID")
            env_uuid = ask("Environment UUID")
            return proj_uuid, env_uuid

    # The list endpoint above doesn't include environments -- only the
    # per-project detail endpoint does.
    r = httpx.get(f"{coolify_url}/projects/{proj_uuid}", headers=headers, timeout=10)
    r.raise_for_status()
    envs = r.json().get("environments") or []
    if not envs:
        raise RuntimeError(f"project {proj_uuid} has no environments -- check it in the Coolify UI")
    return proj_uuid, envs[0]["uuid"]


def cloudflare_zone(domain: str, token: str) -> tuple[str, str]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = httpx.get(f"{CLOUDFLARE_API}/zones", headers=headers, params={"name": domain}, timeout=10)
    r.raise_for_status()
    result = r.json()["result"]
    if not result:
        raise RuntimeError(f"{domain!r} isn't a zone on this Cloudflare account")
    zone = result[0]
    return zone["id"], zone["account"]["id"]


def setup_cloudflare(domain: str) -> dict:
    step(4, "Cloudflare")
    token = ask("Cloudflare API token (needs Zone:DNS:Edit for your domain)")
    zone_id, account_id = cloudflare_zone(domain, token)
    print(f"Found zone {domain} (zone_id={zone_id[:8]}..., account_id={account_id[:8]}...).")

    SECRETS.mkdir(parents=True, exist_ok=True)
    (SECRETS / "cloudflare.json").write_text(json.dumps({"token": token}))

    if not shutil.which("cloudflared"):
        if confirm("cloudflared isn't installed. Install it now?"):
            run(["bash", "-c",
                 "curl -fsSL https://pkg.cloudflare.com/cloudflared-stable-linux-amd64.deb -o /tmp/cloudflared.deb"
                 " && sudo dpkg -i /tmp/cloudflared.deb"], check=True)
        else:
            print("Install cloudflared yourself, then re-run this wizard.")
            sys.exit(1)

    cert_path = Path.home() / ".cloudflared" / "cert.pem"
    if not cert_path.exists():
        print("Opening a browser to authorize cloudflared against your Cloudflare account...")
        run(["cloudflared", "tunnel", "login"], check=True)

    tunnel_name = ask("Tunnel name", "zorc")
    creds_dir = Path.home() / ".cloudflared"
    existing_names = set()
    if shutil.which("cloudflared"):
        list_out = subprocess.run(["cloudflared", "tunnel", "list"], capture_output=True, text=True)
        existing_names = {line.split()[1] for line in list_out.stdout.splitlines()[1:] if len(line.split()) > 1}

    if tunnel_name in existing_names:
        print(f"Tunnel {tunnel_name!r} already exists, reusing it.")
        list_out = subprocess.run(["cloudflared", "tunnel", "list"], capture_output=True, text=True, check=True)
        tunnel_id = None
        for line in list_out.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) > 1 and parts[1] == tunnel_name:
                tunnel_id = parts[0]
                break
        if not tunnel_id:
            raise RuntimeError(f"found tunnel {tunnel_name!r} in the list but couldn't parse its id")
    else:
        out = run(["cloudflared", "tunnel", "create", tunnel_name], capture_output=True, text=True, check=True)
        tunnel_id = None
        for line in (out.stdout + out.stderr).splitlines():
            if "Created tunnel" in line:
                tunnel_id = line.strip().split()[-1].strip(".")
        if not tunnel_id:
            for f in creds_dir.glob("*.json"):
                tunnel_id = f.stem
        if not tunnel_id:
            raise RuntimeError("tunnel created but couldn't determine its id -- check ~/.cloudflared/")

    return {"token": token, "zone_id": zone_id, "account_id": account_id,
            "tunnel_id": tunnel_id, "tunnel_name": tunnel_name}


def write_cloudflared_config(domain: str, cf: dict) -> None:
    creds_file = Path.home() / ".cloudflared" / f"{cf['tunnel_id']}.json"
    config = {
        "tunnel": cf["tunnel_id"],
        "credentials-file": str(creds_file),
        "ingress": [
            {"hostname": f"mcp.{domain}", "service": "http://localhost:8081"},
            {"service": "http_status:404"},
        ],
    }
    cf_dir = ROOT / "cloudflared"
    cf_dir.mkdir(exist_ok=True)
    (cf_dir / "config.yml").write_text(yaml.dump(config, sort_keys=False))
    print(f"Wrote {cf_dir / 'config.yml'}")

    if confirm("Install cloudflared as a system service using this config (needs sudo)?"):
        run(["sudo", "cp", str(cf_dir / "config.yml"), "/etc/cloudflared/config.yml"], check=True)
        run(["sudo", "cloudflared", "service", "install"], check=False)
        run(["sudo", "systemctl", "restart", "cloudflared"], check=True)


def local_memory_mb() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) // 1024
    return 8000


def write_registry(server_uuid: str) -> None:
    path = ROOT / "registry.yaml"
    if path.exists():
        print(f"{path} already exists, leaving it alone.")
        return
    total = local_memory_mb()
    reserved = min(2000, total // 8)
    usable = total - reserved
    registry = {
        "nodes": {
            "local": {
                "total_memory_mb": total, "reserved_mb": reserved, "usable_mb": usable,
                "max_utilisation": 0.80, "server_uuid": server_uuid, "has_public_ip": False,
                "provider": "self-hosted", "backend": "coolify", "is_control_plane": True,
            }
        },
        "owner_budgets": {"default_mb": 8192, "overrides": {}},
    }
    # apps: is appended as raw text, not part of the dump above -- it must
    # end the file as a bare "apps:" with nothing after it (no [], no
    # blank line) to match register_app()'s exact marker string, which it
    # uses to append new entries by plain text substitution, not by
    # re-parsing and re-dumping the whole file.
    text = yaml.dump(registry, sort_keys=False)
    text += ("\n# ---------------------------------------------------------------------------\n"
             "# Applications. Add new entries at the end. Keep alphabetical within groups.\n"
             "# ---------------------------------------------------------------------------\n"
             "apps:")
    path.write_text(text)
    print(f"Wrote {path} ({total}MB detected on this machine).")


def write_config(domain: str, coolify_url: str, proj_uuid: str, env_uuid: str, cf: dict) -> None:
    path = ROOT / "config.yaml"
    if path.exists() and not confirm(f"{path} already exists. Overwrite?"):
        print(f"Leaving {path} alone.")
        return
    config = {
        "root_domain": domain,
        "coolify_url": coolify_url,
        "coolify_project_uuid": proj_uuid,
        "coolify_environment_uuid": env_uuid,
        "coolify_environment_name": "production",
        "cloudflare_account_id": cf["account_id"],
        "cloudflare_zone_id": cf["zone_id"],
        "cloudflare_tunnel_id": cf["tunnel_id"],
        "local_node": "local",
        "owner_budget_default_mb": 8192,
    }
    path.write_text(yaml.dump(config, sort_keys=False))
    print(f"Wrote {path}")


def install_service() -> None:
    step(6, "zorc-mcp service")
    name = ask("Your name (for the first admin token)", "admin")
    run([str(VENV_PYTHON), str(ROOT / "scripts" / "mint_token.py"), name, "admin"], check=True)

    unit = ROOT / "systemd" / "zorc-mcp.service.example"
    text = unit.read_text()
    text = text.replace("/opt/zorc", str(ROOT)).replace("User=zorc", f"User={Path.home().name}")
    (ROOT / "systemd" / "zorc-mcp.service").write_text(text)

    if confirm("Install and start the zorc-mcp systemd service now (needs sudo)?"):
        run(["sudo", "cp", str(ROOT / "systemd" / "zorc-mcp.service"), "/etc/systemd/system/zorc-mcp.service"],
            check=True)
        run(["sudo", "systemctl", "daemon-reload"], check=True)
        run(["sudo", "systemctl", "enable", "--now", "zorc-mcp"], check=True)
        print("zorc-mcp is running. Check status with: systemctl status zorc-mcp")


def main() -> None:
    check_linux()
    print("zorc setup -- this installs and configures Coolify, cloudflared, and zorc-mcp.\n")

    coolify_url = install_coolify()
    token, headers = coolify_token(coolify_url)
    (SECRETS).mkdir(parents=True, exist_ok=True)
    (SECRETS / "coolify.json").write_text(json.dumps({"token": token}))

    server_uuid = find_local_server(coolify_url, headers)
    proj_uuid, env_uuid = setup_project(coolify_url, headers)

    domain = ask("Domain you'll deploy apps under (must be on Cloudflare)")
    cf = setup_cloudflare(domain)
    write_cloudflared_config(domain, cf)

    step(5, "registry.yaml and config.yaml")
    write_registry(server_uuid)
    write_config(domain, coolify_url, proj_uuid, env_uuid, cf)

    install_service()

    print("\nDone. Point your coding agent's MCP client at:")
    print(f"  https://mcp.{domain}/mcp")
    print("  Authorization: Bearer <the token printed above>")


if __name__ == "__main__":
    main()
