#!/usr/bin/env python3
"""test_teardown.py — Phase 4b pipeline test (request_teardown/
approve_action/reject_action/list_pending_actions)."""
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

    admin_tok, owner_tok, other_tok = "admintok3", "ownertok3", "othertok3"
    tmp_tok = Path(tempfile.mktemp(suffix=".json"))
    tmp_tok.write_text(json.dumps({
        hashlib.sha256(admin_tok.encode()).hexdigest(): {"name": "admin-user", "role": "admin"},
        hashlib.sha256(owner_tok.encode()).hexdigest(): {"name": "owner-client", "role": "client"},
        hashlib.sha256(other_tok.encode()).hexdigest(): {"name": "other-client", "role": "client"},
    }))
    m.MCP_TOKEN_PATH = tmp_tok
    m._TOKEN_CACHE = {"mtime": None, "map": {}}

    fixture_registry = {"apps": [{"name": "doomed-app", "owner": "owner-client", "target": "local"}]}
    original_load_registry = agent.load_registry
    agent.load_registry = lambda: fixture_registry

    original_delete_app = agent.delete_app
    delete_calls = []
    agent.delete_app = lambda name: (delete_calls.append(name), {"deleted": name, "kind": "coolify"})[1]

    audit_tmp = Path(tempfile.mktemp(suffix=".log"))
    original_audit_path = m.AUDIT_LOG_PATH
    m.AUDIT_LOG_PATH = audit_tmp

    pending_tmp = Path(tempfile.mktemp(suffix=".json"))
    original_pending_path = m.PENDING_ACTIONS_PATH
    m.PENDING_ACTIONS_PATH = pending_tmp

    m._teardown_request_timestamps.clear()
    m._approve_action_timestamps.clear()
    m._reject_action_timestamps.clear()

    try:
        delete_calls.clear()
        r = m.request_teardown(FakeCtx(other_tok), "doomed-app")
        check("request_teardown: non-owner client is refused", r.get("ok") is False, f"got {r}")
        check("...and delete_app was never called", delete_calls == [], f"{delete_calls}")

        r = m.request_teardown(FakeCtx(owner_tok), "doomed-app")
        check("request_teardown: owner can request", r.get("status") == "requested", f"got {r}")
        check("...and delete_app STILL was never called (only queued)", delete_calls == [], f"{delete_calls}")
        request_id = r.get("id")
        check("request_teardown returns a usable id", bool(request_id), f"got {r}")

        r2 = m.request_teardown(FakeCtx(owner_tok), "doomed-app")
        check("a second request for the same app returns the existing id, not a new one",
              r2.get("status") == "already_pending" and r2.get("id") == request_id, f"got {r2}")

        r = m.request_teardown(FakeCtx(owner_tok), "does-not-exist")
        check("request_teardown on a nonexistent app gives a clean refusal",
              r.get("ok") is False and "not a registered app" in r.get("reason", ""), f"got {r}")

        listing = m.list_pending_actions(FakeCtx(owner_tok))
        check("owner sees their own pending request", any(a["id"] == request_id for a in listing["actions"]),
              f"{listing}")
        listing_other = m.list_pending_actions(FakeCtx(other_tok))
        check("a different client does NOT see someone else's pending request",
              not any(a["id"] == request_id for a in listing_other["actions"]), f"{listing_other}")
        listing_admin = m.list_pending_actions(FakeCtx(admin_tok))
        check("admin sees every pending request", any(a["id"] == request_id for a in listing_admin["actions"]),
              f"{listing_admin}")

        delete_calls.clear()
        r = m.approve_action(FakeCtx(owner_tok), request_id)
        check("approve_action: the REQUESTER (a client, not admin) cannot approve their own request",
              r.get("status") == "rejected" and "admin-only" in r.get("reason", ""), f"got {r}")
        check("...and delete_app was never called", delete_calls == [], f"{delete_calls}")

        r = m.approve_action(FakeCtx(admin_tok), "not-a-real-id")
        check("approve_action refuses an unknown id cleanly", r.get("status") == "rejected", f"got {r}")
        check("...without calling delete_app", delete_calls == [], f"{delete_calls}")

        r = m.approve_action(FakeCtx(admin_tok), request_id)
        check("approve_action: admin CAN approve -- executes exactly once",
              r.get("status") == "executed" and delete_calls == ["doomed-app"], f"got {r}, calls={delete_calls}")

        delete_calls.clear()
        r = m.approve_action(FakeCtx(admin_tok), request_id)
        check("re-approving an already-executed id refuses, does not re-delete",
              r.get("status") == "rejected" and "already" in r.get("reason", "") and delete_calls == [],
              f"got {r}, calls={delete_calls}")

        r = m.request_teardown(FakeCtx(owner_tok), "doomed-app")
        check("can request again after the prior request was executed (not stuck pending forever)",
              r.get("status") == "requested", f"got {r}")
        request_id_2 = r.get("id")

        delete_calls.clear()
        r = m.reject_action(FakeCtx(owner_tok), request_id_2)
        check("reject_action: non-admin (even the requester) cannot reject", r.get("status") == "rejected", f"got {r}")

        r = m.reject_action(FakeCtx(admin_tok), request_id_2)
        check("reject_action: admin can reject", r.get("status") == "action_rejected", f"got {r}")
        check("...and delete_app was never called for a rejected request", delete_calls == [], f"{delete_calls}")

        r = m.approve_action(FakeCtx(admin_tok), request_id_2)
        check("cannot approve an already-rejected id", r.get("status") == "rejected" and delete_calls == [],
              f"got {r}")

        r = m.request_teardown(FakeCtx(owner_tok), "doomed-app")
        request_id_3 = r["id"]
        agent.delete_app = lambda name: (_ for _ in ()).throw(RuntimeError("Coolify API returned 500"))
        r = m.approve_action(FakeCtx(admin_tok), request_id_3)
        check("a failing delete_app surfaces as status=failed, not a crash",
              r.get("status") == "failed" and "500" in r.get("reason", ""), f"got {r}")
        entry = m._load_pending_actions()[request_id_3]
        check("the failure is recorded on the queue entry itself", entry.get("status") == "failed", f"{entry}")

        agent.delete_app = lambda name: (delete_calls.append(name), {"deleted": name})[1]
        m._teardown_request_timestamps.clear()
        results = []
        for i in range(m.TEARDOWN_REQUEST_RATE_LIMIT + 2):
            fixture_registry["apps"].append({"name": f"app-{i}", "owner": "owner-client", "target": "local"})
            results.append(m.request_teardown(FakeCtx(owner_tok), f"app-{i}"))
        succeeded = sum(1 for r in results if r.get("status") == "requested")
        check(f"request_teardown rate limit trips at exactly {m.TEARDOWN_REQUEST_RATE_LIMIT}/hour",
              succeeded == m.TEARDOWN_REQUEST_RATE_LIMIT, f"succeeded={succeeded} results={results}")
    finally:
        agent.load_registry = original_load_registry
        agent.delete_app = original_delete_app
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
