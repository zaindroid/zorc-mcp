"""Loads instance configuration from config.yaml. Fails fast on startup
if required fields are missing rather than falling back to a guess."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ZORC_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("ZORC_CONFIG_PATH", ZORC_DIR / "config.yaml"))

REQUIRED_FIELDS = (
    "root_domain",
    "coolify_url",
    "coolify_project_uuid",
    "coolify_environment_uuid",
    "cloudflare_account_id",
    "cloudflare_zone_id",
    "cloudflare_tunnel_id",
    "local_node",
)


@dataclass(frozen=True)
class Config:
    root_domain: str
    coolify_url: str
    coolify_project_uuid: str
    coolify_environment_uuid: str
    coolify_environment_name: str
    cloudflare_account_id: str
    cloudflare_zone_id: str
    cloudflare_tunnel_id: str
    local_node: str
    owner_budget_default_mb: int


def load() -> Config:
    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"{CONFIG_PATH} not found. Copy config.yaml.example to config.yaml and fill it in, "
            "or run scripts/setup.py."
        )
    raw = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    missing = [f for f in REQUIRED_FIELDS if not raw.get(f)]
    if missing:
        raise RuntimeError(f"config.yaml is missing required field(s): {', '.join(missing)}")
    return Config(
        root_domain=raw["root_domain"],
        coolify_url=raw["coolify_url"].rstrip("/"),
        coolify_project_uuid=raw["coolify_project_uuid"],
        coolify_environment_uuid=raw["coolify_environment_uuid"],
        coolify_environment_name=raw.get("coolify_environment_name", "production"),
        cloudflare_account_id=raw["cloudflare_account_id"],
        cloudflare_zone_id=raw["cloudflare_zone_id"],
        cloudflare_tunnel_id=raw["cloudflare_tunnel_id"],
        local_node=raw["local_node"],
        owner_budget_default_mb=int(raw.get("owner_budget_default_mb", 8192)),
    )


config = load()
