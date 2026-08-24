# zorc

Deploy apps to your own hardware from an AI coding agent, through a
guarded MCP server, without paying for cloud compute you already own.

You don't need a cloud account or a VPS to have a real deployment
platform. Any machine you already have -- a desktop, an old laptop, a
home server -- can run zorc and become a place your coding agent deploys
real, internet-reachable apps to, with the same guardrails a team would
expect from a shared platform: ownership, memory budgets, audit logging,
and admin approval for anything destructive.

## What it actually is

zorc is an MCP server that sits in front of [Coolify](https://coolify.io)
(the open-source self-hosted PaaS that does the actual container
building and running) and adds the parts an AI agent needs to deploy
safely and repeatably: a required capability/requirements check before
every deploy, per-owner memory budgets, a two-step request-and-approve
queue for anything destructive, and a full audit log.

It is not a replacement for Coolify, and it is not magic -- it still
needs a machine, Coolify installed on that machine, and some way to
reach it from the internet (Cloudflare Tunnel by default, so you don't
need a public IP or open ports).

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

- `deploy/agent.py` -- the mechanism: Coolify API calls, Cloudflare DNS
  and tunnel management, git operations, resource accounting.
- `deploy/mcp_server.py` -- the MCP tool surface: auth, ownership,
  rate limits, the approval queue, and the guardrails layered on top of
  agent.py. It never reimplements agent.py's logic.
- `scripts/` -- token minting and the test suite.

Not yet in this release: GPU fleet orchestration and a shared AI
gateway. Both existed in the private version this was forked from and
may show up here later.

## Quickstart

See [docs/setup.md](docs/setup.md) for the full walkthrough. Short
version:

1. Install Coolify on the machine you're deploying to.
2. Set up a Cloudflare Tunnel (or your own reverse proxy) pointing at
   this machine.
3. `cp config.yaml.example config.yaml` and `cp registry.yaml.example
   registry.yaml`, fill in your own values.
4. `pip install -r deploy/requirements.txt`
5. `python3 scripts/mint_token.py <your-name> admin`
6. Run `deploy/mcp_server.py` (directly, or via the systemd unit in
   `systemd/`) and point your coding agent's MCP client at it.

## License

AGPLv3. See [LICENSE](LICENSE). If you run a modified version of this
as a network service, you're required to make your changes available to
its users.
