#!/usr/bin/env python3
"""test_set_env.py — set_app_env_vars pipeline test. Run in CI alongside
the other scripts/test_*.py.

Two layers, both entirely mocked/monkeypatched -- never touches the real
Coolify API, registry.yaml, or mcp_token.json:

  1. agent.set_coolify_env_vars()'s upsert logic and agent.set_app_env_vars()
     as pure logic: right refusal for a missing app / wrong kind / no
     uuid / empty env_vars, and that an existing key gets PATCHed (both
     is_preview copies) while a new key gets a single POST -- the real
     bug found live (Coolify 409s a POST for a key that already exists).
  2. mcp_server.py's set_app_env_vars() tool: ownership gate, rate limit,
     and that values never reach the audit log, only key names.

Usage:
    python3 scripts/test_set_env.py
"""
import hashlib
import json
import sys
import tempfile
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


class _FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def test_set_coolify_env_vars_upsert(agent) -> None:
    calls = []

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None):
            calls.append(("GET", url))
            return _FakeResponse(200, [{"key": "ALREADY_SET", "value": "old", "is_preview": False}])

        def post(self, url, headers=None, json=None):
            calls.append(("POST", json["key"], json["is_preview"]))
            return _FakeResponse(201, {"uuid": "new"})

        def patch(self, url, headers=None, json=None):
            calls.append(("PATCH", json["key"], json["is_preview"]))
            return _FakeResponse(201, {"uuid": "existing"})

    original_client = agent.httpx.Client
    agent.httpx.Client = FakeClient
    try:
        calls.clear()
        agent.set_coolify_env_vars("uuid1", {"ALREADY_SET": "new-value", "BRAND_NEW": "v"})
        check("existing key gets PATCHed for BOTH is_preview values",
              ("PATCH", "ALREADY_SET", False) in calls and ("PATCH", "ALREADY_SET", True) in calls,
              f"calls={calls}")
        check("existing key is never POSTed (would 409)",
              not any(c[0] == "POST" and c[1] == "ALREADY_SET" for c in calls), f"calls={calls}")
        check("new key gets a single POST, not PATCH",
              ("POST", "BRAND_NEW", False) in calls
              and not any(c[0] == "PATCH" and c[1] == "BRAND_NEW" for c in calls), f"calls={calls}")
    finally:
        agent.httpx.Client = original_client


def test_agent_set_app_env_vars(agent) -> None:
    original_name_taken = agent.name_taken
    original_load_resource_map = agent._load_resource_map
    original_set_vars = agent.set_coolify_env_vars
    original_trigger = agent.trigger_coolify_deploy

    set_calls, trigger_calls = [], []
    agent.set_coolify_env_vars = lambda uuid, vars: set_calls.append((uuid, dict(vars)))
    agent.trigger_coolify_deploy = lambda uuid: trigger_calls.append(uuid)

    try:
        agent.name_taken = lambda n: False
        try:
            agent.set_app_env_vars("ghost-app", {"K": "v"})
            check("set_app_env_vars on missing app refuses", False, "no exception raised")
        except ValueError as e:
            check("set_app_env_vars on missing app refuses", "not a registered app" in str(e), str(e))

        agent.name_taken = lambda n: True

        try:
            agent.set_app_env_vars("myapp", {})
            check("set_app_env_vars with empty env_vars refuses", False, "no exception raised")
        except ValueError as e:
            check("set_app_env_vars with empty env_vars refuses", "must not be empty" in str(e), str(e))

        for bad_kind in ("coolify-service", "zorc-agent", "pages", None):
            agent._load_resource_map = lambda k=bad_kind: {"myapp": {"kind": k, "coolify_uuid": "u1"}}
            set_calls.clear()
            try:
                agent.set_app_env_vars("myapp", {"K": "v"})
                check(f"set_app_env_vars refuses kind={bad_kind!r}", False, "no exception raised")
            except ValueError:
                check(f"set_app_env_vars refuses kind={bad_kind!r}", True)
            check(f"set_app_env_vars refuses kind={bad_kind!r} without touching Coolify",
                  set_calls == [], f"calls={set_calls}")

        agent._load_resource_map = lambda: {"myapp": {"kind": "coolify"}}
        try:
            agent.set_app_env_vars("myapp", {"K": "v"})
            check("set_app_env_vars refuses coolify app with no recorded uuid", False)
        except ValueError:
            check("set_app_env_vars refuses coolify app with no recorded uuid", True)

        agent._load_resource_map = lambda: {"myapp": {"kind": "coolify", "coolify_uuid": "the-real-uuid"}}
        set_calls.clear()
        trigger_calls.clear()
        result = agent.set_app_env_vars("myapp", {"API_KEY": "secret-value", "OTHER": "x"})
        check("set_app_env_vars sets exactly the given vars on the right app",
              set_calls == [("the-real-uuid", {"API_KEY": "secret-value", "OTHER": "x"})], f"calls={set_calls}")
        check("set_app_env_vars triggers a redeploy so it actually applies",
              trigger_calls == ["the-real-uuid"], f"calls={trigger_calls}")
        check("set_app_env_vars returns the touched key NAMES",
              result.get("keys") == ["API_KEY", "OTHER"], f"got {result}")
        check("set_app_env_vars never returns the actual VALUES",
              "secret-value" not in json.dumps(result), f"got {result}")
    finally:
        agent.name_taken = original_name_taken
        agent._load_resource_map = original_load_resource_map
        agent.set_coolify_env_vars = original_set_vars
        agent.trigger_coolify_deploy = original_trigger


def test_mcp_tool(agent, m) -> None:
    admin_tok, owner_tok, other_tok = "admintok5", "ownertok5", "othertok5"
    tmp = Path(tempfile.mktemp(suffix=".json"))
    tmp.write_text(json.dumps({
        hashlib.sha256(admin_tok.encode()).hexdigest(): {"name": "root-admin", "role": "admin"},
        hashlib.sha256(owner_tok.encode()).hexdigest(): {"name": "owner-client", "role": "client"},
        hashlib.sha256(other_tok.encode()).hexdigest(): {"name": "other-client", "role": "client"},
    }))
    m.MCP_TOKEN_PATH = tmp
    m._TOKEN_CACHE = {"mtime": None, "map": {}}

    audit_tmp = Path(tempfile.mktemp(suffix=".log"))
    original_audit_path = m.AUDIT_LOG_PATH
    m.AUDIT_LOG_PATH = audit_tmp

    fixture_registry = {"apps": [{"name": "app-x", "owner": "owner-client", "target": "local"}]}
    original_load_registry = agent.load_registry
    agent.load_registry = lambda: fixture_registry

    original_set_env = agent.set_app_env_vars
    calls = []
    agent.set_app_env_vars = lambda name, env_vars: (
        calls.append((name, dict(env_vars))), {"updated": name, "keys": sorted(env_vars.keys())})[1]

    m._set_env_timestamps.clear()

    try:
        calls.clear()
        r = m.set_app_env_vars(FakeCtx(other_tok), "app-x", {"K": "v"})
        check("set_app_env_vars: non-owner client is refused",
              r.get("status") == "rejected" or r.get("ok") is False, f"got {r}")
        check("...and agent.set_app_env_vars was never called", calls == [], f"{calls}")

        calls.clear()
        r = m.set_app_env_vars(FakeCtx(owner_tok), "app-x", {"REAL_SECRET": "sk-abc123", "PLAIN": "1"})
        check("set_app_env_vars: owner is allowed",
              calls == [("app-x", {"REAL_SECRET": "sk-abc123", "PLAIN": "1"})], f"got {r}, calls={calls}")
        check("response reports the touched keys, not values",
              r.get("keys") == ["PLAIN", "REAL_SECRET"], f"got {r}")

        audit_text = audit_tmp.read_text() if audit_tmp.exists() else ""
        check("the raw secret value never reaches the audit log", "sk-abc123" not in audit_text, audit_text)
        check("the key NAMES do reach the audit log (visibility, not secrecy)",
              "REAL_SECRET" in audit_text, audit_text)

        calls.clear()
        r = m.set_app_env_vars(FakeCtx(admin_tok), "app-x", {"K": "v"})
        check("set_app_env_vars: admin is allowed on a non-owned app", calls == [("app-x", {"K": "v"})], f"got {r}")

        r = m.set_app_env_vars(FakeCtx(owner_tok), "app-x", {})
        check("set_app_env_vars: empty env_vars is refused", r.get("status") == "rejected", f"got {r}")

        m._set_env_timestamps.clear()
        calls.clear()
        results = [m.set_app_env_vars(FakeCtx(owner_tok), "app-x", {"K": "v"}) for _ in range(m.SET_ENV_RATE_LIMIT + 2)]
        succeeded = sum(1 for r in results if r.get("status") == "updated")
        check(f"set_app_env_vars rate limit trips at exactly {m.SET_ENV_RATE_LIMIT}/hour",
              succeeded == m.SET_ENV_RATE_LIMIT, f"succeeded={succeeded} results={results}")
    finally:
        agent.load_registry = original_load_registry
        agent.set_app_env_vars = original_set_env
        m.AUDIT_LOG_PATH = original_audit_path
        tmp.unlink(missing_ok=True)
        audit_tmp.unlink(missing_ok=True)


def main() -> int:
    import agent
    import mcp_server as m

    test_set_coolify_env_vars_upsert(agent)
    test_agent_set_app_env_vars(agent)
    test_mcp_tool(agent, m)

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
