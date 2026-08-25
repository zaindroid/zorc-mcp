# Setup

## The wizard

```
git clone https://github.com/zaindroid/zorc-mcp
cd zorc-mcp
python3 scripts/setup.py
```

It bootstraps its own virtualenv on first run, then walks through:

1. **Coolify.** If it's not already running on this machine, offers to
   install it (runs Coolify's own official installer). You'll need to
   open its web UI once and create your admin account before continuing
   -- that first-run step has no API, it has to be done by hand.
2. **Coolify API token.** Coolify UI -> Keys & Tokens -> create one with
   API access, paste it in.
3. **Coolify project.** Picks your first existing project, or creates
   one named "zorc" if you don't have one yet.
4. **Domain + Cloudflare API token.** The domain you'll deploy apps
   under (must already be on Cloudflare), and a token with at least
   Zone:DNS:Edit for it.
5. **cloudflared.** Installs it if missing, runs `cloudflared tunnel
   login` (opens a browser -- this step is unavoidably interactive,
   Cloudflare doesn't offer a non-interactive way to authorize a new
   tunnel), then creates a tunnel and writes `cloudflared/config.yml`.
6. **registry.yaml / config.yaml.** Detects this machine's real memory,
   writes both files.
7. **zorc-mcp itself.** Mints your first admin token, installs and
   starts the systemd service.

Re-run it any time -- every step checks whether it's already done
before doing it again.

At the end it prints the URL and token to point your coding agent's MCP
client at.

## Adding more machines later

Each additional machine is another entry in `registry.yaml`'s `nodes:`
section. `backend: coolify` if Coolify is installed there too;
`backend: zorc-agent` for a lighter machine that only has Docker --
zorc deploys to it directly over SSH, no Coolify needed on that one.
The wizard only sets up the first machine; add others by hand following
the shape of the `local` entry it wrote.

## Doing it by hand instead

Not on Linux, or want to configure a piece yourself rather than let the
wizard do it -- everything below is what the wizard automates.

### Coolify

Install from [Coolify's own docs](https://coolify.io/docs/get-started/installation).
Create a project in the UI, open it -- the project and environment
UUIDs are in the URL. Create an API token (Keys & Tokens) and save it:

```
mkdir -p deploy/secrets
echo '{"token": "your-coolify-token"}' > deploy/secrets/coolify.json
```

### Cloudflare Tunnel

1. Add your domain to Cloudflare if it isn't already.
2. `cloudflared tunnel login`, then `cloudflared tunnel create zorc` --
   note the tunnel ID it prints.
3. `cp cloudflared/config.yml.example cloudflared/config.yml`, fill in
   the tunnel ID and the credentials file path it printed.
4. `cloudflared service install`, pointed at that config.
5. Get your account ID and zone ID from the Cloudflare dashboard
   (domain Overview page, right sidebar).
6. Save an API token with Zone:DNS:Edit:

```
echo '{"token": "your-cloudflare-token"}' > deploy/secrets/cloudflare.json
```

### zorc itself

```
cp config.yaml.example config.yaml
cp registry.yaml.example registry.yaml
```

Fill in `config.yaml` with the values from the two sections above.
Adjust `registry.yaml`'s `local` node entry to match this machine's real
memory and Coolify server UUID (Coolify UI -> Servers).

```
pip install -r deploy/requirements.txt
python3 scripts/mint_token.py <your-name> admin
```

Then either run directly:

```
cd deploy && uvicorn mcp_server:app --host 127.0.0.1 --port 8081
```

or install the systemd unit:

```
cp systemd/zorc-mcp.service.example /etc/systemd/system/zorc-mcp.service
# edit the paths and User= inside to match your setup
systemctl daemon-reload && systemctl enable --now zorc-mcp
```

Add `mcp.<your-domain>` to your tunnel's ingress rules pointing at
`http://localhost:8081`.

## Rate limits

Every mutating tool (deploy, redeploy, restart, teardown requests,
memory increases, env var updates, token minting/revocation) is
rate-limited by default, platform-wide. Set
`ZORC_MCP_RATE_LIMITS_DISABLED=1` in the systemd unit's `[Service]`
section (or the environment zorc-mcp runs in generally) to turn all of
them off at once without removing the mechanism -- ownership checks and
auth are unaffected either way.

## Point your coding agent at it

Any MCP-capable agent, configured with:

- URL: `https://mcp.<your-domain>/mcp`
- Header: `Authorization: Bearer <token from setup>`

Call `get_platform_contract()` first to see what it expects from an app,
then `analyze_deployment_requirements()` before every `deploy()`.
