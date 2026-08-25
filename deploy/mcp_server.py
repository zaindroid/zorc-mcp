"""zorc-mcp — a guarded MCP server exposing the deploy agent to any
MCP-capable coding agent, over Streamable HTTP with bearer-token auth."""
import hashlib
import json
import os
import re
import secrets as secrets_module
import shutil
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Literal

import uvicorn
import yaml
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

import agent  # deploy/agent.py, sibling module

MCP_SECRETS = Path(__file__).parent / "secrets"
MCP_TOKEN_PATH = Path(os.environ.get("ZORC_MCP_TOKEN_PATH", str(MCP_SECRETS / "mcp_token.json")))
AUDIT_LOG_PATH = Path(os.environ.get("ZORC_MCP_AUDIT_LOG_PATH", str(Path(__file__).parent / "mcp_audit.log")))
PENDING_ACTIONS_PATH = Path(os.environ.get("ZORC_MCP_PENDING_ACTIONS_PATH",
                                             str(Path(__file__).parent / "pending_actions.json")))

PUBLIC_PATHS = {"/health", "/ready", "/version"}

# Every mutating tool checks its own rate limit through this one
# function -- a single place to disable all of them at once for a given
# deployment (ZORC_MCP_RATE_LIMITS_DISABLED=1) without deleting the
# mechanism itself. Off by default only where explicitly set; the
# framework's own default is enabled.
RATE_LIMITS_ENABLED = os.environ.get("ZORC_MCP_RATE_LIMITS_DISABLED", "").strip().lower() not in ("1", "true", "yes")


def _rate_limited(timestamps: deque, limit: int, window_sec: float) -> bool:
    """True if recording one more call right now would exceed limit
    within the last window_sec seconds. Purges expired entries as a side
    effect either way -- callers still append their own timestamp on
    success, this only decides whether to let that happen."""
    if not RATE_LIMITS_ENABLED:
        return False
    now = time.time()
    while timestamps and now - timestamps[0] > window_sec:
        timestamps.popleft()
    return len(timestamps) >= limit

DEPLOY_RATE_LIMIT = 5           # max successful deploys...
DEPLOY_RATE_WINDOW_SEC = 3600   # ...per this many seconds
_deploy_timestamps: deque[float] = deque()

REDEPLOY_RATE_LIMIT = 3
REDEPLOY_RATE_WINDOW_SEC = 3600
_redeploy_timestamps: deque[float] = deque()

RESTART_RATE_LIMIT = 10
RESTART_RATE_WINDOW_SEC = 3600
_restart_timestamps: deque[float] = deque()

SET_ENV_RATE_LIMIT = 10
SET_ENV_RATE_WINDOW_SEC = 3600
_set_env_timestamps: deque[float] = deque()

MINT_TOKEN_RATE_LIMIT = 5
MINT_TOKEN_RATE_WINDOW_SEC = 3600
_mint_token_timestamps: deque[float] = deque()

REVOKE_TOKEN_RATE_LIMIT = 5
REVOKE_TOKEN_RATE_WINDOW_SEC = 3600
_revoke_token_timestamps: deque[float] = deque()

TEARDOWN_REQUEST_RATE_LIMIT = 5
TEARDOWN_REQUEST_RATE_WINDOW_SEC = 3600
_teardown_request_timestamps: deque[float] = deque()

APPROVE_ACTION_RATE_LIMIT = 3
APPROVE_ACTION_RATE_WINDOW_SEC = 3600
_approve_action_timestamps: deque[float] = deque()

REJECT_ACTION_RATE_LIMIT = 20
REJECT_ACTION_RATE_WINDOW_SEC = 3600
_reject_action_timestamps: deque[float] = deque()

MEMORY_INCREASE_REQUEST_RATE_LIMIT = 5
MEMORY_INCREASE_REQUEST_RATE_WINDOW_SEC = 3600
_memory_increase_request_timestamps: deque[float] = deque()

BUILD_SHA = "dev"

REPORT_TTL_SEC = 3600
_approved_reports: dict[str, dict] = {}

_HEAVY_DEPENDENCY_SIGNALS = (
    "next", "nuxt", "gatsby", "@remix-run", "puppeteer", "playwright",
    "sharp", "canvas", "ffmpeg", "tensorflow", "torch", "opencv",
    "pandas", "numpy", "scipy", "django", "selenium",
)


def _audit(action: str, params: dict, outcome: dict, client: dict | None = None) -> None:
    """Structured JSON-lines audit log -- every mutating call, in or out,
    including rejections."""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "action": action,
        "client": (client or {}).get("name", "unknown"),
        "params": params,
        "outcome": outcome,
    }
    print(json.dumps(entry), flush=True)  # also to stdout, per AGENTS.md §8
    with open(AUDIT_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


mcp = MCPServer(
    name="zorc",
    version="1.0.0",
    instructions=(
        "Deploy and inspect apps on the zorc platform (Coolify-orchestrated, "
        "the nodes defined in registry.yaml). Workflow: (1) "
        "get_platform_contract() to learn the required app shape before "
        "writing any code, (2) check_capability_exists() so you don't build "
        "something that already exists here, (3) once the repo exists, "
        "analyze_deployment_requirements() -- REQUIRED, not optional: submit "
        "your real understanding of the app (kind, framework, expected load, "
        "database/background-jobs/websocket/storage/public-ip needs, your own "
        "memory estimate and the reasoning behind it) and it's cross-checked "
        "against what the repo actually looks like; a poorly-justified or "
        "wildly-off estimate gets blocked with the specific discrepancy "
        "rather than silently accepted, (4) deploy() using the report_id that "
        "call returns -- memory and node placement come from that report, not "
        "from anything you pass directly. deploy() only ever creates a new "
        "app; it cannot touch, modify, or delete anything that already exists. "
        "Needs a database? Set app.yaml's database: true -- a dedicated Postgres "
        "gets provisioned and DATABASE_URL set automatically, you never handle "
        "credentials yourself. If your app needs any other env var beyond APP_ENV/"
        "LOG_LEVEL, declare it in app.yaml's env: section (see get_platform_contract) -- "
        "internal secrets get generated and set for you automatically; "
        "anything tied to a real external account must be passed to deploy() "
        "via env_overrides, and analyze_deployment_requirements()'s report "
        "tells you which is which before you get there."
    ),
)


@mcp.tool()
def whoami(ctx: Context) -> dict:
    """Returns the calling client's own resolved identity ({"name", "role"})"""
    return _caller_identity(ctx)


@mcp.tool()
def get_platform_contract() -> dict:
    """Returns the required app contract: files, endpoints, env vars, and
    hard rules an app must follow to be deployable on this platform."""
    return {
        "required_files": {
            "app.yaml": "declares name, memory_mb, port, domains, dependencies, an "
                        "(optional) database: true flag -- see database_provisioning "
                        "below -- and an (optional) env: section for anything beyond "
                        "the auto-provided vars -- see env_vars_beyond_defaults below",
            "Dockerfile": "only if your stack needs something build-autodetection "
                           "can't handle; otherwise a standard manifest "
                           "(package.json / requirements.txt / go.mod / etc) is enough",
        },
        "required_endpoints": {
            "GET /health": "200 {'status':'ok'} -- must NOT touch the database "
                            "(a slow query here can cascade into every app on the "
                            "node getting marked unhealthy at once)",
            "GET /ready": "200 once dependencies (DB etc) are actually reachable",
            "GET /version": "{'sha':..., 'built':...}",
            "GET /openapi.json": "your API spec",
        },
        "env_vars_provided_at_deploy": ["APP_ENV", "LOG_LEVEL"],
        "database_provisioning": (
            "DATABASE_URL is NOT provided unconditionally -- set app.yaml's top-level "
            "database: true and deploy() provisions a real, dedicated Postgres instance "
            "for your app, creates a scoped role+database on it, and sets DATABASE_URL "
            "before your container's first real start (same generate-and-set pattern as "
            "env:'s generate: hex secrets -- you never see or handle the credentials). "
            "Omit database: true (or set it false) if your app has no database -- "
            "nothing gets provisioned and DATABASE_URL is simply not set."
        ),
        "env_vars_beyond_defaults": (
            "If your app needs any env var other than the three above (a JWT/session "
            "signing secret, a third-party API key, anything your code reads at "
            "startup), declare it in app.yaml's env: section -- undeclared vars are "
            "never invented for you, and per the fail-loudly rule below your app "
            "should refuse to start without them, which means an undeclared one WILL "
            "crash-loop after an otherwise-successful deploy. Two kinds: "
            "`{GENERATE_ME: {generate: hex}}` for internal secrets zorc generates "
            "itself and sets before your container's first real start (you never see "
            "the value); `{EXTERNAL_KEY: {required: true}}` for anything tied to a "
            "real external account -- zorc can't invent those, so you (or whoever "
            "calls deploy()) must supply the actual value via deploy()'s "
            "env_overrides. analyze_deployment_requirements()'s report tells you "
            "which is which before you ever call deploy()."
        ),
        "hard_rules": [
            "No host port binding -- Traefik reaches containers by name on the shared network.",
            "No cross-app database access -- each app owns its database, no exceptions.",
            "Every service declares a memory limit (this is your app.yaml memory_mb).",
            "No secrets committed to the repo -- environment variables only.",
            "Structured JSON logs to stdout, never files; never log secrets/tokens/full request bodies.",
            "Fail loudly at startup if a required env var is missing -- never silently default.",
        ],
        "app_kinds": {
            "static": "index.html with no backend manifest -> Cloudflare Pages, zero node memory",
            "node / python / go / dockerfile": "-> Coolify on the chosen node, real memory_mb budget applies",
        },
        "deploy_workflow": (
            "Once your repo exists and pushes to GitHub: call analyze_deployment_requirements() -- "
            "REQUIRED before deploy(), not optional. It clones the repo, cross-checks your own stated "
            "requirements against what the code actually looks like, and either approves (returns a "
            "report_id) or blocks with the specific reason if your estimate doesn't hold up. deploy() "
            "then takes that report_id and derives memory/node placement from it, not from anything "
            "passed directly."
        ),
        "note": "This mirrors deploy/agent.py's classify() and AGENTS.md's app contract exactly -- "
                "classify_repo() will tell you which kind your actual repo will be detected as.",
    }


@mcp.tool()
def list_nodes() -> list[dict]:
    """Live view of every node this platform can deploy to: declared vs."""
    reg = agent.load_registry()
    out = []
    for node_name, node in reg["nodes"].items():
        static_headroom = agent.budget_headroom_mb(node_name)
        try:
            live_headroom = agent.live_headroom_mb(node_name)
        except Exception:
            live_headroom = None  # e.g. the node is unreachable right now
        app_count = sum(1 for a in reg.get("apps", []) if a.get("target") == node_name)
        out.append({
            "node": node_name,
            "static_headroom_mb": round(static_headroom),
            "live_headroom_mb": round(live_headroom) if live_headroom is not None else None,
            "has_public_ip": bool(node.get("has_public_ip", False)),
            "backend": node.get("backend"),
            "is_control_plane": bool(node.get("is_control_plane", False)),
            "provider": node.get("provider"),
            "app_count": app_count,
        })
    return out


@mcp.tool()
def propose_node(hostname: str) -> dict:
    """Read-only capability report for a node that is NOT yet part of this"""
    return agent.propose_node(hostname)


@mcp.tool()
def check_capability_exists(description: str) -> dict:
    """Search existing apps for something that might already provide what
    you're about to build (AGENTS.md's decision procedure step 1)."""
    reg = agent.load_registry()
    needle = description.lower()
    matches = [
        {"name": a["name"], "repo": a.get("repo"), "subdomain": a.get("subdomain")}
        for a in reg.get("apps", [])
        if needle in a["name"].lower() or needle in (a.get("repo") or "").lower()
    ]
    return {
        "query": description,
        "possible_matches": matches,
        "note": "Name/repo substring match only, not semantic search -- "
                "check any matches manually before assuming nothing exists.",
    }


@mcp.tool()
def classify_repo(owner_repo: str) -> dict:
    """Dry-run: clones the repo and classifies it (kind/language/estimated
    memory) exactly as deploy() would internally, WITHOUT deploying
    anything."""
    repo_dir = agent.clone_repo(owner_repo)
    try:
        return agent.classify(repo_dir)
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


NODES_DIR = agent.ZORC_DIR / "nodes"

NODE_TELEMETRY_STALE_SEC = 3600


def _load_node_telemetry(node_name: str) -> dict:
    """nodes/<name>.yaml -- live, self-reported hardware/health data, kept
    deliberately separate from registry.yaml's human-set policy layer (see
    that file's own comment on why)."""
    path = NODES_DIR / f"{node_name}.yaml"
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return {}


def _score_node(node_name: str, node_cfg: dict, memory_mb: int,
                 needs_public_ip: bool, required_arch: str | None, needs_gpu: bool = False) -> dict:
    """One node's fitness for one placement request."""
    if needs_public_ip and not node_cfg.get("has_public_ip"):
        return {"eligible": False, "score": 0.0, "reasons": ["needs a public IP, this node doesn't have one"]}

    headroom = agent.budget_headroom_mb(node_name)
    if memory_mb > headroom:
        return {"eligible": False, "score": 0.0,
                "reasons": [f"needs {memory_mb}MB, only {headroom:.0f}MB headroom"]}

    telemetry = _load_node_telemetry(node_name)
    node_arch = telemetry.get("arch")
    if required_arch and node_arch and node_arch != required_arch:
        return {"eligible": False, "score": 0.0,
                "reasons": [f"needs {required_arch}, node reports {node_arch}"]}

    if needs_gpu:
        has_accelerator = bool((telemetry.get("accelerator") or {}).get("name"))
        if not has_accelerator:
            return {"eligible": False, "score": 0.0, "reasons": ["needs a GPU, this node has no accelerator"]}
        if node_cfg.get("backend") != "zorc-agent":
            return {"eligible": False, "score": 0.0,
                    "reasons": [f"has an accelerator but backend={node_cfg.get('backend')!r} "
                                "doesn't implement GPU passthrough"]}

    if telemetry.get("status") == "unreachable":
        return {"eligible": False, "score": 0.0, "reasons": ["node is currently unreachable"]}

    reasons = [f"{headroom:.0f}MB headroom after fit"]
    score = min(headroom / max(memory_mb, 1), 10.0)

    last_seen = telemetry.get("last_seen")
    if last_seen:
        try:
            age_sec = time.time() - datetime.fromisoformat(last_seen).timestamp()
            if age_sec < 600:
                score += 2.0
                reasons.append("live telemetry fresh (<10min)")
            elif age_sec > NODE_TELEMETRY_STALE_SEC:
                score -= 3.0
                reasons.append(f"live telemetry stale ({age_sec / 3600:.1f}h old)")
        except ValueError:
            pass
    else:
        reasons.append("no live telemetry yet")

    if node_cfg.get("is_control_plane") and not needs_public_ip:
        score += 1.0
        reasons.append("control-plane node preferred by default")

    return {"eligible": True, "score": score, "reasons": reasons}


def _recommend_placement(memory_mb: int, needs_public_ip: bool, required_arch: str | None = None,
                          needs_gpu: bool = False) -> dict:
    """Scores every node in registry.yaml against this placement request,"""
    reg = agent.load_registry()
    scored = {
        node_name: _score_node(node_name, node_cfg, memory_mb, needs_public_ip, required_arch, needs_gpu)
        for node_name, node_cfg in reg["nodes"].items()
    }

    eligible = {n: s for n, s in scored.items() if s["eligible"]}
    if not eligible:
        detail = "; ".join(f"{n}: {', '.join(s['reasons'])}" for n, s in scored.items())
        return {"recommended_node": None, "fits": False, "reason": f"no eligible node -- {detail}"}

    best_name = max(eligible, key=lambda n: eligible[n]["score"])
    best = eligible[best_name]
    return {
        "recommended_node": best_name,
        "fits": True,
        "reason": "; ".join(best["reasons"]),
        "candidates_considered": {
            n: {"eligible": s["eligible"], "score": round(s["score"], 2)} for n, s in scored.items()
        },
    }


@mcp.tool()
def recommend_placement(memory_mb: int, needs_public_ip: bool = False, required_arch: str | None = None,
                         needs_gpu: bool = False) -> dict:
    """Recommends which node to target by scoring every registered node"""
    return _recommend_placement(memory_mb, needs_public_ip, required_arch, needs_gpu)


def _estimate_memory_from_repo(repo_dir, classification: dict) -> tuple[int, list[str]]:
    """A richer estimate than classify()'s flat per-language default --
    looks at actual dependencies, not just which manifest file exists."""
    base_mb = classification["memory_mb"]
    signals: list[str] = []
    deps: set[str] = set()

    pkg_json = repo_dir / "package.json"
    requirements_txt = repo_dir / "requirements.txt"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            deps = {d.lower() for d in {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}}
        except (json.JSONDecodeError, OSError):
            pass
    elif requirements_txt.exists():
        for line in requirements_txt.read_text().splitlines():
            line = line.strip().lower()
            if line and not line.startswith("#"):
                deps.add(line.split("==")[0].split(">=")[0].split("[")[0].strip())

    heavy_hits = sorted({d for d in deps for signal in _HEAVY_DEPENDENCY_SIGNALS if signal in d})
    if heavy_hits:
        base_mb = max(base_mb, 768)
        signals.append(f"heavy dependencies detected: {heavy_hits}")

    if len(deps) > 40:
        base_mb = max(base_mb, 512)
        signals.append(f"{len(deps)} dependencies -- larger than typical")

    return base_mb, signals


@mcp.tool()
def analyze_deployment_requirements(
    ctx: Context,
    owner_repo: str,
    architecture: Literal["single_service", "frontend_backend_split"],
    app_kind: Literal["static", "api", "full_stack_web", "background_worker", "realtime", "other"],
    frontend_rendering: Literal["static", "server_rendered", "none"],
    framework: str,
    expected_concurrency: Literal["low", "medium", "high"],
    has_database: bool,
    has_background_jobs: bool,
    needs_websockets: bool,
    needs_persistent_storage: bool,
    needs_public_ip: bool,
    needs_gpu: bool,
    estimated_memory_mb: int,
    reasoning: str,
) -> dict:
    """REQUIRED before deploy() -- deploy() will reject any call that
    doesn't reference an approved report_id from here."""
    caller = _caller_identity(ctx)

    if architecture == "frontend_backend_split":
        return {
            "status": "needs_split",
            "reason": (
                "This platform deploys one container per repo (AGENTS.md's app contract) -- a repo with "
                "genuinely separate frontend and backend processes doesn't fit that as a single analysis/deploy. "
                "Split it into two: call analyze_deployment_requirements() again for the frontend piece "
                "(architecture=\"single_service\", app_kind usually \"static\" unless it genuinely needs its "
                "own server) and again for the backend piece (architecture=\"single_service\", app_kind=\"api\" "
                "or similar), then deploy() each separately with distinct names/subdomains. If they're "
                "currently one repo with two subdirectories, the cleanest path is usually splitting them into "
                "two repos too -- ask the human if you're not sure that's wanted before restructuring anything."
            ),
        }

    if not reasoning or len(reasoning.strip()) < 20:
        return {
            "status": "rejected",
            "reason": "reasoning must actually justify the estimate (at least 20 characters) -- "
                      "a placeholder isn't acceptable, explain what in the app drives this number",
        }
    if estimated_memory_mb <= 0:
        return {"status": "rejected", "reason": "estimated_memory_mb must be a positive number"}

    repo_dir = agent.clone_repo(owner_repo)
    try:
        classification = agent.classify(repo_dir)
        if classification["kind"] == "unknown":
            return {"status": "rejected",
                    "reason": classification["reason"] + " -- cannot analyze an unrecognized repo"}

        try:
            parsed_app_yaml = agent.parse_app_yaml(repo_dir)
        except ValueError as e:
            return {"status": "rejected", "reason": f"app.yaml is malformed: {e}"}
        declared_env = parsed_app_yaml["env"]
        database_requested = parsed_app_yaml["database"]
        env_requirements = {
            "generated_internally": sorted(k for k, spec in declared_env.items() if "generate" in spec),
            "required_from_caller": sorted(k for k, spec in declared_env.items() if "required" in spec),
        }

        warnings = []
        if frontend_rendering == "static" and classification["kind"] != "static":
            warnings.append(
                f"you said frontend_rendering=\"static\" but the repo was detected as "
                f"kind={classification['kind']!r} ({classification['reason']}) -- this will deploy as a real "
                f"container on Coolify, not to Cloudflare Pages. If you intended a static export, check for a "
                f"lingering server start script or SSR config."
            )
        elif frontend_rendering == "server_rendered" and classification["kind"] == "static":
            warnings.append(
                f"you said frontend_rendering=\"server_rendered\" but the repo was detected as static "
                f"({classification['reason']}) -- this will deploy to Cloudflare Pages with zero node memory, "
                f"not as a running container. If you actually need server-side logic at runtime, that won't work "
                f"here -- check your build config."
            )

        if classification["kind"] == "static":
            report_id = secrets_module.token_hex(8)
            report = {
                "repo_kind": "static", "recommended_memory_mb": 0, "recommended_node": None,
                "recommended_build_tool": "cloudflare_pages", "status": "approved", "warnings": warnings,
            }
            _approved_reports[report_id] = {"report": report, "expires_at": time.time() + REPORT_TTL_SEC}
            return {**report, "report_id": report_id}

        repo_baseline_mb, signals = _estimate_memory_from_repo(repo_dir, classification)
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)

    concurrency_multiplier = {"low": 1.0, "medium": 1.5, "high": 2.5}[expected_concurrency]
    adjusted_estimate_mb = round(repo_baseline_mb * concurrency_multiplier)
    if has_background_jobs:
        adjusted_estimate_mb += 128
        signals.append("+128MB for background jobs")
    if needs_websockets:
        adjusted_estimate_mb += 128
        signals.append("+128MB for websockets")

    lower_bound = adjusted_estimate_mb * 0.5
    upper_bound = adjusted_estimate_mb * 2.0
    mismatched = not (lower_bound <= estimated_memory_mb <= upper_bound)

    if mismatched:
        return {
            "status": "blocked",
            "repo_kind": classification["kind"],
            "repo_language": classification["language"],
            "repo_baseline_mb": repo_baseline_mb,
            "signals": signals,
            "warnings": warnings,
            "env_requirements": env_requirements,
            "database_provisioned": database_requested,
            "concurrency_adjusted_estimate_mb": adjusted_estimate_mb,
            "self_reported_mb": estimated_memory_mb,
            "reason": (
                f"self-reported {estimated_memory_mb}MB is too far from the repo-derived estimate of "
                f"{adjusted_estimate_mb}MB (baseline {repo_baseline_mb}MB for {classification['language']}"
                + (f", {'; '.join(signals)}" if signals else "")
                + f", x{concurrency_multiplier} for {expected_concurrency} concurrency). "
                  f"Either the estimate is too low (real risk of the container getting OOM-killed) or too "
                  f"high (wastes node budget another app could use). Revise estimated_memory_mb and "
                  f"reasoning to match what the code actually needs, or if this app is genuinely unusual, "
                  f"explain specifically why in reasoning and call this again."
            ),
        }

    if caller.get("role") != "admin":
        owner_name = caller.get("name")
        current_total_mb = agent.owner_memory_total_mb(owner_name)
        owner_cap_mb = agent.owner_budget_mb(owner_name)
        projected_total_mb = current_total_mb + adjusted_estimate_mb
        if projected_total_mb > owner_cap_mb:
            return {
                "status": "blocked",
                "repo_kind": classification["kind"], "repo_language": classification["language"],
                "concurrency_adjusted_estimate_mb": adjusted_estimate_mb,
                "warnings": warnings,
                "env_requirements": env_requirements,
                "database_provisioned": database_requested,
                "owner_current_total_mb": current_total_mb,
                "owner_budget_mb": owner_cap_mb,
                "reason": (
                    f"{owner_name!r}'s apps already total {current_total_mb}MB across the platform; adding "
                    f"this app's {adjusted_estimate_mb}MB would bring that to {projected_total_mb}MB, over "
                    f"your {owner_cap_mb}MB soft per-owner budget (registry.yaml's owner_budgets). This is "
                    f"separate from node budget -- there may be plenty of room on the target node, this is "
                    f"specifically about how much YOU own platform-wide. Ask an admin to raise your override "
                    f"in registry.yaml if this app genuinely needs it."
                ),
            }

    placement = _recommend_placement(adjusted_estimate_mb, needs_public_ip, needs_gpu=needs_gpu)
    if not placement["fits"]:
        return {
            "status": "blocked",
            "repo_kind": classification["kind"], "repo_language": classification["language"],
            "concurrency_adjusted_estimate_mb": adjusted_estimate_mb,
            "warnings": warnings,
            "env_requirements": env_requirements,
            "database_provisioned": database_requested,
            "needs_gpu": needs_gpu,
            "reason": f"requirements are consistent, but nothing fits: {placement['reason']}",
        }

    report_id = secrets_module.token_hex(8)
    report = {
        "repo_kind": classification["kind"],
        "repo_language": classification["language"],
        "repo_baseline_mb": repo_baseline_mb,
        "signals": signals,
        "warnings": warnings,
        "recommended_memory_mb": adjusted_estimate_mb,
        "recommended_node": placement["recommended_node"],
        "recommended_build_tool": "dockerfile" if classification["language"] == "dockerfile" else "nixpacks",
        "app_kind": app_kind,
        "framework": framework,
        "env_requirements": env_requirements,
        "database_provisioned": database_requested,
        "needs_gpu": needs_gpu,
        "status": "approved",
    }
    _approved_reports[report_id] = {"report": report, "expires_at": time.time() + REPORT_TTL_SEC}
    note = f"valid for {REPORT_TTL_SEC // 60} minutes -- pass report_id to deploy()"
    if env_requirements["required_from_caller"]:
        note += (f"; deploy() will need env_overrides for {env_requirements['required_from_caller']} "
                 "(tied to an external account, zorc can't generate these itself)")
    return {**report, "report_id": report_id, "note": note}


@mcp.tool()
def check_budget(name: str, memory_mb: int, target_node: str = agent.LOCAL_NODE) -> dict:
    """Checks whether a deploy would fit the given node's budget, without
    deploying anything."""
    ok, reason = agent.check_deploy_budget(name, memory_mb, target_node)
    return {"fits": ok, "reason": reason}



def _read_deploy_history(name: str, limit: int = 20) -> list[dict]:
    """Every audit-logged action (deploy/redeploy/restart, including
    rejections and failures) whose params reference this app name, newest
    first."""
    if not AUDIT_LOG_PATH.exists():
        return []
    entries = []
    with open(AUDIT_LOG_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # a corrupt line shouldn't take the whole history down
            if entry.get("params", {}).get("name") == name:
                entries.append(entry)
    entries.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return entries[:limit]


@mcp.tool()
def get_app_status(ctx: Context, name: str) -> dict:
    """Live status of an app you own (or any app, if you're admin):"""
    caller = _caller_identity(ctx)
    gate = _require_owner_or_admin(caller, name)
    if not gate["ok"]:
        return gate
    status = agent.app_status(name)
    budget_mb, used_mb = status.get("memory_mb") or 0, status.get("mem_used_mb")
    if budget_mb and used_mb is not None:
        status["budget_utilization_percent"] = round(100 * used_mb / budget_mb, 1)
    return status


@mcp.tool()
def get_app_logs(ctx: Context, name: str, tail: int = 200, since: str | None = None,
                  grep: str | None = None) -> str:
    """Recent logs for an app you own (or any app, if you're admin)."""
    caller = _caller_identity(ctx)
    gate = _require_owner_or_admin(caller, name)
    if not gate["ok"]:
        return f"(refused: {gate['reason']})"
    return agent.app_logs(name, tail, since, grep)


@mcp.tool()
def get_deploy_history(ctx: Context, name: str, limit: int = 20) -> dict:
    """Recent deploy/redeploy/restart actions taken against an app you own"""
    caller = _caller_identity(ctx)
    gate = _require_owner_or_admin(caller, name)
    if not gate["ok"]:
        return gate
    return {"name": name, "history": _read_deploy_history(name, limit)}


_ENV_VAR_LOG_SIGNAL_RE = re.compile(
    r"\b[A-Z][A-Z0-9_]{2,}\b[^\n]{0,40}\b(is not (defined|set)|is required|must be set|missing)\b"
    r"|\b(missing|required)\b[^\n]{0,40}\b[A-Z][A-Z0-9_]{2,}\b"
    r"|KeyError:\s*'?[A-Z][A-Z0-9_]{2,}'?",
    re.IGNORECASE,
)

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "info": 3}


@mcp.tool()
def diagnose_app(ctx: Context, name: str) -> dict:
    """Fuses recent logs + live status + last deploy outcome + resource
    use into one "why might this app be unhealthy" answer, for an app you
    own (or any app, if you're admin)."""
    caller = _caller_identity(ctx)
    gate = _require_owner_or_admin(caller, name)
    if not gate["ok"]:
        return gate

    status = agent.app_status(name)
    history = _read_deploy_history(name, limit=5)
    logs = agent.app_logs(name, lines=200)

    findings = []

    if status.get("status") == "not_found":
        findings.append({"severity": "critical", "signal": "no running resource found",
                          "detail": "status is 'not_found' -- the container/app may never have started, "
                                    "or was removed outside zorc"})
    elif not str(status.get("status", "")).lower().startswith("running"):
        findings.append({"severity": "critical", "signal": f"status is {status.get('status')!r}, not running",
                          "detail": status})

    if status.get("kind") == "zorc-agent" and (status.get("restart_count") or 0) >= 3:
        findings.append({"severity": "high",
                          "signal": f"container has restarted {status['restart_count']} times",
                          "detail": "a high restart count usually means it's crash-looping, not just slow to start"})

    last_deploy = next((h for h in history if h.get("action") in ("deploy", "redeploy")), None)
    if last_deploy and last_deploy.get("outcome", {}).get("status") in ("failed", "rejected"):
        outcome = last_deploy["outcome"]
        findings.append({"severity": "high", "signal": f"last {last_deploy['action']} did not succeed",
                          "detail": {"step": outcome.get("step"), "reason": outcome.get("reason")}})

    budget_mb, used_mb = status.get("memory_mb") or 0, status.get("mem_used_mb")
    if budget_mb and used_mb is not None and used_mb >= budget_mb * 0.95:
        findings.append({"severity": "medium", "signal": f"using {used_mb}MB against a {budget_mb}MB budget",
                          "detail": "at or near its declared memory limit -- possible OOM risk/kill"})

    if isinstance(logs, str) and not logs.startswith("("):
        env_lines = [line for line in logs.splitlines() if _ENV_VAR_LOG_SIGNAL_RE.search(line)]
        if env_lines:
            findings.append({"severity": "high",
                              "signal": "log lines suggest a missing/misconfigured environment variable",
                              "detail": env_lines[:5]})

    if not findings:
        findings.append({"severity": "info", "signal": "no obvious problem found by these heuristics",
                          "detail": "status/logs/deploy-history all look nominal from here -- check the "
                                    "app's own /health and /ready responses and application-level logs "
                                    "for anything these heuristics wouldn't catch"})

    findings.sort(key=lambda f: _SEVERITY_ORDER.get(f["severity"], 4))
    return {
        "name": name,
        "status_summary": {"kind": status.get("kind"), "status": status.get("status"),
                            "memory_mb": status.get("memory_mb"), "mem_used_mb": status.get("mem_used_mb")},
        "last_deploy": last_deploy,
        "findings": findings,
        "log_tail": logs[-2000:] if isinstance(logs, str) else logs,
    }


@mcp.tool()
def deploy(ctx: Context, owner_repo: str, name: str, report_id: str, git_branch: str = "main",
           env_overrides: dict[str, str] | None = None) -> dict:
    """Deploys a new app."""
    caller = _caller_identity(ctx)
    params = {"owner_repo": owner_repo, "name": name, "report_id": report_id, "git_branch": git_branch,
              "env_overrides_keys": sorted((env_overrides or {}).keys())}  # keys only -- never log secret values

    entry = _approved_reports.get(report_id)
    if entry is None:
        outcome = {"status": "rejected",
                   "reason": f"no approved report {report_id!r} -- call analyze_deployment_requirements() "
                              "first (or it expired; reports are valid for 1 hour)"}
        _audit("deploy", params, outcome, client=caller)
        return outcome
    if time.time() > entry["expires_at"]:
        del _approved_reports[report_id]
        outcome = {"status": "rejected", "reason": f"report {report_id!r} expired -- call "
                                                     "analyze_deployment_requirements() again"}
        _audit("deploy", params, outcome, client=caller)
        return outcome
    report = entry["report"]

    now = time.time()
    if _rate_limited(_deploy_timestamps, DEPLOY_RATE_LIMIT, DEPLOY_RATE_WINDOW_SEC):
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {DEPLOY_RATE_LIMIT} deploys per {DEPLOY_RATE_WINDOW_SEC}s exceeded"}
        _audit("deploy", params, outcome, client=caller)
        return outcome

    if agent.name_taken(name):
        outcome = {"status": "rejected",
                   "reason": f"'{name}' already exists in registry.yaml -- this tool only creates new apps, "
                              "it cannot modify or redeploy an existing one"}
        _audit("deploy", params, outcome, client=caller)
        return outcome

    target_node = report["recommended_node"] or agent.LOCAL_NODE  # static sites: recommended_node is None, unused here
    memory_mb_override = report["recommended_memory_mb"] if report["repo_kind"] != "static" else None

    try:
        result = agent.deploy(owner_repo=owner_repo, name=name, owner=caller["name"], git_branch=git_branch,
                               target_node=target_node, memory_mb_override=memory_mb_override,
                               env_overrides=env_overrides, needs_gpu=report.get("needs_gpu", False))
        _deploy_timestamps.append(now)
        _audit("deploy", params, {"status": "deployed", "domain": result.get("domain"), "report": report}, client=caller)
        return result
    except agent.DeployError as e:
        outcome = {"status": "failed", "step": e.step, "reason": e.reason}
        _audit("deploy", params, outcome, client=caller)
        return outcome
    except KeyError as e:
        outcome = {"status": "rejected", "reason": str(e)}
        _audit("deploy", params, outcome, client=caller)
        return outcome


@mcp.tool()
def redeploy(ctx: Context, name: str, confirm_redeploy: bool = True) -> dict:
    """Re-triggers a build+deploy of an EXISTING app you already own (or
    any app, if you're admin) -- idempotent, non-destructive re-apply, NOT
    a way to change anything."""
    caller = _caller_identity(ctx)
    params = {"name": name, "confirm_redeploy": confirm_redeploy}

    if not confirm_redeploy:
        outcome = {"status": "rejected", "reason": "confirm_redeploy=False -- pass True to proceed"}
        _audit("redeploy", params, outcome, client=caller)
        return outcome

    gate = _require_owner_or_admin(caller, name)
    if not gate["ok"]:
        _audit("redeploy", params, gate, client=caller)
        return gate

    now = time.time()
    if _rate_limited(_redeploy_timestamps, REDEPLOY_RATE_LIMIT, REDEPLOY_RATE_WINDOW_SEC):
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {REDEPLOY_RATE_LIMIT} redeploys per {REDEPLOY_RATE_WINDOW_SEC}s exceeded"}
        _audit("redeploy", params, outcome, client=caller)
        return outcome

    try:
        result = agent.redeploy(name)
        _redeploy_timestamps.append(now)
        outcome = {"status": "redeployed", **result}
        _audit("redeploy", params, outcome, client=caller)
        return outcome
    except ValueError as e:
        outcome = {"status": "rejected", "reason": str(e)}
        _audit("redeploy", params, outcome, client=caller)
        return outcome


@mcp.tool()
def restart(ctx: Context, name: str) -> dict:
    """Restarts the running container for an app you own (or any app, if
    you're admin) -- no rebuild, no config/env/branch change, just a
    process restart."""
    caller = _caller_identity(ctx)
    params = {"name": name}

    gate = _require_owner_or_admin(caller, name)
    if not gate["ok"]:
        _audit("restart", params, gate, client=caller)
        return gate

    now = time.time()
    if _rate_limited(_restart_timestamps, RESTART_RATE_LIMIT, RESTART_RATE_WINDOW_SEC):
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {RESTART_RATE_LIMIT} restarts per {RESTART_RATE_WINDOW_SEC}s exceeded"}
        _audit("restart", params, outcome, client=caller)
        return outcome

    try:
        result = agent.app_action(name, "restart")
        _restart_timestamps.append(now)
        outcome = {"status": "restarted", "result": result}
        _audit("restart", params, outcome, client=caller)
        return outcome
    except ValueError as e:
        outcome = {"status": "rejected", "reason": str(e)}
        _audit("restart", params, outcome, client=caller)
        return outcome
    except RuntimeError as e:
        outcome = {"status": "failed", "reason": str(e)}
        _audit("restart", params, outcome, client=caller)
        return outcome


@mcp.tool()
def set_app_env_vars(ctx: Context, name: str, env_vars: dict[str, str]) -> dict:
    """Sets or updates env vars on an app you own (or any app, if you're
    admin), then redeploys so it actually reaches the running container.
    deploy() only ever creates a new app and redeploy()/restart() reuse
    whatever env vars Coolify already has, unchanged -- this is the only
    tool that can change one. Same trust tier as redeploy()/restart():
    owner-or-admin, immediate, no approval gate -- an owner can already
    redeploy/restart their own app without one, and this changes strictly
    less than a full redeploy. Values are never logged, only which keys
    were touched."""
    caller = _caller_identity(ctx)
    params = {"name": name, "keys": sorted((env_vars or {}).keys())}

    gate = _require_owner_or_admin(caller, name)
    if not gate["ok"]:
        _audit("set_app_env_vars", params, gate, client=caller)
        return gate

    if not env_vars:
        outcome = {"status": "rejected", "reason": "env_vars must not be empty"}
        _audit("set_app_env_vars", params, outcome, client=caller)
        return outcome

    now = time.time()
    if _rate_limited(_set_env_timestamps, SET_ENV_RATE_LIMIT, SET_ENV_RATE_WINDOW_SEC):
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {SET_ENV_RATE_LIMIT} env var updates per "
                             f"{SET_ENV_RATE_WINDOW_SEC}s exceeded"}
        _audit("set_app_env_vars", params, outcome, client=caller)
        return outcome

    try:
        result = agent.set_app_env_vars(name, env_vars)
        _set_env_timestamps.append(now)
        outcome = {"status": "updated", "keys": result["keys"]}
        _audit("set_app_env_vars", params, outcome, client=caller)
        return outcome
    except ValueError as e:
        outcome = {"status": "rejected", "reason": str(e)}
        _audit("set_app_env_vars", params, outcome, client=caller)
        return outcome
    except Exception as e:
        outcome = {"status": "failed", "reason": str(e)}
        _audit("set_app_env_vars", params, outcome, client=caller)
        return outcome


def _load_pending_actions() -> dict:
    if not PENDING_ACTIONS_PATH.exists():
        return {}
    return json.loads(PENDING_ACTIONS_PATH.read_text())


def _save_pending_actions(actions: dict) -> None:
    PENDING_ACTIONS_PATH.write_text(json.dumps(actions, indent=2))


@mcp.tool()
def request_teardown(ctx: Context, name: str) -> dict:
    """Queues a teardown request for an app you own (or any app, if
    you're admin) -- does NOT delete anything."""
    caller = _caller_identity(ctx)
    params = {"name": name}

    gate = _require_owner_or_admin(caller, name)
    if not gate["ok"]:
        _audit("request_teardown", params, gate, client=caller)
        return gate

    now = time.time()
    if _rate_limited(_teardown_request_timestamps, TEARDOWN_REQUEST_RATE_LIMIT, TEARDOWN_REQUEST_RATE_WINDOW_SEC):
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {TEARDOWN_REQUEST_RATE_LIMIT} teardown requests per "
                             f"{TEARDOWN_REQUEST_RATE_WINDOW_SEC}s exceeded"}
        _audit("request_teardown", params, outcome, client=caller)
        return outcome

    actions = _load_pending_actions()
    existing = next((a for a in actions.values()
                      if a.get("name") == name and a.get("action") == "teardown" and a.get("status") == "pending"),
                     None)
    if existing:
        outcome = {"status": "already_pending", "id": existing["id"], "name": name,
                    "reason": f"a teardown request for {name!r} is already pending (id {existing['id']!r}, "
                              f"requested by {existing['requested_by']!r})"}
        _audit("request_teardown", params, outcome, client=caller)
        return outcome

    action_id = secrets_module.token_hex(8)
    actions[action_id] = {
        "id": action_id, "action": "teardown", "name": name,
        "requested_by": caller["name"], "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "pending",
    }
    _save_pending_actions(actions)
    _teardown_request_timestamps.append(now)
    outcome = {"status": "requested", "id": action_id, "name": name}
    _audit("request_teardown", params, outcome, client=caller)
    return outcome


@mcp.tool()
def request_memory_increase(ctx: Context, name: str, requested_memory_mb: int, reason: str) -> dict:
    """Queues a memory increase request for an app you own (or any app,
    if you're admin) -- does NOT change anything."""
    caller = _caller_identity(ctx)
    params = {"name": name, "requested_memory_mb": requested_memory_mb}

    gate = _require_owner_or_admin(caller, name)
    if not gate["ok"]:
        _audit("request_memory_increase", params, gate, client=caller)
        return gate

    if not reason or len(reason.strip()) < 10:
        outcome = {"status": "rejected",
                   "reason": "reason must actually justify the request (at least 10 characters)"}
        _audit("request_memory_increase", params, outcome, client=caller)
        return outcome

    reg = agent.load_registry()
    app_entry = next((a for a in reg.get("apps", []) if a["name"] == name), None)
    if app_entry is None:
        outcome = {"status": "rejected", "reason": f"{name!r} is not a registered app"}
        _audit("request_memory_increase", params, outcome, client=caller)
        return outcome
    current_memory_mb = app_entry["memory_mb"]

    if requested_memory_mb <= current_memory_mb:
        outcome = {"status": "rejected",
                   "reason": f"requested_memory_mb ({requested_memory_mb}) must be greater than "
                             f"{name!r}'s current memory_mb ({current_memory_mb}) -- this tool only "
                             "queues an increase, there is no decrease path"}
        _audit("request_memory_increase", params, outcome, client=caller)
        return outcome

    if caller.get("role") != "admin":
        owner_name = caller.get("name")
        delta = requested_memory_mb - current_memory_mb
        current_total_mb = agent.owner_memory_total_mb(owner_name)
        owner_cap_mb = agent.owner_budget_mb(owner_name)
        projected_total_mb = current_total_mb + delta
        if projected_total_mb > owner_cap_mb:
            outcome = {
                "status": "rejected",
                "owner_current_total_mb": current_total_mb, "owner_budget_mb": owner_cap_mb,
                "reason": (
                    f"{owner_name!r}'s apps already total {current_total_mb}MB across the platform; this "
                    f"+{delta}MB increase would bring that to {projected_total_mb}MB, over your "
                    f"{owner_cap_mb}MB soft per-owner budget (registry.yaml's owner_budgets). Ask an admin "
                    "to raise your override in registry.yaml if this app genuinely needs it."
                ),
            }
            _audit("request_memory_increase", params, outcome, client=caller)
            return outcome

    now = time.time()
    if _rate_limited(_memory_increase_request_timestamps, MEMORY_INCREASE_REQUEST_RATE_LIMIT, MEMORY_INCREASE_REQUEST_RATE_WINDOW_SEC):
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {MEMORY_INCREASE_REQUEST_RATE_LIMIT} memory increase requests per "
                             f"{MEMORY_INCREASE_REQUEST_RATE_WINDOW_SEC}s exceeded"}
        _audit("request_memory_increase", params, outcome, client=caller)
        return outcome

    actions = _load_pending_actions()
    existing = next((a for a in actions.values()
                      if a.get("name") == name and a.get("action") == "memory_increase"
                      and a.get("status") == "pending"), None)
    if existing:
        outcome = {"status": "already_pending", "id": existing["id"], "name": name,
                    "reason": f"a memory increase request for {name!r} is already pending "
                              f"(id {existing['id']!r}, requested by {existing['requested_by']!r})"}
        _audit("request_memory_increase", params, outcome, client=caller)
        return outcome

    action_id = secrets_module.token_hex(8)
    actions[action_id] = {
        "id": action_id, "action": "memory_increase", "name": name,
        "requested_by": caller["name"], "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "current_memory_mb": current_memory_mb, "requested_memory_mb": requested_memory_mb,
        "reason": reason.strip(),
        "status": "pending",
    }
    _save_pending_actions(actions)
    _memory_increase_request_timestamps.append(now)
    outcome = {"status": "requested", "id": action_id, "name": name,
               "current_memory_mb": current_memory_mb, "requested_memory_mb": requested_memory_mb}
    _audit("request_memory_increase", params, outcome, client=caller)
    return outcome



@mcp.tool()
def list_pending_actions(ctx: Context) -> dict:
    """Lists queued actions from request_teardown() and request_memory_increase()."""
    caller = _caller_identity(ctx)
    actions = list(_load_pending_actions().values())
    if caller.get("role") != "admin":
        actions = [a for a in actions if a.get("requested_by") == caller.get("name")]
    actions.sort(key=lambda a: a.get("requested_at", ""), reverse=True)
    return {"actions": actions}


@mcp.tool()
def approve_action(ctx: Context, id: str) -> dict:
    """Executes a pending action queued by request_teardown() or
    request_memory_increase() -- ADMIN ONLY, regardless of who requested
    it or who owns the app."""
    caller = _caller_identity(ctx)
    params = {"id": id}

    if caller.get("role") != "admin":
        outcome = {"status": "rejected", "reason": "approve_action is admin-only"}
        _audit("approve_action", params, outcome, client=caller)
        return outcome

    now = time.time()
    if _rate_limited(_approve_action_timestamps, APPROVE_ACTION_RATE_LIMIT, APPROVE_ACTION_RATE_WINDOW_SEC):
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {APPROVE_ACTION_RATE_LIMIT} approvals per "
                             f"{APPROVE_ACTION_RATE_WINDOW_SEC}s exceeded"}
        _audit("approve_action", params, outcome, client=caller)
        return outcome

    actions = _load_pending_actions()
    entry = actions.get(id)
    if entry is None:
        outcome = {"status": "rejected", "reason": f"no pending action with id {id!r}"}
        _audit("approve_action", params, outcome, client=caller)
        return outcome
    if entry.get("status") != "pending":
        outcome = {"status": "rejected",
                   "reason": f"action {id!r} is already {entry.get('status')!r}, not pending"}
        _audit("approve_action", params, outcome, client=caller)
        return outcome
    if entry.get("action") not in ("teardown", "memory_increase"):
        outcome = {"status": "rejected", "reason": f"unknown action type {entry.get('action')!r}"}
        _audit("approve_action", params, outcome, client=caller)
        return outcome

    try:
        if entry["action"] == "teardown":
            result = agent.delete_app(entry["name"])
        else:  # memory_increase
            result = agent.resize_app_memory(entry["name"], entry["requested_memory_mb"])
        entry.update(status="approved_and_executed", approved_by=caller["name"],
                      approved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), result=result)
        actions[id] = entry
        _save_pending_actions(actions)
        _approve_action_timestamps.append(now)
        outcome = {"status": "executed", "id": id, "name": entry["name"], "result": result}
        _audit("approve_action", params, outcome, client=caller)
        return outcome
    except Exception as e:
        entry.update(status="failed", approved_by=caller["name"],
                      approved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), error=str(e))
        actions[id] = entry
        _save_pending_actions(actions)
        outcome = {"status": "failed", "id": id, "name": entry["name"], "reason": str(e)}
        _audit("approve_action", params, outcome, client=caller)
        return outcome


@mcp.tool()
def reject_action(ctx: Context, id: str) -> dict:
    """Declines a pending action queued by request_teardown() or
    request_memory_increase() without executing it -- ADMIN ONLY."""
    caller = _caller_identity(ctx)
    params = {"id": id}

    if caller.get("role") != "admin":
        outcome = {"status": "rejected", "reason": "reject_action is admin-only"}
        _audit("reject_action", params, outcome, client=caller)
        return outcome

    now = time.time()
    if _rate_limited(_reject_action_timestamps, REJECT_ACTION_RATE_LIMIT, REJECT_ACTION_RATE_WINDOW_SEC):
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {REJECT_ACTION_RATE_LIMIT} rejections per "
                             f"{REJECT_ACTION_RATE_WINDOW_SEC}s exceeded"}
        _audit("reject_action", params, outcome, client=caller)
        return outcome

    actions = _load_pending_actions()
    entry = actions.get(id)
    if entry is None:
        outcome = {"status": "rejected", "reason": f"no pending action with id {id!r}"}
        _audit("reject_action", params, outcome, client=caller)
        return outcome
    if entry.get("status") != "pending":
        outcome = {"status": "rejected",
                   "reason": f"action {id!r} is already {entry.get('status')!r}, not pending"}
        _audit("reject_action", params, outcome, client=caller)
        return outcome

    entry.update(status="rejected", rejected_by=caller["name"],
                 rejected_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    actions[id] = entry
    _save_pending_actions(actions)
    _reject_action_timestamps.append(now)
    outcome = {"status": "action_rejected", "id": id, "name": entry["name"]}
    _audit("reject_action", params, outcome, client=caller)
    return outcome



def _write_token_map(token_map: dict) -> None:
    MCP_TOKEN_PATH.write_text(json.dumps(token_map, indent=2) + "\n")
    _TOKEN_CACHE["mtime"] = None  # force _load_token_map() to reread on next call


@mcp.tool()
def list_clients(ctx: Context) -> dict:
    """Lists every client with a bearer token -- name and role only,
    never the token or its hash. Any authenticated caller can see this
    (matches mint_client_token() not being role-gated). Read-only, not
    rate-limited."""
    _caller_identity(ctx)  # still must resolve to a real, valid token
    try:
        token_map = _load_token_map()
    except Exception as e:
        return {"status": "rejected", "reason": f"token map is currently unreadable: {e}"}
    clients = sorted(
        ({"name": info["name"], "role": info["role"]} for info in token_map.values()),
        key=lambda c: c["name"],
    )
    return {"clients": clients}


@mcp.tool()
def mint_client_token(ctx: Context, name: str, role: Literal["admin", "client"]) -> dict:
    """Mints (or rotates) a bearer token for one client -- the
    self-service replacement for running scripts/mint_token.py by hand.
    Not admin-gated, deliberately: reaching this server at all already
    requires holding a valid token -- that's the real trust boundary, not
    an extra role check on top of it. Returns the raw token ONCE, in this
    response -- it is never stored, logged, or retrievable again; if it's
    lost, mint again. Rotating an existing name replaces only that
    client's token, everyone else is untouched. Rate-limited and audited
    -- the raw token is never written to the audit log."""
    caller = _caller_identity(ctx)
    params = {"name": name, "role": role}
    name = (name or "").strip()
    if not name:
        outcome = {"status": "rejected", "reason": "name must not be empty"}
        _audit("mint_client_token", params, outcome, client=caller)
        return outcome

    now = time.time()
    if _rate_limited(_mint_token_timestamps, MINT_TOKEN_RATE_LIMIT, MINT_TOKEN_RATE_WINDOW_SEC):
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {MINT_TOKEN_RATE_LIMIT} mints per "
                             f"{MINT_TOKEN_RATE_WINDOW_SEC}s exceeded"}
        _audit("mint_client_token", params, outcome, client=caller)
        return outcome

    try:
        token_map = _load_token_map()
    except Exception:
        token_map = {}
    before = len(token_map)
    token_map = {h: info for h, info in token_map.items() if info.get("name") != name}
    replaced = len(token_map) < before

    token = secrets_module.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    token_map[token_hash] = {"name": name, "role": role}
    _write_token_map(token_map)

    _mint_token_timestamps.append(now)
    outcome = {"status": "rotated" if replaced else "minted", "name": name, "role": role}
    _audit("mint_client_token", params, outcome, client=caller)
    return {**outcome, "token": token}


@mcp.tool()
def revoke_client_token(ctx: Context, name: str) -> dict:
    """Removes a client's token entirely -- ADMIN ONLY, unlike minting.
    Taking access away is a different, less recoverable direction than
    granting it. Not a rotation: that name has no valid token afterward
    until mint_client_token() is called for it again. Refuses to remove
    the last remaining admin -- that would lock everyone out with no way
    back in short of shell access. Rate-limited and audited."""
    caller = _caller_identity(ctx)
    params = {"name": name}
    if caller.get("role") != "admin":
        outcome = {"status": "rejected", "reason": "revoke_client_token is admin-only"}
        _audit("revoke_client_token", params, outcome, client=caller)
        return outcome

    now = time.time()
    if _rate_limited(_revoke_token_timestamps, REVOKE_TOKEN_RATE_LIMIT, REVOKE_TOKEN_RATE_WINDOW_SEC):
        outcome = {"status": "rejected",
                   "reason": f"rate limit: {REVOKE_TOKEN_RATE_LIMIT} revocations per "
                             f"{REVOKE_TOKEN_RATE_WINDOW_SEC}s exceeded"}
        _audit("revoke_client_token", params, outcome, client=caller)
        return outcome

    try:
        token_map = _load_token_map()
    except Exception as e:
        outcome = {"status": "rejected", "reason": f"token map is currently unreadable: {e}"}
        _audit("revoke_client_token", params, outcome, client=caller)
        return outcome

    matching = {h: info for h, info in token_map.items() if info.get("name") == name}
    if not matching:
        outcome = {"status": "rejected", "reason": f"no token for {name!r}"}
        _audit("revoke_client_token", params, outcome, client=caller)
        return outcome

    remaining_admins = sum(1 for h, info in token_map.items()
                            if info.get("role") == "admin" and h not in matching)
    if any(info.get("role") == "admin" for info in matching.values()) and remaining_admins == 0:
        outcome = {"status": "rejected",
                   "reason": f"{name!r} is the last remaining admin -- revoking it would lock everyone out"}
        _audit("revoke_client_token", params, outcome, client=caller)
        return outcome

    new_map = {h: info for h, info in token_map.items() if info.get("name") != name}
    _write_token_map(new_map)

    _revoke_token_timestamps.append(now)
    outcome = {"status": "revoked", "name": name}
    _audit("revoke_client_token", params, outcome, client=caller)
    return outcome


_TOKEN_CACHE: dict = {"mtime": None, "map": {}}


def _load_token_map() -> dict:
    """{sha256(token) hex: {"name": ..., "role": "admin"|"client"}} -- see
    scripts/mint_token.py, the only thing that ever writes this file."""
    mtime = MCP_TOKEN_PATH.stat().st_mtime
    if _TOKEN_CACHE["mtime"] != mtime:
        raw = json.loads(MCP_TOKEN_PATH.read_text())
        if not isinstance(raw, dict):
            raise ValueError(f"{MCP_TOKEN_PATH}: expected a JSON object of {{hash: {{name, role}}}}, "
                              f"got {type(raw).__name__}")
        for h, info in raw.items():
            if (not isinstance(info, dict) or not isinstance(info.get("name"), str) or not info.get("name")
                    or info.get("role") not in ("admin", "client")):
                raise ValueError(f"{MCP_TOKEN_PATH}: malformed entry for hash {h[:8]}... -- "
                                  "expected {'name': <non-empty str>, 'role': 'admin'|'client'}")
        _TOKEN_CACHE["map"] = raw
        _TOKEN_CACHE["mtime"] = mtime
    return _TOKEN_CACHE["map"]


def _resolve_client(token: str) -> dict | None:
    """Hashes the candidate bearer token and checks it against every stored"""
    if not token:
        return None
    try:
        token_map = _load_token_map()
    except Exception:
        return None  # can't authoritatively resolve anything right now -- fail closed, not 500
    candidate_hash = hashlib.sha256(token.encode()).hexdigest()
    for stored_hash, info in token_map.items():
        if secrets_module.compare_digest(candidate_hash, stored_hash):
            return info
    return None


def _caller_identity(ctx: Context) -> dict:
    """Re-resolves the calling client's {"name", "role"} from the bearer
    token on this MCP request, for tools to pass into _audit()."""
    headers = ctx.headers or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
    client = _resolve_client(token)
    if client is None:
        raise PermissionError("could not resolve caller identity from this request's bearer token")
    return client



def _require_owner_or_admin(caller: dict, app_name: str) -> dict:
    """Ownership gate for a mutating tool acting on an EXISTING app."""
    reg = agent.load_registry()
    app = next((a for a in reg.get("apps", []) if a.get("name") == app_name), None)
    if app is None:
        return {"ok": False, "reason": f"{app_name!r} is not a registered app"}
    if caller.get("role") == "admin":
        return {"ok": True, "app": app}
    owner = app.get("owner")
    if not owner or owner != caller.get("name"):
        return {"ok": False, "reason": f"{caller.get('name')!r} does not own {app_name!r}"}
    return {"ok": True, "app": app}


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Per-client bearer tokens (see _resolve_client/_load_token_map above),
    checked on every request except the platform's own required
    health/version endpoints."""

    _OAUTH_DISCOVERY_PREFIXES = ("/.well-known/",)
    _OAUTH_DISCOVERY_PATHS = {"/register"}

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (path in PUBLIC_PATHS
                or path in self._OAUTH_DISCOVERY_PATHS
                or path.startswith(self._OAUTH_DISCOVERY_PREFIXES)):
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        client = _resolve_client(token)
        if client is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        request.state.client_name = client["name"]
        request.state.client_role = client["role"]
        return await call_next(request)



async def health(request: Request):
    return JSONResponse({"status": "ok"})


async def ready(request: Request):
    return JSONResponse({"status": "ready"})


async def version(request: Request):
    return JSONResponse({"sha": BUILD_SHA, "built": None})


def build_app() -> Starlette:
    _load_token_map()

    allowed_hosts = [f"mcp.{agent.config.root_domain}", "127.0.0.1:8081", "localhost:8081"]
    allowed_hosts += [h for h in os.environ.get("ZORC_MCP_ALLOWED_HOSTS", "").split(",") if h]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
    )
    app = mcp.streamable_http_app(transport_security=security)
    app.add_route("/health", health, methods=["GET"])
    app.add_route("/ready", ready, methods=["GET"])
    app.add_route("/version", version, methods=["GET"])
    app.add_middleware(BearerAuthMiddleware)
    return app


app = build_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
