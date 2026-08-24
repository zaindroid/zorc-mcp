#!/usr/bin/env python3
"""test_owner_budgets.py — Phase 5 pipeline test (soft per-owner memory
budgets)."""
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
    def __init__(self, name: str, role: str):
        self.name, self.role = name, role


def test_owner_memory_total_and_budget(agent) -> None:
    original_load_registry = agent.load_registry
    try:
        agent.load_registry = lambda: {
            "owner_budgets": {"default_mb": 8192, "overrides": {"big-client": 20000}},
            "apps": [
                {"name": "a1", "owner": "multi-app-client", "memory_mb": 512},
                {"name": "a2", "owner": "multi-app-client", "memory_mb": 1024},
                {"name": "a3", "owner": "multi-app-client", "memory_mb": 256},
                {"name": "a4", "owner": "someone-else", "memory_mb": 99999},
            ],
        }
        check("owner_memory_total_mb sums correctly across multiple owned apps",
              agent.owner_memory_total_mb("multi-app-client") == 512 + 1024 + 256,
              str(agent.owner_memory_total_mb("multi-app-client")))
        check("owner_memory_total_mb ignores other owners' apps entirely",
              agent.owner_memory_total_mb("nobody-owns-this") == 0,
              str(agent.owner_memory_total_mb("nobody-owns-this")))
        check("owner_budget_mb returns default_mb when no override exists",
              agent.owner_budget_mb("multi-app-client") == 8192, str(agent.owner_budget_mb("multi-app-client")))
        check("owner_budget_mb returns the override when one exists",
              agent.owner_budget_mb("big-client") == 20000, str(agent.owner_budget_mb("big-client")))
    finally:
        agent.load_registry = original_load_registry


def test_analyze_deployment_requirements_budget_gate(agent, m) -> None:
    original = {
        "load_registry": agent.load_registry,
        "clone_repo": agent.clone_repo,
        "classify": agent.classify,
        "parse_app_yaml": agent.parse_app_yaml,
        "estimate": m._estimate_memory_from_repo,
        "placement": m._recommend_placement,
        "caller_identity": m._caller_identity,
        "rmtree": __import__("shutil").rmtree,
    }

    fixture_registry = {"owner_budgets": {"default_mb": 8192, "overrides": {"roomy-client": 20000}}, "apps": []}

    def set_owner_apps(owner: str, total_mb: int):
        fixture_registry["apps"] = [{"name": f"{owner}-existing", "owner": owner, "memory_mb": total_mb}] if total_mb else []

    agent.load_registry = lambda: fixture_registry
    agent.clone_repo = lambda owner_repo, git_branch="main": Path("/tmp/fake-repo-does-not-need-to-exist")
    agent.classify = lambda repo_dir: {"kind": "python", "language": "python", "reason": "requirements.txt found"}
    agent.parse_app_yaml = lambda repo_dir: {"env": {}, "database": False, "persistent_storage": None}
    m._estimate_memory_from_repo = lambda repo_dir, classification: (512, [])
    m._recommend_placement = lambda memory_mb, needs_public_ip, needs_gpu=False: {
        "recommended_node": "local", "fits": True, "reason": "plenty of headroom",
        "candidates_considered": {},
    }
    __import__("shutil").rmtree = lambda *a, **kw: None

    def call(name: str, role: str) -> dict:
        m._caller_identity = lambda ctx: {"name": name, "role": role}
        return m.analyze_deployment_requirements(
            ctx=object(),  # unused -- _caller_identity is monkeypatched above
            owner_repo="someorg/somerepo", architecture="single_service", app_kind="api",
            frontend_rendering="none", framework="fastapi", expected_concurrency="low",
            has_database=False, has_background_jobs=False, needs_websockets=False,
            needs_persistent_storage=False, needs_public_ip=False, needs_gpu=False,
            estimated_memory_mb=512, reasoning="a plain FastAPI service, 512MB is the standard baseline for this",
        )

    try:
        set_owner_apps("fresh-client", 0)
        r = call("fresh-client", "client")
        check("client with no existing apps and a small estimate is approved",
              r.get("status") == "approved", f"got {r}")

        set_owner_apps("near-cap-client", 7800)  # + 512 new = 8312 > 8192 default
        r = call("near-cap-client", "client")
        check("client pushed over their cap by this deploy is blocked",
              r.get("status") == "blocked", f"got {r}")
        check("...with a readable reason naming the actual numbers",
              all(str(n) in r.get("reason", "") for n in (7800, 512, 8312, 8192)), f"got {r}")
        check("...and reports the correct current/cap numbers as structured fields",
              r.get("owner_current_total_mb") == 7800 and r.get("owner_budget_mb") == 8192, f"got {r}")

        set_owner_apps("roomy-client", 7800)
        r = call("roomy-client", "client")
        check("a client with a raised override is NOT blocked at the same total",
              r.get("status") == "approved", f"got {r}")

        set_owner_apps("admin-user", 999999)
        r = call("admin-user", "admin")
        check("admin is exempt from the budget check entirely, no matter their existing total",
              r.get("status") == "approved", f"got {r}")

        set_owner_apps("exact-client", 8192 - 512)  # + 512 new = exactly 8192
        r = call("exact-client", "client")
        check("landing exactly ON the cap is approved, not blocked (over, not at-or-over)",
              r.get("status") == "approved", f"got {r}")
    finally:
        agent.load_registry = original["load_registry"]
        agent.clone_repo = original["clone_repo"]
        agent.classify = original["classify"]
        agent.parse_app_yaml = original["parse_app_yaml"]
        m._estimate_memory_from_repo = original["estimate"]
        m._recommend_placement = original["placement"]
        m._caller_identity = original["caller_identity"]
        __import__("shutil").rmtree = original["rmtree"]


def test_fresh_registry_with_no_apps_yet(agent) -> None:
    """A bare "apps:" with nothing under it (the real, exact shape of a
    freshly generated registry.yaml, before the first app is ever
    deployed) parses as apps=None, not apps=[] -- this used to crash the
    very first call any new install ever made. Written against a real
    file and the real load_registry(), not a mocked-out dict."""
    fixture = Path(tempfile.mktemp(suffix=".yaml"))
    fixture.write_text(
        "nodes:\n"
        "  local:\n"
        "    total_memory_mb: 16000\n"
        "    reserved_mb: 2000\n"
        "    usable_mb: 14000\n"
        "    max_utilisation: 0.8\n"
        "owner_budgets:\n"
        "  default_mb: 8192\n"
        "  overrides: {}\n"
        "apps:"
    )
    original_path = agent.REGISTRY_PATH
    try:
        agent.REGISTRY_PATH = fixture
        reg = agent.load_registry()
        check("a bare apps: line loads as an empty list, not None", reg["apps"] == [], f"got {reg['apps']!r}")
        check("budget_headroom_mb works against it", agent.budget_headroom_mb("local") == 11200.0,
              f"got {agent.budget_headroom_mb('local')}")
        check("owner_memory_total_mb works against it", agent.owner_memory_total_mb("anyone") == 0)
        check("name_taken works against it", agent.name_taken("anything") is False)
    finally:
        agent.REGISTRY_PATH = original_path
        fixture.unlink(missing_ok=True)


def main() -> int:
    import agent
    import mcp_server as m

    test_owner_memory_total_and_budget(agent)
    test_analyze_deployment_requirements_budget_gate(agent, m)
    test_fresh_registry_with_no_apps_yet(agent)

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
