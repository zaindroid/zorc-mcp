#!/usr/bin/env python3
"""test_observability.py — Phase 3 pipeline test (get_app_status/
get_app_logs/get_deploy_history/diagnose_app)."""
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
    def __init__(self, bearer_token: str):
        self.headers = {"authorization": f"Bearer {bearer_token}"}


def test_agent_app_logs_filters(agent) -> None:
    original_load_resource_map = agent._load_resource_map
    original_node_config = agent.node_config
    original_ssh_run = agent._ssh_run
    ssh_calls = []

    def fake_ssh_run(tailscale_ip, ssh_key, remote_cmd, user="root", timeout=15):
        ssh_calls.append(remote_cmd)
        return (0, "line one\nERROR something bad\nline three\n", "")

    try:
        agent._load_resource_map = lambda: {"gpu-app": {"kind": "zorc-agent", "node": "n1", "container_name": "c1"}}
        agent.node_config = lambda n: {"tailscale_ip": "10.0.0.1", "ssh_key": "deploy/secrets/fake",
                                        "ssh_user": "u"}
        agent._ssh_run = fake_ssh_run

        out = agent.app_logs("gpu-app", lines=50, since="1h")
        check("zorc-agent logs passes --tail and --since to docker",
              ssh_calls[-1] == ["docker", "logs", "--tail", "50", "--since", "1h", "c1"], f"got {ssh_calls[-1]}")

        out = agent.app_logs("gpu-app", grep="ERROR")
        check("grep filters client-side to only matching lines",
              out.strip() == "ERROR something bad", f"got {out!r}")

        out = agent.app_logs("gpu-app", grep="nope-not-present")
        check("grep with no matches says so instead of returning everything",
              "no lines matched" in out, f"got {out!r}")
    finally:
        agent._load_resource_map = original_load_resource_map
        agent.node_config = original_node_config
        agent._ssh_run = original_ssh_run


def test_agent_status_zorc_agent(agent) -> None:
    original_load_resource_map = agent._load_resource_map
    original_node_config = agent.node_config
    original_inspect = agent._zorc_agent_inspect
    original_stats_remote = agent._docker_stats_remote
    original_load_registry = agent.load_registry

    try:
        agent.load_registry = lambda: {"apps": [{"name": "gpu-app", "memory_mb": 4096}]}
        agent._load_resource_map = lambda: {"gpu-app": {"kind": "zorc-agent", "node": "n1", "container_name": "c1"}}
        agent.node_config = lambda n: {"tailscale_ip": "10.0.0.1", "ssh_key": "deploy/secrets/fake", "ssh_user": "u"}
        agent._zorc_agent_inspect = lambda ip, key, user, container: {
            "State": {"Status": "running", "StartedAt": "2026-08-22T00:00:00Z", "ExitCode": 0},
            "RestartCount": 7,
        }
        agent._docker_stats_remote = lambda ip, key, user: [
            {"name": "c1", "cpu_percent": 12.5, "mem_used_mb": 3900.0, "mem_limit_mb": 4096.0}
        ]

        status = agent.app_status("gpu-app")
        check("zorc-agent status reports real container state", status.get("status") == "running", f"{status}")
        check("zorc-agent status surfaces restart_count", status.get("restart_count") == 7, f"{status}")
        check("zorc-agent status reports real live memory usage", status.get("mem_used_mb") == 3900.0, f"{status}")
        check("zorc-agent status kind is correctly labeled (not 'coolify')",
              status.get("kind") == "zorc-agent", f"{status}")
    finally:
        agent._load_resource_map = original_load_resource_map
        agent.node_config = original_node_config
        agent._zorc_agent_inspect = original_inspect
        agent._docker_stats_remote = original_stats_remote
        agent.load_registry = original_load_registry


def test_deploy_history(m) -> None:
    tmp = Path(tempfile.mktemp(suffix=".log"))
    entries = [
        {"ts": "2026-08-20T10:00:00Z", "action": "deploy", "client": "admin-user",
         "params": {"name": "myapp"}, "outcome": {"status": "deployed"}},
        {"ts": "2026-08-21T10:00:00Z", "action": "restart", "client": "owner-client",
         "params": {"name": "myapp"}, "outcome": {"status": "restarted"}},
        {"ts": "2026-08-19T10:00:00Z", "action": "deploy", "client": "admin-user",
         "params": {"name": "other-app"}, "outcome": {"status": "deployed"}},
        "not even json",
        {"ts": "2026-08-22T10:00:00Z", "action": "redeploy", "client": "owner-client",
         "params": {"name": "myapp"}, "outcome": {"status": "rejected", "reason": "rate limit"}},
    ]
    with open(tmp, "w") as f:
        for e in entries:
            f.write((json.dumps(e) if isinstance(e, dict) else e) + "\n")

    original_path = m.AUDIT_LOG_PATH
    m.AUDIT_LOG_PATH = tmp
    try:
        history = m._read_deploy_history("myapp", limit=20)
        check("deploy history only includes entries for the named app",
              all(h["params"]["name"] == "myapp" for h in history) and len(history) == 3, f"{history}")
        check("deploy history is newest-first", history[0]["ts"] == "2026-08-22T10:00:00Z", f"{history}")
        check("deploy history survives a corrupt line without crashing", True)  # reaching here proves it

        limited = m._read_deploy_history("myapp", limit=2)
        check("limit is respected", len(limited) == 2, f"{limited}")
    finally:
        m.AUDIT_LOG_PATH = original_path
        tmp.unlink(missing_ok=True)


def test_mcp_tools_ownership_and_diagnosis(agent, m) -> None:
    admin_tok, owner_tok, other_tok = "admintok2", "ownertok2", "othertok2"
    tmp_tok = Path(tempfile.mktemp(suffix=".json"))
    tmp_tok.write_text(json.dumps({
        hashlib.sha256(admin_tok.encode()).hexdigest(): {"name": "admin-user", "role": "admin"},
        hashlib.sha256(owner_tok.encode()).hexdigest(): {"name": "owner-client", "role": "client"},
        hashlib.sha256(other_tok.encode()).hexdigest(): {"name": "other-client", "role": "client"},
    }))
    m.MCP_TOKEN_PATH = tmp_tok
    m._TOKEN_CACHE = {"mtime": None, "map": {}}

    fixture_registry = {"apps": [{"name": "crashy-app", "owner": "owner-client", "target": "local",
                                   "memory_mb": 512}]}
    original_load_registry = agent.load_registry
    agent.load_registry = lambda: fixture_registry

    original_app_status = agent.app_status
    original_app_logs = agent.app_logs

    audit_tmp = Path(tempfile.mktemp(suffix=".log"))
    original_audit_path = m.AUDIT_LOG_PATH
    m.AUDIT_LOG_PATH = audit_tmp

    with open(audit_tmp, "w") as f:
        f.write(json.dumps({"ts": "2026-08-22T09:00:00Z", "action": "deploy", "client": "owner-client",
                             "params": {"name": "crashy-app"}, "outcome": {"status": "deployed"}}) + "\n")

    def is_refused(tool, r) -> bool:
        if tool is m.get_app_logs:
            return isinstance(r, str) and r.startswith("(refused:")
        return isinstance(r, dict) and r.get("ok") is False

    def refusal_reason(tool, r) -> str:
        if tool is m.get_app_logs:
            return r
        return r.get("reason", "") if isinstance(r, dict) else ""

    try:
        for tool, args in [
            (m.get_app_status, ("crashy-app",)),
            (m.get_app_logs, ("crashy-app",)),
            (m.get_deploy_history, ("crashy-app",)),
            (m.diagnose_app, ("crashy-app",)),
        ]:
            agent.app_status = lambda n: {"kind": "coolify", "name": n, "status": "running", "memory_mb": 512, "mem_used_mb": 10}
            agent.app_logs = lambda n, *a, **kw: "all fine"

            r = tool(FakeCtx(other_tok), *args)
            check(f"{tool.__name__}: non-owner client is refused", is_refused(tool, r), f"got {r}")

            r = tool(FakeCtx(owner_tok), *args)
            check(f"{tool.__name__}: owner is allowed", not is_refused(tool, r), f"got {r}")

            r = tool(FakeCtx(admin_tok), *args)
            check(f"{tool.__name__}: admin is allowed on a non-owned app", not is_refused(tool, r), f"got {r}")

            r = tool(FakeCtx(owner_tok), "does-not-exist")
            check(f"{tool.__name__}: nonexistent app gives a clean refusal",
                  is_refused(tool, r) and "not a registered app" in refusal_reason(tool, r), f"got {r}")

        agent.app_status = lambda n: {
            "kind": "zorc-agent", "name": n, "status": "restarting", "restart_count": 9,
            "memory_mb": 512, "mem_used_mb": 40.0,
        }
        agent.app_logs = lambda n, *a, **kw: (
            "starting up...\n"
            "connecting to database...\n"
            "Error: DATABASE_URL is not set\n"
            "process exiting with code 1\n"
        )

        result = m.diagnose_app(FakeCtx(owner_tok), "crashy-app")
        check("diagnose_app returns findings for the crash-looping app", bool(result.get("findings")), f"{result}")

        signals = " | ".join(f["signal"] for f in result.get("findings", []))
        check("diagnose_app flags the container as not running",
              any("not running" in f["signal"] or "restarted" in f["signal"] for f in result["findings"]),
              signals)
        check("diagnose_app flags the high restart count",
              any("restarted 9 times" in f["signal"] for f in result["findings"]), signals)

        env_finding = next((f for f in result["findings"] if "environment variable" in f["signal"]), None)
        check("diagnose_app surfaces the missing-env-var log line specifically", env_finding is not None, signals)
        if env_finding:
            check("...and the surfaced detail actually names DATABASE_URL",
                  any("DATABASE_URL" in line for line in env_finding["detail"]), str(env_finding))

        check("diagnose_app's top finding is critical/high severity, not buried under 'info'",
              result["findings"][0]["severity"] in ("critical", "high"), str(result["findings"]))
    finally:
        agent.load_registry = original_load_registry
        agent.app_status = original_app_status
        agent.app_logs = original_app_logs
        m.AUDIT_LOG_PATH = original_audit_path
        tmp_tok.unlink(missing_ok=True)
        audit_tmp.unlink(missing_ok=True)


def main() -> int:
    import agent
    import mcp_server as m

    test_agent_app_logs_filters(agent)
    test_agent_status_zorc_agent(agent)
    test_deploy_history(m)
    test_mcp_tools_ownership_and_diagnosis(agent, m)

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
