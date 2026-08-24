#!/usr/bin/env python3
"""test_mcp_auth.py — Phase 0 auth pipeline test."""
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ZORC_DIR = Path(__file__).resolve().parent.parent
DEPLOY_DIR = ZORC_DIR / "deploy"
VENV_PY = ZORC_DIR / "monitoring" / ".venv" / "bin" / "python3"
PYTHON = str(VENV_PY) if VENV_PY.exists() else sys.executable

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_token_file(path: Path, entries: dict[str, dict]) -> None:
    """entries: {name: role} -- generates a fresh random token per entry,
    writes the hash map, returns nothing (tokens are returned separately
    by the caller building this dict itself -- see main())."""
    path.write_text(json.dumps(entries))


def wait_healthy(port: int, proc: subprocess.Popen, timeout: float = 10.0) -> bool:
    import httpx
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False  # process already exited -- never became healthy
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def spawn_server(token_path: Path, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["ZORC_MCP_TOKEN_PATH"] = str(token_path)
    env["ZORC_MCP_ALLOWED_HOSTS"] = f"127.0.0.1:{port},localhost:{port}"
    env["ZORC_MCP_AUDIT_LOG_PATH"] = str(Path(tempfile.mktemp(suffix=".log")))
    return subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "mcp_server:app", "--host", "127.0.0.1", "--port", str(port),
         "--app-dir", str(DEPLOY_DIR)],  # matches production's systemd unit invocation exactly
        cwd=str(DEPLOY_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def rpc(port: int, method: str, params: dict | None, *, token: str | None,
        session_id: str | None, msg_id: int | None = 1,
        raw_auth_header: str | None = None) -> tuple[int, dict | None, str | None]:
    """One JSON-RPC call over the real /mcp endpoint."""
    import httpx
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if raw_auth_header is not None:
        headers["Authorization"] = raw_auth_header
    elif token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["mcp-session-id"] = session_id
    body = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        body["id"] = msg_id
    if params is not None:
        body["params"] = params
    r = httpx.post(f"http://127.0.0.1:{port}/mcp", headers=headers, json=body, timeout=10.0)
    parsed = None
    text = r.text
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                parsed = json.loads(line[len("data:"):].strip())
            except json.JSONDecodeError:
                pass
            break
    if parsed is None and r.headers.get("content-type", "").startswith("application/json") and text.strip():
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            pass
    return r.status_code, parsed, r.headers.get("mcp-session-id")


def full_session(port: int, token: str) -> str:
    """initialize + notifications/initialized -> returns the session id."""
    status, body, session_id = rpc(
        port, "initialize",
        {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test_mcp_auth", "version": "0"}},
        token=token, session_id=None, msg_id=1,
    )
    if status != 200 or not session_id:
        raise RuntimeError(f"initialize failed: status={status} body={body}")
    status, _, _ = rpc(port, "notifications/initialized", None, token=token, session_id=session_id, msg_id=None)
    if status != 202:
        raise RuntimeError(f"notifications/initialized failed: status={status}")
    return session_id


_SAFE_TO_ACTUALLY_CALL = {"whoami", "get_platform_contract"}


def test_valid_tokens_reach_every_tool(port: int, admin_token: str, admin_name: str,
                                        client_token: str, client_name: str) -> None:
    for role_label, token, expected_name, expected_role in (
        ("admin", admin_token, admin_name, "admin"),
        ("client", client_token, client_name, "client"),
    ):
        session_id = full_session(port, token)
        status, body, _ = rpc(port, "tools/list", None, token=token, session_id=session_id, msg_id=2)
        check(f"tools/list succeeds for {role_label} token", status == 200 and body and "result" in body,
              f"status={status} body={body}")
        tool_names = sorted(t["name"] for t in body["result"]["tools"]) if body else []
        check(f"{role_label}: at least whoami + deploy are registered",
              {"whoami", "deploy"}.issubset(set(tool_names)), f"got {tool_names}")

        msg_id = 3
        for name in sorted(_SAFE_TO_ACTUALLY_CALL & set(tool_names)):
            status, body, _ = rpc(port, "tools/call", {"name": name, "arguments": {}},
                                   token=token, session_id=session_id, msg_id=msg_id)
            msg_id += 1
            check(f"{role_label} token can actually invoke '{name}' (not 401)", status != 401,
                  f"status={status}")

        status, body, _ = rpc(port, "tools/call", {"name": "whoami", "arguments": {}},
                               token=token, session_id=session_id, msg_id=msg_id)
        resolved = None
        try:
            resolved = json.loads(body["result"]["content"][0]["text"])
        except Exception:
            pass
        check(f"whoami resolves correct identity for {role_label} token",
              resolved == {"name": expected_name, "role": expected_role},
              f"got {resolved}, expected name={expected_name!r} role={expected_role!r}")


def test_no_identity_spoofing(port: int, client_token: str, client_name: str) -> None:
    """A caller can't override who they are by putting a name/role in the
    tool arguments -- identity comes ONLY from the bearer token."""
    session_id = full_session(port, client_token)
    status, body, _ = rpc(
        port, "tools/call",
        {"name": "whoami", "arguments": {"name": "admin", "role": "admin", "client_name": "admin"}},
        token=client_token, session_id=session_id, msg_id=99,
    )
    resolved = None
    try:
        resolved = json.loads(body["result"]["content"][0]["text"])
    except Exception:
        pass
    check("caller-supplied name/role fields cannot spoof identity",
          resolved == {"name": client_name, "role": "client"},
          f"got {resolved} -- possible spoofing if this shows role=admin or name=admin")


def test_invalid_tokens_always_401(port: int, admin_token: str) -> None:
    session_id = full_session(port, admin_token)  # a real session, to prove reuse doesn't bypass auth
    cases = [
        ("missing (no header)", None, None),
        ("wrong scheme (exercises the empty-token path)", None, "Basic dXNlcjpwYXNz"),
        ("garbage", "not-a-real-token-" + secrets.token_hex(8), None),
        ("all-zeros (looks token-shaped, isn't real)", "0" * 64, None),
    ]
    for label, bad_token, raw_header in cases:
        for method, params in (("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "x", "version": "0"}}),
                                ("tools/list", None),
                                ("tools/call", {"name": "whoami", "arguments": {}})):
            status, body, _ = rpc(port, method, params, token=bad_token, session_id=session_id, msg_id=1,
                                   raw_auth_header=raw_header)
            check(f"{method} with {label} token -> 401", status == 401, f"status={status} body={body}")


def test_boot_refuses_on_malformed_token_file() -> None:
    cases = {
        "not JSON at all": "not valid json {{{",
        "JSON but not an object": json.dumps(["a", "list", "not", "a", "map"]),
        "object with wrong-shaped entry": json.dumps({"abc123": "just a string, not {name,role}"}),
        "object with bad role": json.dumps({"abc123": {"name": "x", "role": "superadmin"}}),
        "missing file entirely": None,
    }
    for label, content in cases.items():
        tmp_path = Path(tempfile.mktemp(suffix=".json"))
        if content is not None:
            tmp_path.write_text(content)
        port = free_port()
        proc = spawn_server(tmp_path, port)
        became_healthy = wait_healthy(port, proc, timeout=5.0)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        check(f"boot refuses to serve on malformed token file ({label})", not became_healthy,
              "server became healthy despite a broken token file -- fail-closed boot check violated")
        if tmp_path.exists():
            tmp_path.unlink()


def main() -> int:
    admin_token, client_token = secrets.token_hex(32), secrets.token_hex(32)
    admin_name, client_name = "ci-admin", "ci-client"
    token_path = Path(tempfile.mktemp(suffix=".json"))
    write_token_file(token_path, {
        hashlib.sha256(admin_token.encode()).hexdigest(): {"name": admin_name, "role": "admin"},
        hashlib.sha256(client_token.encode()).hexdigest(): {"name": client_name, "role": "client"},
    })

    port = free_port()
    proc = spawn_server(token_path, port)
    try:
        healthy = wait_healthy(port, proc)
        check("server boots and becomes healthy on a well-formed token file", healthy)
        if not healthy:
            out = proc.stdout.read() if proc.stdout else ""
            print("--- server output ---\n" + out[-3000:])
            return 1

        test_valid_tokens_reach_every_tool(port, admin_token, admin_name, client_token, client_name)
        test_no_identity_spoofing(port, client_token, client_name)
        test_invalid_tokens_always_401(port, admin_token)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        token_path.unlink(missing_ok=True)

    test_boot_refuses_on_malformed_token_file()

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
