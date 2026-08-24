# zorc

A self-hosted app deployment platform, and the framework to run it.
Point an AI coding agent at zorc and it can deploy real, internet-
reachable apps to hardware you already own -- a desktop, an old laptop,
a home server -- with the same guardrails a team would expect from a
shared platform: ownership, memory budgets, audit logging, and admin
approval for anything destructive.

You don't need a cloud account or a VPS. One setup script turns a bare
Linux machine into a working deployment target: it installs
[Coolify](https://coolify.io) (the open-source engine that actually
builds and runs your containers), sets up a Cloudflare Tunnel so your
apps are reachable without a public IP or open ports, and installs
zorc's own MCP server on top with the guardrails an AI agent needs.

## What it actually is

zorc is the framework end to end: an installer that provisions Coolify
and Cloudflare Tunnel, plus an MCP server sitting in front of them that
adds what an AI agent needs to deploy safely and repeatably -- a
required capability/requirements check before every deploy, per-owner
memory budgets, a two-step request-and-approve queue for anything
destructive, and a full audit log.

It is not magic -- Coolify still does the real work of building and
running containers, zorc adds the orchestration and guardrails around
it. Understanding that split matters if you ever need to debug something
at the Coolify layer directly.

## Why an MCP server instead of a CLI

Because the intended caller is an AI coding agent, not a human typing
commands. Point Claude, Cursor, or any MCP-capable agent at zorc and it
can clone a repo, work out how to deploy it, submit that reasoning for a
sanity check, and deploy it -- all through tool calls, with every
mutating action gated, rate-limited, and logged under a real identity,
never a shared or anonymous credential.

## Core guarantees

- **Ownership.** Every app has an owner. A client token can only touch
  apps it owns; admin can touch anything.
- **Budgets.** Every owner has a soft memory cap, enforced before a
  deploy is even analyzed, not after it fails.
- **Approval for anything destructive.** Tearing an app down or giving
  one more memory never happens immediately -- it's requested, then an
  admin approves or rejects it. Nothing short of that approval call ever
  executes it.
- **Audit log.** Every mutating action, who did it, and the outcome,
  as JSON lines.
- **Create-only deploys.** The deploy tool only ever creates a new app.
  It cannot modify or delete an existing one -- that split is
  deliberate, not an oversight.

## What's in this repository

- `scripts/setup.py` -- the installer. Installs Coolify if it isn't
  already there, walks you through a Coolify API token and a Cloudflare
  API token, creates a tunnel, writes `config.yaml`/`registry.yaml`,
  mints your first admin token, and installs zorc-mcp as a system
  service. Safe to re-run -- every step checks whether it's already done.
- `deploy/agent.py` -- the mechanism: Coolify API calls, Cloudflare DNS
  and tunnel management, git operations, resource accounting.
- `deploy/mcp_server.py` -- the MCP tool surface: auth, ownership,
  rate limits, the approval queue, and the guardrails layered on top of
  agent.py. It never reimplements agent.py's logic.
- `scripts/` -- the installer, token minting, and the test suite.

Not yet in this release: GPU fleet orchestration and a shared AI
gateway. Both existed in the private version this was forked from and
may show up here later.

## Quickstart

```
git clone https://github.com/zaindroid/zorc-mcp
cd zorc-mcp
python3 scripts/setup.py
```

That's the whole install. It asks for a Coolify API token and a
Cloudflare API token along the way -- see
[docs/setup.md](docs/setup.md) for where to get those and what each
step of the wizard actually does, including the manual path if you're
not on Linux or want to configure a piece yourself.

## License

AGPLv3. See [LICENSE](LICENSE). If you run a modified version of this
as a network service, you're required to make your changes available to
its users.
