# Cloud Provider Guide

Mission Control's infrastructure monitoring (Quill agent) connects to your cloud via **MCP (Model Context Protocol)** — not hardcoded API calls. This means swapping DigitalOcean for another cloud is just:

1. Install a different MCP server package
2. Update one wrapper script
3. Edit Quill's monitoring prompt

No Python code changes needed.

## How It Works

```
Quill agent → Copilot SDK → MCP tool calls → Cloud MCP server → Cloud API
                                                    ↑
                                          npx @digitalocean/mcp
                                          (swap this package)
```

The entire cloud integration is a single npx command in a wrapper script. Quill's SOUL.md tells the LLM *what* to check — the MCP server handles *how* to talk to the API.

## Supported Clouds

### DigitalOcean (Default)

**Best for:** Hobby developers who want simple, predictable pricing. $4-12/mo droplets.

```bash
# .env
DO_API_TOKEN=your_token
```

MCP package: `@digitalocean/mcp` (official)
No changes needed — this is the default.

---

### Railway

**Best for:** Developers who want Heroku-like simplicity. Deploy from GitHub with zero config.

MCP package: `@railway/mcp-server` (official)

**Step 1:** Get your Railway API token from [railway.com/account/tokens](https://railway.com/account/tokens).

**Step 2:** Update the wrapper in `agents/mission_control/core/base_agent.py`:

```python
# In _ensure_do_mcp_wrapper(), change the exec line:
exec npx -y @railway/mcp-server "$@"
```

Or add a new wrapper method and swap the MCP key from `"digitalocean"` to `"railway"`.

**Step 3:** Update `agents/squad/quill/SOUL.md` monitoring checklist to match Railway's resources:
- Services (status, deployments, logs)
- Databases (PostgreSQL, Redis, MySQL)
- Volumes, cron jobs
- Environment variables and secrets

**Step 4:** Update `MONITORING_PROMPT` in `agents/squad/quill/agent.py`:

```python
MONITORING_PROMPT = """Run your infrastructure monitoring checklist NOW.

Use your Railway MCP tools to check:
1. List all services — check each status (active/deploying/failed)
2. List all databases — check each status
3. Check recent deployments for failures
4. Get logs for any service with errors
5. Check resource usage (CPU, memory, bandwidth)

Write a summary. Report issues clearly with severity."""
```

---

### Hetzner Cloud

**Best for:** EU-based developers who want the cheapest VPS. €3.29/mo for 2 vCPU, 2GB RAM.

MCP package: `mcp-hetzner` (community, Python-based — [github.com/dkruyt/mcp-hetzner](https://github.com/dkruyt/mcp-hetzner))

**Step 1:** Install the Hetzner MCP server:

```bash
pip install mcp-hetzner
# or: uvx mcp-hetzner
```

**Step 2:** Since this is a Python MCP server (not npm), update the wrapper:

```bash
#!/bin/bash
export HETZNER_API_TOKEN="your_token"
exec python -m mcp_hetzner "$@"
```

**Step 3:** Update Quill's SOUL.md for Hetzner resources:
- Servers (status, metrics, console)
- Load balancers, firewalls
- Volumes, floating IPs
- SSH keys, images

---

### AWS Lightsail

**Best for:** Developers already in the AWS ecosystem who want simple VPS without full AWS complexity. $3.50/mo instances.

No official MCP package exists yet. Two approaches:

**Option A: Use the AWS CLI MCP wrapper**

```bash
#!/bin/bash
# Quill can use the Copilot SDK's native tool calling to run AWS CLI commands
# No MCP server needed — just ensure `aws` CLI is configured
aws configure  # set up credentials
```

Then update Quill's SOUL.md to instruct it to use shell commands:
```
Use `aws lightsail` CLI commands to check instance status, databases, load balancers.
```

**Option B: Wait for official AWS MCP** — AWS has announced MCP support in Quick Suite. A standalone MCP server package is expected.

---

### Vultr

**Best for:** Developers who want bare metal and high-frequency compute at competitive prices. $2.50/mo for basic VPS.

No official MCP package exists yet. Same approach as AWS Lightsail — use Vultr CLI or API via shell commands:

```bash
pip install vultr-cli
# or use the Vultr API directly via curl
```

Update Quill's SOUL.md to use `vultr-cli` commands for monitoring.

## Adding a New Cloud Provider

The general pattern:

1. **Find or build an MCP server** for your cloud. Check:
   - [mcphub.io](https://mcphub.io) — community MCP directory
   - [modelcontextprotocol.io/servers](https://modelcontextprotocol.io/servers) — official registry
   - Your cloud provider's docs

2. **Create a wrapper script** (or reuse the existing pattern in `base_agent.py`):
   ```bash
   #!/bin/bash
   export YOUR_CLOUD_TOKEN="..."
   exec npx -y @your-cloud/mcp-server "$@"
   ```

3. **Update `base_agent.py`** — add a new MCP server entry in `_build_mcp_servers()`:
   ```python
   if "your-cloud" in self.mcp_servers:
       if settings.your_cloud_token:
           wrapper = self._ensure_your_cloud_wrapper(settings.your_cloud_token)
           mcp_servers["your-cloud"] = {
               "type": "local",
               "command": wrapper,
               "args": [],
           }
   ```

4. **Update Quill's config** — in `factory.py`, change `mcp_servers: ["your-cloud"]`

5. **Rewrite Quill's SOUL.md** — describe what resources to monitor and how to report them. The LLM figures out which MCP tools to call based on the instructions.

That's it. No other code changes needed. The MCP architecture keeps cloud integrations fully decoupled from the agent logic.

## Running Without Any Cloud

If you're running everything locally (laptop, home server, Raspberry Pi), you can:

1. Set `VISION_MONITORED_SERVICES` to your local services
2. Skip the cloud MCP entirely — just don't set `DO_API_TOKEN`
3. Quill will still check for assigned tasks but won't run cloud monitoring
4. Vision still monitors local services, memory, logs, tasks, and processes
