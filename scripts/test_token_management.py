#!/usr/bin/env python3
"""test_token_management.py — list_clients/mint_client_token/
revoke_client_token pipeline test.

Never touches the real deploy/secrets/mcp_token.json or mcp_audit.log --
both point at throwaway temp files for the duration of this run.

Usage:
    python3 scripts/test_token_management.py
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
    import mcp_server as m

    admin_tok, client_tok, other_admin_tok = "admtok1", "clitok1", "admtok2"
    tmp_tok = Path(tempfile.mktemp(suffix=".json"))
    tmp_tok.write_text(json.dumps({
        hashlib.sha256(admin_tok.encode()).hexdigest(): {"name": "root-admin", "role": "admin"},
        hashlib.sha256(client_tok.encode()).hexdigest(): {"name": "some-client", "role": "client"},
        hashlib.sha256(other_admin_tok.encode()).hexdigest(): {"name": "second-admin", "role": "admin"},
    }))
    original_token_path = m.MCP_TOKEN_PATH
    m.MCP_TOKEN_PATH = tmp_tok
    m._TOKEN_CACHE = {"mtime": None, "map": {}}

    audit_tmp = Path(tempfile.mktemp(suffix=".log"))
    original_audit_path = m.AUDIT_LOG_PATH
    m.AUDIT_LOG_PATH = audit_tmp

    m._mint_token_timestamps.clear()
    m._revoke_token_timestamps.clear()

    admin_ctx = FakeCtx(admin_tok)
    client_ctx = FakeCtx(client_tok)

    try:
        # list_clients: open to any authenticated caller, not admin-gated
        r = m.list_clients(client_ctx)
        check("list_clients: a plain client can see the roster too", len(r.get("clients", [])) == 3, f"got {r}")
        check("list_clients: never leaks a token or hash",
              "token" not in json.dumps(r) and admin_tok not in json.dumps(r))

        # mint_client_token: NOT admin-gated -- reaching the tool at all
        # already means holding a valid token, that's the trust boundary.
        r = m.mint_client_token(client_ctx, name="new-client", role="client")
        check("mint_client_token: a plain client can mint too, not just admin",
              r.get("status") == "minted" and "token" in r, f"got {r}")
        new_token = r["token"]

        new_ctx = FakeCtx(new_token)
        who = m.whoami(new_ctx)
        check("newly minted token actually authenticates as the right identity",
              who == {"name": "new-client", "role": "client"}, f"got {who}")

        r = m.mint_client_token(admin_ctx, name="", role="client")
        check("mint_client_token: empty name refused", r.get("status") == "rejected", f"got {r}")

        r = m.mint_client_token(client_ctx, name="self-promoted", role="admin")
        check("a plain client can mint itself an ADMIN-role token -- deliberate, not a bug: "
              "reaching this tool at all already required holding a valid token",
              r.get("status") == "minted" and r.get("role") == "admin", f"got {r}")

        r = m.mint_client_token(admin_ctx, name="new-client", role="admin")
        check("re-minting an existing name rotates it", r.get("status") == "rotated", f"got {r}")
        old_ctx = FakeCtx(new_token)
        try:
            m.whoami(old_ctx)
            old_token_still_works = True
        except PermissionError:
            old_token_still_works = False
        check("the OLD token for that name no longer works after rotation", not old_token_still_works)

        audit_text = audit_tmp.read_text() if audit_tmp.exists() else ""
        check("mint_client_token's audit entries never contain the raw token value",
              new_token not in audit_text)

        # revoke_client_token
        r = m.revoke_client_token(client_ctx, name="some-client")
        check("revoke_client_token: non-admin refused", r.get("status") == "rejected", f"got {r}")

        r = m.revoke_client_token(admin_ctx, name="does-not-exist")
        check("revoke_client_token: unknown name refused cleanly", r.get("status") == "rejected", f"got {r}")

        r = m.revoke_client_token(admin_ctx, name="some-client")
        check("revoke_client_token: admin can revoke a client", r.get("status") == "revoked", f"got {r}")
        r = m.list_clients(admin_ctx)
        names = [c["name"] for c in r["clients"]]
        check("revoked client no longer appears in list_clients", "some-client" not in names, f"got {names}")

        # last-admin protection -- also removes "new-client" and
        # "self-promoted", both minted as role=admin above, so root-admin
        # is actually the sole remaining admin by the time this matters.
        r = m.revoke_client_token(admin_ctx, name="new-client")
        check("cleanup: removing the extra admin from the earlier re-mint step",
              r.get("status") == "revoked", f"got {r}")
        r = m.revoke_client_token(admin_ctx, name="self-promoted")
        check("cleanup: removing the self-promoted admin too", r.get("status") == "revoked", f"got {r}")

        r = m.revoke_client_token(admin_ctx, name="second-admin")
        check("revoking a non-last admin (root-admin still exists) succeeds",
              r.get("status") == "revoked", f"got {r}")

        r = m.revoke_client_token(admin_ctx, name="root-admin")
        check("revoking the LAST remaining admin is refused", r.get("status") == "rejected", f"got {r}")
        check("...with a clear reason naming the risk", "last remaining admin" in r.get("reason", ""), f"got {r}")
        r = m.list_clients(admin_ctx)
        names = [c["name"] for c in r["clients"]]
        check("root-admin still there after the refused self-lockout attempt", "root-admin" in names, f"got {names}")

        # rate limits
        for i in range(m.MINT_TOKEN_RATE_LIMIT + 2):
            r = m.mint_client_token(admin_ctx, name=f"rl-{i}", role="client")
        check(f"mint_client_token rate limit trips at exactly {m.MINT_TOKEN_RATE_LIMIT}/hour",
              r.get("status") == "rejected" and "rate limit" in r.get("reason", ""), f"got {r}")
    finally:
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
