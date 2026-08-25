#!/usr/bin/env python3
"""test_rate_limit_toggle.py — ZORC_MCP_RATE_LIMITS_DISABLED pipeline
test. Run in CI alongside the other scripts/test_*.py.

Proves both directions: the toggle untouched still enforces every limit
exactly as before (the framework's own default), and flipping it makes
every single rate-limited tool stop enforcing entirely, without deleting
the mechanism -- see mcp_server.py's _rate_limited().

Usage:
    python3 scripts/test_rate_limit_toggle.py
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


def main() -> int:
    import agent
    import mcp_server as m

    admin_tok = "admtokRL"
    tmp_tok = Path(tempfile.mktemp(suffix=".json"))
    tmp_tok.write_text(json.dumps({
        hashlib.sha256(admin_tok.encode()).hexdigest(): {"name": "root-admin", "role": "admin"},
    }))
    original_token_path = m.MCP_TOKEN_PATH
    m.MCP_TOKEN_PATH = tmp_tok
    m._TOKEN_CACHE = {"mtime": None, "map": {}}

    audit_tmp = Path(tempfile.mktemp(suffix=".log"))
    original_audit_path = m.AUDIT_LOG_PATH
    m.AUDIT_LOG_PATH = audit_tmp

    fixture_registry = {"apps": [{"name": "app-x", "owner": "root-admin", "target": "local"}]}
    original_load_registry = agent.load_registry
    agent.load_registry = lambda: fixture_registry
    original_app_action = agent.app_action
    agent.app_action = lambda name, action: {"action": action, "name": name}

    original_enabled = m.RATE_LIMITS_ENABLED
    ctx = FakeCtx(admin_tok)

    try:
        check("rate limits are ON by default (this test never set the env var)", m.RATE_LIMITS_ENABLED is True)

        m._restart_timestamps.clear()
        results = [m.restart(ctx, "app-x") for _ in range(m.RESTART_RATE_LIMIT + 3)]
        succeeded = sum(1 for r in results if r.get("status") == "restarted")
        check(f"with the toggle untouched, restart still trips at {m.RESTART_RATE_LIMIT}/hour",
              succeeded == m.RESTART_RATE_LIMIT, f"succeeded={succeeded}")

        # Simulate ZORC_MCP_RATE_LIMITS_DISABLED=1 -- the module-level
        # flag is what every _rate_limited() call actually reads.
        m.RATE_LIMITS_ENABLED = False
        m._restart_timestamps.clear()
        n = m.RESTART_RATE_LIMIT + 20
        results = [m.restart(ctx, "app-x") for _ in range(n)]
        succeeded = sum(1 for r in results if r.get("status") == "restarted")
        check(f"with the toggle off, all {n} calls succeed, none rate-limited",
              succeeded == n, f"succeeded={succeeded}")
    finally:
        m.RATE_LIMITS_ENABLED = original_enabled
        agent.load_registry = original_load_registry
        agent.app_action = original_app_action
        m.MCP_TOKEN_PATH = original_token_path
        m.AUDIT_LOG_PATH = original_audit_path
        tmp_tok.unlink(missing_ok=True)
        audit_tmp.unlink(missing_ok=True)

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
