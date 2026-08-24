#!/usr/bin/env python3
"""test_ownership.py — Phase 1 ownership pipeline test."""
import sys
from pathlib import Path

ZORC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ZORC_DIR / "deploy"))

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


FIXTURE_REGISTRY = {
    "apps": [
        {"name": "app-a", "owner": "client-a", "target": "local"},
        {"name": "app-b", "owner": "client-b", "target": "local"},
        {"name": "app-unowned", "target": "local"},  # no owner field at all
        {"name": "app-empty-owner", "owner": "", "target": "local"},  # owner present but empty
    ]
}


def main() -> int:
    import agent
    import mcp_server as m

    original_load_registry = agent.load_registry
    agent.load_registry = lambda: FIXTURE_REGISTRY
    try:
        client_a = {"name": "client-a", "role": "client"}
        client_b = {"name": "client-b", "role": "client"}
        admin = {"name": "admin-user", "role": "admin"}

        r = m._require_owner_or_admin(client_a, "app-a")
        check("owner can act on their own app", r.get("ok") is True and r.get("app", {}).get("name") == "app-a",
              f"got {r}")

        r = m._require_owner_or_admin(client_a, "app-b")
        check("client A cannot act on client B's app", r.get("ok") is False, f"got {r}")
        check("...and gives a reason, not just False", bool(r.get("reason")), f"got {r}")

        r = m._require_owner_or_admin(client_b, "app-a")
        check("client B cannot act on client A's app (symmetric)", r.get("ok") is False, f"got {r}")

        r = m._require_owner_or_admin(admin, "app-a")
        check("admin can act on client A's app", r.get("ok") is True, f"got {r}")
        r = m._require_owner_or_admin(admin, "app-b")
        check("admin can act on client B's app", r.get("ok") is True, f"got {r}")

        r = m._require_owner_or_admin(client_a, "does-not-exist")
        check("nonexistent app gives a clean refusal", r.get("ok") is False and "not a registered app" in r.get("reason", ""),
              f"got {r}")
        r = m._require_owner_or_admin(admin, "does-not-exist")
        check("nonexistent app refuses even admin (nothing to act on)", r.get("ok") is False, f"got {r}")

        r = m._require_owner_or_admin(client_a, "app-unowned")
        check("app with no owner field refuses a client", r.get("ok") is False, f"got {r}")
        r = m._require_owner_or_admin(admin, "app-unowned")
        check("app with no owner field still allows admin", r.get("ok") is True, f"got {r}")

        r = m._require_owner_or_admin(client_a, "app-empty-owner")
        check("app with empty-string owner refuses a client", r.get("ok") is False, f"got {r}")

        agent.load_registry = lambda: {"apps": [{"name": "app-c", "owner": "client-a-extra"}]}
        r = m._require_owner_or_admin(client_a, "app-c")
        check("owner match is exact, not prefix/substring", r.get("ok") is False, f"got {r}")

    finally:
        agent.load_registry = original_load_registry

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
