#!/usr/bin/env python3
"""test_memory_increase.py — request_memory_increase/approve_action
(memory_increase branch) pipeline test."""
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

    admin_tok, owner_tok, other_tok = "admintok4", "ownertok4", "othertok4"
    tmp_tok = Path(tempfile.mktemp(suffix=".json"))
    tmp_tok.write_text(json.dumps({
        hashlib.sha256(admin_tok.encode()).hexdigest(): {"name": "admin-user", "role": "admin"},
        hashlib.sha256(owner_tok.encode()).hexdigest(): {"name": "owner-client", "role": "client"},
        hashlib.sha256(other_tok.encode()).hexdigest(): {"name": "other-client", "role": "client"},
    }))
    m.MCP_TOKEN_PATH = tmp_tok
    m._TOKEN_CACHE = {"mtime": None, "map": {}}

    fixture_registry = {
        "apps": [{"name": "hungry-app", "owner": "owner-client", "target": "local", "memory_mb": 384}],
        "owner_budgets": {"default_mb": 2048, "overrides": {}},
    }
    original_load_registry = agent.load_registry
    agent.load_registry = lambda: fixture_registry

    original_owner_total = agent.owner_memory_total_mb
    agent.owner_memory_total_mb = lambda owner: sum(
        a["memory_mb"] for a in fixture_registry["apps"] if a["owner"] == owner
    )

    original_resize = agent.resize_app_memory
    resize_calls = []
    agent.resize_app_memory = lambda name, new_memory_mb: (
        resize_calls.append((name, new_memory_mb)),
        {"resized": name, "old_memory_mb": 384, "new_memory_mb": new_memory_mb},
    )[1]

    audit_tmp = Path(tempfile.mktemp(suffix=".log"))
    original_audit_path = m.AUDIT_LOG_PATH
    m.AUDIT_LOG_PATH = audit_tmp

    pending_tmp = Path(tempfile.mktemp(suffix=".json"))
    original_pending_path = m.PENDING_ACTIONS_PATH
    m.PENDING_ACTIONS_PATH = pending_tmp

    m._memory_increase_request_timestamps.clear()
    m._approve_action_timestamps.clear()
    m._reject_action_timestamps.clear()

    try:
        resize_calls.clear()
        r = m.request_memory_increase(FakeCtx(other_tok), "hungry-app", 768, "a real justification here")
        check("request_memory_increase: non-owner client is refused", r.get("ok") is False, f"got {r}")
        check("...and resize_app_memory was never called", resize_calls == [], f"{resize_calls}")

        r = m.request_memory_increase(FakeCtx(owner_tok), "hungry-app", 768, "short")
        check("reason under 10 chars is refused", r.get("status") == "rejected", f"got {r}")

        r = m.request_memory_increase(FakeCtx(owner_tok), "hungry-app", 100, "a real justification here")
        check("requested_memory_mb <= current is refused (increase-only)", r.get("status") == "rejected", f"got {r}")

        r = m.request_memory_increase(FakeCtx(owner_tok), "hungry-app", 384, "a real justification here")
        check("requesting the SAME value as current is refused too", r.get("status") == "rejected", f"got {r}")

        r = m.request_memory_increase(FakeCtx(owner_tok), "does-not-exist", 768, "a real justification here")
        check("request for a nonexistent app gives a clean refusal", r.get("ok") is False, f"got {r}")

        fixture_registry["apps"].append(
            {"name": "already-big", "owner": "owner-client", "target": "local", "memory_mb": 1792})
        r = m.request_memory_increase(FakeCtx(owner_tok), "hungry-app", 768, "would push me over my cap")
        check("increase that would exceed the owner's platform-wide budget is refused",
              r.get("status") == "rejected" and "owner_budget_mb" in r, f"got {r}")
        check("...and resize_app_memory was never called", resize_calls == [], f"{resize_calls}")

        r = m.request_memory_increase(FakeCtx(admin_tok), "hungry-app", 768, "admin is exempt from owner budget")
        check("admin caller is exempt from the owner budget gate", r.get("status") == "requested", f"got {r}")
        admin_request_id = r["id"]
        m.reject_action(FakeCtx(admin_tok), admin_request_id)  # clean up so the next real test starts fresh
        fixture_registry["apps"].pop()  # remove "already-big" again

        r = m.request_memory_increase(FakeCtx(owner_tok), "hungry-app", 768, "load testing needs more headroom")
        check("owner can request a genuine increase", r.get("status") == "requested", f"got {r}")
        check("...and resize_app_memory STILL was never called (only queued)", resize_calls == [], f"{resize_calls}")
        request_id = r.get("id")
        check("request_memory_increase returns a usable id", bool(request_id), f"got {r}")
        check("response reports current and requested memory",
              r.get("current_memory_mb") == 384 and r.get("requested_memory_mb") == 768, f"got {r}")

        r2 = m.request_memory_increase(FakeCtx(owner_tok), "hungry-app", 1024, "trying to double-request")
        check("a second request for the same app returns the existing id, not a new one",
              r2.get("status") == "already_pending" and r2.get("id") == request_id, f"got {r2}")

        listing = m.list_pending_actions(FakeCtx(owner_tok))
        check("owner sees their own pending request", any(a["id"] == request_id for a in listing["actions"]),
              f"{listing}")
        listing_other = m.list_pending_actions(FakeCtx(other_tok))
        check("a different client does NOT see someone else's pending request",
              not any(a["id"] == request_id for a in listing_other["actions"]), f"{listing_other}")

        resize_calls.clear()
        r = m.approve_action(FakeCtx(owner_tok), request_id)
        check("approve_action: the REQUESTER (a client, not admin) cannot approve their own request",
              r.get("status") == "rejected" and "admin-only" in r.get("reason", ""), f"got {r}")
        check("...and resize_app_memory was never called", resize_calls == [], f"{resize_calls}")

        r = m.approve_action(FakeCtx(admin_tok), request_id)
        check("approve_action: admin CAN approve -- executes exactly once with the right args",
              r.get("status") == "executed" and resize_calls == [("hungry-app", 768)], f"got {r}, calls={resize_calls}")

        resize_calls.clear()
        r = m.approve_action(FakeCtx(admin_tok), request_id)
        check("re-approving an already-executed id refuses, does not re-resize",
              r.get("status") == "rejected" and "already" in r.get("reason", "") and resize_calls == [],
              f"got {r}, calls={resize_calls}")

        fixture_registry["apps"][0]["memory_mb"] = 768  # reflect the "applied" increase for the next request
        r = m.request_memory_increase(FakeCtx(owner_tok), "hungry-app", 1536, "second increase")
        check("can request again after the prior request was executed (not stuck pending forever)",
              r.get("status") == "requested", f"got {r}")
        request_id_2 = r.get("id")

        resize_calls.clear()
        r = m.reject_action(FakeCtx(owner_tok), request_id_2)
        check("reject_action: non-admin (even the requester) cannot reject", r.get("status") == "rejected", f"got {r}")

        r = m.reject_action(FakeCtx(admin_tok), request_id_2)
        check("reject_action: admin can reject", r.get("status") == "action_rejected", f"got {r}")
        check("...and resize_app_memory was never called for a rejected request", resize_calls == [], f"{resize_calls}")

        r = m.approve_action(FakeCtx(admin_tok), request_id_2)
        check("cannot approve an already-rejected id", r.get("status") == "rejected" and resize_calls == [],
              f"got {r}")

        r = m.request_memory_increase(FakeCtx(owner_tok), "hungry-app", 1536, "third increase, will fail")
        request_id_3 = r["id"]
        agent.resize_app_memory = lambda name, new_memory_mb: (_ for _ in ()).throw(
            RuntimeError("not enough live headroom on local"))
        r = m.approve_action(FakeCtx(admin_tok), request_id_3)
        check("a failing resize surfaces as status=failed, not a crash",
              r.get("status") == "failed" and "headroom" in r.get("reason", ""), f"got {r}")
        entry = m._load_pending_actions()[request_id_3]
        check("the failure is recorded on the queue entry itself", entry.get("status") == "failed", f"{entry}")

        agent.resize_app_memory = lambda name, new_memory_mb: (
            resize_calls.append((name, new_memory_mb)), {"resized": name, "new_memory_mb": new_memory_mb})[1]
        m._memory_increase_request_timestamps.clear()
        results = []
        for i in range(m.MEMORY_INCREASE_REQUEST_RATE_LIMIT + 2):
            fixture_registry["apps"].append(
                {"name": f"rl-app-{i}", "owner": "owner-client", "target": "local", "memory_mb": 128})
            results.append(m.request_memory_increase(FakeCtx(owner_tok), f"rl-app-{i}", 256, "rate limit probe"))
        succeeded = sum(1 for r in results if r.get("status") == "requested")
        check(f"request_memory_increase rate limit trips at exactly {m.MEMORY_INCREASE_REQUEST_RATE_LIMIT}/hour",
              succeeded == m.MEMORY_INCREASE_REQUEST_RATE_LIMIT, f"succeeded={succeeded} results={results}")
    finally:
        agent.load_registry = original_load_registry
        agent.owner_memory_total_mb = original_owner_total
        agent.resize_app_memory = original_resize
        m.AUDIT_LOG_PATH = original_audit_path
        m.PENDING_ACTIONS_PATH = original_pending_path
        tmp_tok.unlink(missing_ok=True)
        audit_tmp.unlink(missing_ok=True)
        pending_tmp.unlink(missing_ok=True)

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
