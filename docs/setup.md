# Setup

## 1. Install Coolify

Coolify does the actual container building and running. Install it on
the machine you want to deploy to, following
[Coolify's own install docs](https://coolify.io/docs/get-started/installation).
Once it's running, log into its UI, create a project, and open it -- the
project and environment UUIDs are in the URL, you'll need them below.

## 2. Get a Coolify API token

Coolify UI -> Keys & Tokens -> create a token with API access. Save it:

```
mkdir -p deploy/secrets
echo '{"token": "your-coolify-token"}' > deploy/secrets/coolify.json
```

## 3. Set up Cloudflare Tunnel

This is what makes "no VPS, no open ports" work: cloudflared runs on the
same machine as Coolify and creates an outbound-only tunnel, so apps
become reachable at a real domain without you exposing anything to the
internet directly.

1. Add your domain to Cloudflare if it isn't already.
2. `cloudflared tunnel create zorc` (or via the Cloudflare dashboard) --
   note the tunnel ID it prints.
3. `cp cloudflared/config.yml.example cloudflared/config.yml`, fill in
   the tunnel ID and the credentials file path it printed.
4. Install cloudflared as a system service pointing at that config
   (`cloudflared service install`, or your distro's equivalent).
5. Get your Cloudflare account ID and zone ID from the dashboard
   (Overview page for your domain, right-hand sidebar).
6. Save your Cloudflare API token (needs Zone:DNS:Edit permission):

```
echo '{"token": "your-cloudflare-token"}' > deploy/secrets/cloudflare.json
```

## 4. Configure zorc

```
cp config.yaml.example config.yaml
cp registry.yaml.example registry.yaml
```

Fill in `config.yaml`: your domain, Coolify's URL and project/environment
UUIDs from step 1, your Cloudflare account/zone/tunnel IDs from step 3,
and `local_node` (a name for this machine -- must match the key you use
in `registry.yaml`'s `nodes:` section).

Adjust `registry.yaml`'s `local` node entry to match this machine's real
memory -- `total_memory_mb` is the machine's actual RAM,
`server_uuid` is Coolify's UUID for this server (Coolify UI -> Servers).

## 5. Install and run

```
pip install -r deploy/requirements.txt
python3 scripts/mint_token.py <your-name> admin
```

Store the printed token -- it's shown once. Then either run directly:

```
cd deploy && uvicorn mcp_server:app --host 127.0.0.1 --port 8081
```

or install the systemd unit for it to survive reboots:

```
cp systemd/zorc-mcp.service.example /etc/systemd/system/zorc-mcp.service
# edit the paths and User= inside to match your setup
systemctl daemon-reload && systemctl enable --now zorc-mcp
```

Add `mcp.<your-domain>` to your tunnel's ingress rules pointing at
`http://localhost:8081`, matching the pattern already in
`cloudflared/config.yml.example`.

## 6. Point your coding agent at it

Any MCP-capable agent, configured with:

- URL: `https://mcp.<your-domain>/mcp`
- Header: `Authorization: Bearer <token from step 5>`

Call `get_platform_contract()` first to see what it expects from an app,
then `analyze_deployment_requirements()` before every `deploy()`.

## Adding more machines later

Each additional machine is another entry in `registry.yaml`'s `nodes:`
section. `backend: coolify` if Coolify is installed there too;
`backend: zorc-agent` for a lighter machine that only has Docker --
zorc deploys to it directly over SSH, no Coolify needed on that one.
