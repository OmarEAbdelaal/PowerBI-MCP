# Power BI MCP Server for Claude

An [MCP](https://modelcontextprotocol.io) server that connects Claude (Claude Desktop, Claude Code,
or any MCP client) to **Microsoft Power BI** through the Power BI REST API.

Ask Claude things like:

- *"What were total sales by region last quarter in the Sales model?"* (Claude finds the semantic model, reads its schema, writes DAX, and runs it)
- *"Which datasets in the Finance workspace failed their last refresh, and why?"*
- *"Refresh the Inventory dataset and tell me when it's done."*
- *"Export the Executive Summary report to PDF."*
- *"Change the refresh schedule of Orders to 7am and 4:30pm on weekdays (Arab Standard Time)."*

---

## Tools

| Area | Tools |
|---|---|
| Connection | `check_connection` |
| Workspaces | `list_workspaces`, `get_workspace`, `list_workspace_users`, `create_workspace`✏️, `add_workspace_user`✏️, `delete_workspace`⚠️ |
| Semantic models (datasets) | `list_datasets`, `get_dataset`, `get_dataset_schema`, **`execute_dax_query`**, `get_refresh_history`, `get_refresh_schedule`, `get_dataset_parameters`, `get_dataset_datasources`, `refresh_dataset`✏️, `cancel_refresh`✏️, `update_refresh_schedule`✏️, `update_dataset_parameters`✏️, `take_over_dataset`✏️, `delete_dataset`⚠️ |
| Reports | `list_reports`, `get_report`, `get_report_pages`, `export_report` (PDF/PPTX/PNG), `clone_report`✏️, `rebind_report`✏️, `import_pbix`✏️, `delete_report`⚠️ |
| Dashboards | `list_dashboards`, `get_dashboard_tiles` |
| Dataflows | `list_dataflows`, `get_dataflow_transactions`, `refresh_dataflow`✏️ |
| Apps / gateways / capacities | `list_apps`, `list_app_reports`, `list_gateways`, `list_gateway_datasources`, `list_capacities` |
| Admin (opt-in) | `admin_list_workspaces`, `admin_get_activity_events` |
| Prompt | `analyze_dataset` |

✏️ = changes Power BI (hidden when `POWERBI_READ_ONLY=true`) ⚠️ = destructive (also hidden in read-only mode)

Omitting `workspace_id` targets **My workspace**, or `POWERBI_DEFAULT_WORKSPACE_ID` if set.

---

## 1. Install

Requires Python 3.10+.

```bash
git clone <this repo> powerbi-mcp   # or copy the folder
cd powerbi-mcp
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e .
```

(Or with [uv](https://docs.astral.sh/uv/): `uv pip install -e .`)

## 2. Create an Azure (Entra ID) app registration

Pick **one** authentication mode.

### Option A — Service principal (recommended for teams / unattended use)

1. **Azure portal → Microsoft Entra ID → App registrations → New registration.** Name it e.g. `Claude Power BI`. Leave the redirect URI empty.
2. **Certificates & secrets → New client secret.** Copy the value.
3. Note the **Application (client) ID** and **Directory (tenant) ID**.
4. Create an Entra **security group** (e.g. `PowerBI-API-Apps`) and add the app to it.
5. **Power BI Admin portal → Tenant settings → Developer settings →** enable
   *"Service principals can use Fabric APIs"* (older name: *"Allow service principals to use Power BI APIs"*) for that security group.
   - For `execute_dax_query` / `get_dataset_schema`, also enable **Integration settings → "Dataset Execute Queries REST API"**.
   - For admin tools, enable **Admin API settings → "Service principals can access read-only admin APIs"**.
6. In each Power BI workspace Claude should see: **Manage access → add the app** as *Member* or *Contributor* (Viewer is not enough for most API calls; Admin to manage users).

> Service principals cannot access **My workspace** — always pass a `workspace_id` or set `POWERBI_DEFAULT_WORKSPACE_ID`.
> Do **not** grant the app any Power BI API permissions in Azure for this mode; access is controlled by workspace membership.

### Option B — Your own user account (device code sign-in)

1. Create an app registration as above (no secret needed).
2. **Authentication → Advanced settings → Allow public client flows → Yes.**
3. **API permissions → Add → Power BI Service → Delegated**: `Workspace.Read.All`, `Dataset.ReadWrite.All`, `Report.ReadWrite.All`, `Dashboard.Read.All`, `Dataflow.ReadWrite.All`, `Capacity.Read.All`, `Gateway.Read.All`, `App.Read.All` (use `.Read.All` variants only if you want read-only). Grant admin consent if your tenant requires it.
4. Sign in once from a terminal:

   ```bash
   POWERBI_AUTH_MODE=device_code POWERBI_TENANT_ID=<tenant> POWERBI_CLIENT_ID=<client> powerbi-mcp login
   ```

   Open the URL shown, enter the code, sign in. The refresh token is cached in `~/.powerbi-mcp/token_cache.json` and reused silently.

### Option C — Quick test with a raw token

Set `POWERBI_ACCESS_TOKEN` (e.g. from `az account get-access-token --resource https://analysis.windows.net/powerbi/api --query accessToken -o tsv`). Expires after ~1 hour.

## 3. Verify

```bash
cp .env.example .env    # fill in values, then export them, or set them in your shell
powerbi-mcp check
```

## 4. Connect to Claude

### Claude Desktop

Edit `claude_desktop_config.json` (Settings → Developer → Edit Config):

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "powerbi": {
      "command": "C:\\path\\to\\powerbi-mcp\\.venv\\Scripts\\powerbi-mcp.exe",
      "env": {
        "POWERBI_AUTH_MODE": "service_principal",
        "POWERBI_TENANT_ID": "00000000-0000-0000-0000-000000000000",
        "POWERBI_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
        "POWERBI_CLIENT_SECRET": "your-secret",
        "POWERBI_DEFAULT_WORKSPACE_ID": "00000000-0000-0000-0000-000000000000",
        "POWERBI_READ_ONLY": "false"
      }
    }
  }
}
```

On macOS/Linux use `/path/to/powerbi-mcp/.venv/bin/powerbi-mcp`. Restart Claude Desktop; the 🔨 tools menu should list the Power BI tools.

### Claude Code

```bash
claude mcp add powerbi \
  -e POWERBI_TENANT_ID=... -e POWERBI_CLIENT_ID=... -e POWERBI_CLIENT_SECRET=... \
  -- /path/to/powerbi-mcp/.venv/bin/powerbi-mcp
```

### Remote / shared (HTTP)

```bash
powerbi-mcp --http --host 0.0.0.0 --port 8000     # endpoint: http://<host>:8000/mcp
```

Put it behind HTTPS + authentication (reverse proxy, VPN) before exposing it — anyone who can reach
the endpoint acts with the configured Power BI identity. A `Dockerfile` is included.

---

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `POWERBI_AUTH_MODE` | auto | `service_principal`, `device_code`, or `access_token` (auto-detected from what's set) |
| `POWERBI_TENANT_ID` | – | Entra tenant id (also reads `AZURE_TENANT_ID`) |
| `POWERBI_CLIENT_ID` | – | App registration client id (also `AZURE_CLIENT_ID`) |
| `POWERBI_CLIENT_SECRET` | – | Client secret (also `AZURE_CLIENT_SECRET`) |
| `POWERBI_ACCESS_TOKEN` | – | Pre-acquired bearer token |
| `POWERBI_DEFAULT_WORKSPACE_ID` | – | Workspace used when a tool's `workspace_id` is omitted |
| `POWERBI_READ_ONLY` | `false` | Hide every tool that modifies Power BI |
| `POWERBI_ENABLE_ADMIN_TOOLS` | `false` | Expose tenant-wide admin tools |
| `POWERBI_MAX_ROWS` | `1000` | Max DAX rows returned to Claude per query |
| `POWERBI_EXPORT_DIR` | `./powerbi_exports` | Where `export_report` saves files |
| `POWERBI_TOKEN_CACHE` | `~/.powerbi-mcp/token_cache.json` | Device-code token cache |
| `POWERBI_REQUEST_TIMEOUT` | `120` | HTTP timeout (s) |
| `POWERBI_API_BASE` | `https://api.powerbi.com/v1.0/myorg` | Sovereign clouds, e.g. `https://api.powerbigov.us/v1.0/myorg` |
| `POWERBI_AUTHORITY_HOST` | `https://login.microsoftonline.com` | e.g. `https://login.microsoftonline.us` for GCC High |
| `POWERBI_SCOPE` | `https://analysis.windows.net/powerbi/api/.default` | e.g. `https://analysis.usgovcloudapi.net/powerbi/api/.default` |

## Limitations to know

- **DAX queries** (`execute_dax_query`): max 100,000 rows / 1M values per query, 120 requests/min per user. Not supported for datasets using live connection to Analysis Services on-premises or with RLS when using a service principal without `impersonated_user`.
- **`get_dataset_schema`** uses DAX `INFO.VIEW.*` functions, so it needs the same "Execute Queries" permission. Falls back to the REST tables endpoint (push datasets only).
- **`export_report`** and table-level refresh require Premium, Premium Per User, Fabric, or Embedded capacity.
- Throttled requests (HTTP 429) are retried automatically using `Retry-After`.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Security notes

- Prefer **read-only mode** and **Viewer/Contributor** workspace roles unless you need Claude to make changes.
- Keep the client secret out of source control (`.env` is in `.gitignore`).
- Claude Desktop/Code still asks you before running each tool unless you've allowed it.
