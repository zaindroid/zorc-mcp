#!/usr/bin/env python3
"""test_redeploy_restart.py — Phase 2 pipeline test (redeploy/restart)."""
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

ZORC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ZORC_DIR / "deploy"))

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


class FakeCtx:
    """The ONLY thing mcp_server.py's _caller_identity touches on a real
    Context is .headers (see its docstring) -- so this is a faithful
    stand-in for testing without spinning up a real MCP session."""
    def __init__(self, bearer_token: str):
        self.headers = {"authorization": f"Bearer {bearer_token}"}


def test_agent_redeploy(agent) -> None:
    original_name_taken = agent.name_taken
    original_load_resource_map = agent._load_resource_map
    original_trigger = agent.trigger_coolify_deploy

    calls = []
    agent.trigger_coolify_deploy = lambda uuid: calls.append(uuid)

    try:
        agent.name_taken = lambda n: False
        try:
            agent.redeploy("ghost-app")
            check("redeploy on missing app refuses", False, "no exception raised")
        except ValueError as e:
            check("redeploy on missing app refuses", "not a registered app" in str(e), str(e))

        agent.name_taken = lambda n: True

        for bad_kind in ("coolify-service", "zorc-agent", "pages", None):
            agent._load_resource_map = lambda k=bad_kind: {"myapp": {"kind": k, "coolify_uuid": "u1"}}
            calls.clear()
            try:
                agent.redeploy("myapp")
                check(f"redeploy refuses kind={bad_kind!r}", False, "no exception raised")
            except ValueError:
                check(f"redeploy refuses kind={bad_kind!r}", True)
            check(f"redeploy refuses kind={bad_kind!r} without calling Coolify", calls == [], f"calls={calls}")

        agent._load_resource_map = lambda: {"myapp": {"kind": "coolify"}}
        calls.clear()
        try:
            agent.redeploy("myapp")
            check("redeploy refuses coolify app with no recorded uuid", False)
        except ValueError:
            check("redeploy refuses coolify app with no recorded uuid", True)

        agent._load_resource_map = lambda: {"myapp": {"kind": "coolify", "coolify_uuid": "the-real-uuid"}}
        calls.clear()
        result = agent.redeploy("myapp")
        check("redeploy on a valid coolify app triggers exactly one build",
              calls == ["the-real-uuid"], f"calls={calls}")
        check("redeploy returns the triggered uuid", result.get("coolify_uuid") == "the-real-uuid", f"got {result}")
    finally:
        agent.name_taken = original_name_taken
        agent._load_resource_map = original_load_resource_map
        agent.trigger_coolify_deploy = original_trigger


def test_agent_app_action_zorc_agent(agent) -> None:
    original_load_resource_map = agent._load_resource_map
    original_node_config = agent.node_config
    original_ssh_run = agent._ssh_run

    ssh_calls = []

    def fake_ssh_run(tailscale_ip, ssh_key, remote_cmd, user="root", timeout=15):
        ssh_calls.append((tailscale_ip, remote_cmd, user))
        return (0, "restarted\n", "")

    try:
        agent._load_resource_map = lambda: {
            "gpu-app": {"kind": "zorc-agent", "node": "fake-node", "container_name": "gpu-app-container"}
        }
        agent.node_config = lambda n: {"tailscale_ip": "100.1.2.3", "ssh_key": "deploy/secrets/fake_key",
                                        "ssh_user": "zul"}
        agent._ssh_run = fake_ssh_run

        result = agent.app_action("gpu-app", "restart")
        check("zorc-agent restart issues the right docker command",
              ssh_calls and ssh_calls[0][1] == ["docker", "restart", "gpu-app-container"], f"got {ssh_calls}")
        check("zorc-agent restart uses the app's own node user", ssh_calls[0][2] == "zul", f"got {ssh_calls}")
        check("zorc-agent restart returns a structured result", result.get("action") == "restart", f"got {result}")

        agent._ssh_run = lambda *a, **kw: (1, "", "no such container")
        try:
            agent.app_action("gpu-app", "restart")
            check("failed docker restart raises", False)
        except RuntimeError as e:
            check("failed docker restart raises", "no such container" in str(e), str(e))
    finally:
        agent._load_resource_map = original_load_resource_map
        agent.node_config = original_node_config
        agent._ssh_run = original_ssh_run


def test_mcp_tools(agent, m) -> None:
    admin_tok, owner_tok, other_tok = "admintok", "ownertok", "othertok"
    tmp = Path(tempfile.mktemp(suffix=".json"))
    tmp.write_text(json.dumps({
        hashlib.sha256(admin_tok.encode()).hexdigest(): {"name": "admin-user", "role": "admin"},
        hashlib.sha256(owner_tok.encode()).hexdigest(): {"name": "owner-client", "role": "client"},
        hashlib.sha256(other_tok.encode()).hexdigest(): {"name": "other-client", "role": "client"},
    }))
    m.MCP_TOKEN_PATH = tmp
    m._TOKEN_CACHE = {"mtime": None, "map": {}}

    audit_tmp = Path(tempfile.mktemp(suffix=".log"))
    original_audit_log_path = m.AUDIT_LOG_PATH
    m.AUDIT_LOG_PATH = audit_tmp

    fixture_registry = {"apps": [{"name": "app-x", "owner": "owner-client", "target": "local"}]}
    original_load_registry = agent.load_registry
    agent.load_registry = lambda: fixture_registry

    original_redeploy = agent.redeploy
    original_app_action = agent.app_action
    redeploy_calls, restart_calls = [], []
    agent.redeploy = lambda name: (redeploy_calls.append(name), {"redeployed": name, "kind": "coolify",
                                                                    "coolify_uuid": "u"})[1]
    agent.app_action = lambda name, action: (restart_calls.append((name, action)),
                                              {"action": action, "name": name})[1]

    m._redeploy_timestamps.clear()
    m._restart_timestamps.clear()

    try:
        redeploy_calls.clear()
        r = m.redeploy(FakeCtx(other_tok), "app-x")
        check("redeploy: non-owner client is refused", r.get("status") == "rejected" or r.get("ok") is False,
              f"got {r}")
        check("redeploy: non-owner refusal never calls agent.redeploy", redeploy_calls == [], f"{redeploy_calls}")

        redeploy_calls.clear()
        r = m.redeploy(FakeCtx(owner_tok), "app-x")
        check("redeploy: owner is allowed", redeploy_calls == ["app-x"], f"got {r}, calls={redeploy_calls}")

        redeploy_calls.clear()
        r = m.redeploy(FakeCtx(admin_tok), "app-x")
        check("redeploy: admin is allowed on a non-owned app", redeploy_calls == ["app-x"], f"got {r}")

        redeploy_calls.clear()
        r = m.redeploy(FakeCtx(owner_tok), "app-x", confirm_redeploy=False)
        check("redeploy: confirm_redeploy=False refuses without calling agent.redeploy",
              redeploy_calls == [] and r.get("status") == "rejected", f"got {r}")

        import inspect
        sig = inspect.signature(m.redeploy)
        param_names = set(sig.parameters) - {"ctx"}
        check("redeploy tool accepts exactly {name, confirm_redeploy} -- no room for extra caller params",
              param_names == {"name", "confirm_redeploy"}, f"got {param_names}")

        m._redeploy_timestamps.clear()
        redeploy_calls.clear()
        results = [m.redeploy(FakeCtx(owner_tok), "app-x") for _ in range(m.REDEPLOY_RATE_LIMIT + 2)]
        succeeded = sum(1 for r in results if r.get("status") == "redeployed")
        rate_limited = sum(1 for r in results if "rate limit" in (r.get("reason") or ""))
        check(f"redeploy rate limit trips at exactly {m.REDEPLOY_RATE_LIMIT}/hour",
              succeeded == m.REDEPLOY_RATE_LIMIT and rate_limited == 2,
              f"succeeded={succeeded} rate_limited={rate_limited} results={results}")

        restart_calls.clear()
        r = m.restart(FakeCtx(other_tok), "app-x")
        check("restart: non-owner client is refused", restart_calls == [], f"got {r}")
        r = m.restart(FakeCtx(owner_tok), "app-x")
        check("restart: owner is allowed", restart_calls == [("app-x", "restart")], f"got {r}")

        m._restart_timestamps.clear()
        restart_calls.clear()
        results = [m.restart(FakeCtx(owner_tok), "app-x") for _ in range(m.RESTART_RATE_LIMIT + 2)]
        succeeded = sum(1 for r in results if r.get("status") == "restarted")
        check(f"restart rate limit trips at exactly {m.RESTART_RATE_LIMIT}/hour",
              succeeded == m.RESTART_RATE_LIMIT, f"succeeded={succeeded} results={results}")
    finally:
        agent.load_registry = original_load_registry
        agent.redeploy = original_redeploy
        agent.app_action = original_app_action
        m.AUDIT_LOG_PATH = original_audit_log_path
        tmp.unlink(missing_ok=True)
        audit_tmp.unlink(missing_ok=True)


def main() -> int:
    import agent
    import mcp_server as m

    test_agent_redeploy(agent)
    test_agent_app_action_zorc_agent(agent)
    test_mcp_tools(agent, m)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
