#!/usr/bin/env python3
"""Mints a new zorc-mcp bearer token for one client."""
import hashlib
import json
import secrets
import sys
from pathlib import Path

TOKEN_PATH = Path(__file__).resolve().parent.parent / "deploy" / "secrets" / "mcp_token.json"
VALID_ROLES = ("admin", "client")


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[2] not in VALID_ROLES:
        print(f"usage: {sys.argv[0]} <name> <{'|'.join(VALID_ROLES)}>", file=sys.stderr)
        return 1
    name, role = sys.argv[1], sys.argv[2]

    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    token_map = json.loads(TOKEN_PATH.read_text()) if TOKEN_PATH.exists() else {}

    before = len(token_map)
    token_map = {h: info for h, info in token_map.items() if info.get("name") != name}
    replaced = len(token_map) < before

    token = secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    token_map[token_hash] = {"name": name, "role": role}

    TOKEN_PATH.write_text(json.dumps(token_map, indent=2) + "\n")

    action = "rotated" if replaced else "minted"
    print(f"{action} token for {name!r} (role={role}). This is shown ONCE -- store it now:\n")
    print(token)
    print(f"\n{TOKEN_PATH} now holds {len(token_map)} client(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
