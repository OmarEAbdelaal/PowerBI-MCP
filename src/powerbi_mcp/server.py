"""Power BI MCP server - exposes Power BI REST API operations as MCP tools for Claude."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import bim
from .client import PowerBIClient, PowerBIError
from .config import Settings

settings = Settings.from_env()

INSTRUCTIONS = """\
Tools for Microsoft Power BI (workspaces, semantic models/datasets, reports,
dashboards, dataflows, gateways, apps).

Typical workflow for answering a data question:
1. list_workspaces -> find the workspace id.
2. list_datasets(workspace_id) -> find the semantic model (dataset) id.
3. get_dataset_schema(dataset_id, workspace_id) -> learn tables, columns, measures.
4. execute_dax_query(dataset_id, "EVALUATE ...", workspace_id) -> get the numbers.

DAX tips: queries must start with EVALUATE (or DEFINE ... EVALUATE). Prefer
SUMMARIZECOLUMNS for aggregations, TOPN to limit rows, and reference existing
measures as [Measure Name]. Omit workspace_id to use "My workspace" (or the
configured default workspace).

APG BIM Intelligence: you also act as a BIM Manager, data analyst and design
intelligence consultant for APG (Architecture, Interior, Landscape). Read the
resource apg://bim-kpi-framework for KPI definitions, targets, dashboard
hierarchy and the mandatory report format (Summary, Key Findings, Risks,
Recommendations, KPI Table with Green/Yellow/Red). Use
calculate_bim_health_score and calculate_design_kpis to score model data
pulled with execute_dax_query. Always interpret: trends, risks, anomalies,
benchmarks and recommended actions - never just raw numbers.
"""

mcp = MCPServer(name="powerbi", title="Power BI", instructions=INSTRUCTIONS, version="1.0.0")

_client: PowerBIClient | None = None


def client() -> PowerBIClient:
    global _client
    if _client is None:
        _client = PowerBIClient(settings)
    return _client


READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)


def read_tool(fn: Callable) -> Callable:
    return mcp.tool(annotations=READ)(fn)


def write_tool(destructive: bool = False) -> Callable[[Callable], Callable]:
    """Register a tool that changes Power BI state (skipped when POWERBI_READ_ONLY=true)."""

    def deco(fn: Callable) -> Callable:
        if settings.read_only:
            return fn
        return mcp.tool(annotations=DESTRUCTIVE if destructive else WRITE)(fn)

    return deco


def admin_tool(fn: Callable) -> Callable:
    if not settings.enable_admin_tools:
        return fn
    return mcp.tool(annotations=READ)(fn)


def _pick(items: list[dict], keys: list[str]) -> list[dict]:
    return [{k: it[k] for k in keys if k in it} for it in items]


def _gp(workspace_id: str | None) -> str:
    return client().group_path(workspace_id)


# =====================================================================
# Connection / workspaces
# =====================================================================


@read_tool
async def check_connection() -> dict:
    """Verify authentication to Power BI works and report the configuration in use."""
    try:
        groups = await client().get("/groups", params={"$top": 5})
        return {
            "ok": True,
            "auth_mode": settings.auth_mode,
            "api_base": settings.api_base,
            "read_only": settings.read_only,
            "admin_tools": settings.enable_admin_tools,
            "sample_workspaces": _pick(groups.get("value", []), ["id", "name"]),
        }
    except Exception as e:  # noqa: BLE001 - surface any auth/config issue to the user
        return {"ok": False, "auth_mode": settings.auth_mode, "error": str(e)}


@read_tool
async def list_workspaces(name_contains: str | None = None, top: int = 200) -> list[dict]:
    """List Power BI workspaces (groups) the caller can access.

    Args:
        name_contains: Optional case-sensitive substring to filter workspace names.
        top: Maximum number of workspaces to return.
    """
    params: dict[str, Any] = {"$top": top}
    if name_contains:
        safe = name_contains.replace("'", "''")
        params["$filter"] = f"contains(name,'{safe}')"
    items = await client().get_values("/groups", params=params)
    return _pick(items, ["id", "name", "type", "isReadOnly", "isOnDedicatedCapacity", "capacityId"])


@read_tool
async def get_workspace(workspace_id: str) -> dict:
    """Get details of one workspace by id."""
    items = await client().get_values(
        "/groups", params={"$filter": f"id eq '{workspace_id}'"}
    )
    if not items:
        raise PowerBIError(404, f"Workspace {workspace_id} not found or not accessible")
    return items[0]


@read_tool
async def list_workspace_users(workspace_id: str) -> list[dict]:
    """List users, groups and service principals with access to a workspace and their roles."""
    return await client().get_values(f"/groups/{workspace_id}/users")


@write_tool()
async def create_workspace(name: str) -> dict:
    """Create a new workspace (V2 workspace)."""
    return await client().post("/groups", {"name": name}, params={"workspaceV2": "True"})


@write_tool()
async def add_workspace_user(
    workspace_id: str,
    identifier: str,
    access_right: Literal["Admin", "Member", "Contributor", "Viewer"] = "Viewer",
    principal_type: Literal["User", "Group", "App"] = "User",
) -> dict:
    """Grant a user / security group / service principal access to a workspace.

    Args:
        identifier: Email/UPN for users, object id for groups and apps.
    """
    body: dict[str, Any] = {
        "identifier": identifier,
        "groupUserAccessRight": access_right,
        "principalType": principal_type,
    }
    if principal_type == "User":
        body["emailAddress"] = identifier
    return await client().post(f"/groups/{workspace_id}/users", body)


@write_tool(destructive=True)
async def delete_workspace(workspace_id: str) -> dict:
    """Permanently delete a workspace. Only call when the user explicitly asks for it."""
    return await client().delete(f"/groups/{workspace_id}")


# =====================================================================
# Datasets / semantic models
# =====================================================================


@read_tool
async def list_datasets(workspace_id: str | None = None) -> list[dict]:
    """List semantic models (datasets) in a workspace. Omit workspace_id for My workspace."""
    items = await client().get_values(f"{_gp(workspace_id)}/datasets")
    return _pick(
        items,
        [
            "id",
            "name",
            "configuredBy",
            "isRefreshable",
            "targetStorageMode",
            "createdDate",
            "webUrl",
        ],
    )


@read_tool
async def get_dataset(dataset_id: str, workspace_id: str | None = None) -> dict:
    """Get metadata for a semantic model (dataset)."""
    return await client().get(f"{_gp(workspace_id)}/datasets/{dataset_id}")


def _clean_key(k: str) -> str:
    """'Sales[Amount]' -> 'Sales[Amount]'; '[Name]' -> 'Name'."""
    if k.startswith("[") and k.endswith("]"):
        return k[1:-1]
    return k


async def _run_dax(
    dataset_id: str,
    query: str,
    workspace_id: str | None,
    impersonated_user: str | None = None,
) -> list[dict]:
    body: dict[str, Any] = {
        "queries": [{"query": query}],
        "serializerSettings": {"includeNulls": True},
    }
    if impersonated_user:
        body["impersonatedUserName"] = impersonated_user
    data = await client().post(f"{_gp(workspace_id)}/datasets/{dataset_id}/executeQueries", body)
    result = data["results"][0]
    if "error" in result:
        raise PowerBIError(400, json.dumps(result["error"]))
    tables = result.get("tables", [])
    if tables and "error" in tables[0]:
        raise PowerBIError(400, json.dumps(tables[0]["error"]))
    return tables[0].get("rows", []) if tables else []


@read_tool
async def execute_dax_query(
    dataset_id: str,
    query: str,
    workspace_id: str | None = None,
    max_rows: int | None = None,
    impersonated_user: str | None = None,
) -> dict:
    """Run a DAX query against a semantic model and return the result rows.

    Args:
        dataset_id: Semantic model (dataset) id.
        query: DAX query, e.g. "EVALUATE TOPN(10, SUMMARIZECOLUMNS('Date'[Year], \"Sales\", [Total Sales]))".
        workspace_id: Workspace id (omit for My workspace / default workspace).
        max_rows: Max rows returned to the conversation (default POWERBI_MAX_ROWS).
        impersonated_user: Optional UPN to evaluate row-level security as that user.
    """
    limit = max_rows or settings.max_rows
    rows = await _run_dax(dataset_id, query, workspace_id, impersonated_user)
    rows_clean = [{_clean_key(k): v for k, v in r.items()} for r in rows]
    return {
        "row_count": len(rows_clean),
        "truncated": len(rows_clean) > limit,
        "columns": list(rows_clean[0].keys()) if rows_clean else [],
        "rows": rows_clean[:limit],
    }


@read_tool
async def get_dataset_schema(
    dataset_id: str,
    workspace_id: str | None = None,
    include_hidden: bool = False,
    include_measure_expressions: bool = True,
) -> dict:
    """Describe a semantic model: tables, columns (with data types), measures and relationships.

    Uses the DAX INFO.VIEW.* functions; falls back to the REST tables endpoint
    (push datasets only) if those aren't available.
    """

    def strip(rows: list[dict]) -> list[dict]:
        return [{_clean_key(k): v for k, v in r.items()} for r in rows]

    try:
        tables, columns, measures, rels = await asyncio.gather(
            _run_dax(dataset_id, "EVALUATE INFO.VIEW.TABLES()", workspace_id),
            _run_dax(dataset_id, "EVALUATE INFO.VIEW.COLUMNS()", workspace_id),
            _run_dax(dataset_id, "EVALUATE INFO.VIEW.MEASURES()", workspace_id),
            _run_dax(dataset_id, "EVALUATE INFO.VIEW.RELATIONSHIPS()", workspace_id),
        )
    except PowerBIError as e:
        try:
            data = await client().get(f"{_gp(workspace_id)}/datasets/{dataset_id}/tables")
            return {"source": "rest_tables_endpoint", "tables": data.get("value", [])}
        except PowerBIError:
            raise e from None

    tables, columns, measures, rels = map(strip, (tables, columns, measures, rels))

    def visible(r: dict) -> bool:
        return include_hidden or not r.get("IsHidden", False)

    out_tables: dict[str, dict] = {}
    for t in tables:
        if not visible(t) or t.get("IsPrivate"):
            continue
        out_tables[t.get("Name")] = {
            "description": t.get("Description") or None,
            "columns": [],
            "measures": [],
        }
    for c in columns:
        tbl = out_tables.get(c.get("Table"))
        if tbl is None or not visible(c) or c.get("Type") == "RowNumber":
            continue
        col = {"name": c.get("Name"), "dataType": c.get("DataType")}
        if c.get("Expression"):
            col["calculated"] = True
        if c.get("Description"):
            col["description"] = c["Description"]
        tbl["columns"].append(col)
    for m in measures:
        tbl = out_tables.get(m.get("Table"))
        if tbl is None or not visible(m):
            continue
        meas = {"name": m.get("Name")}
        if include_measure_expressions and m.get("Expression"):
            meas["expression"] = m["Expression"]
        if m.get("FormatString"):
            meas["format"] = m["FormatString"]
        if m.get("Description"):
            meas["description"] = m["Description"]
        tbl["measures"].append(meas)

    relationships = [
        {
            "from": f"'{r.get('FromTable')}'[{r.get('FromColumn')}]",
            "to": f"'{r.get('ToTable')}'[{r.get('ToColumn')}]",
            "cardinality": f"{r.get('FromCardinality')}:{r.get('ToCardinality')}",
            "crossFilter": r.get("CrossFilteringBehavior"),
            "active": r.get("IsActive"),
        }
        for r in rels
    ]
    return {"source": "dax_info_view", "tables": out_tables, "relationships": relationships}


@read_tool
async def get_refresh_history(
    dataset_id: str, workspace_id: str | None = None, top: int = 10
) -> list[dict]:
    """Get recent refresh operations for a dataset (status, start/end time, errors)."""
    data = await client().get(
        f"{_gp(workspace_id)}/datasets/{dataset_id}/refreshes", params={"$top": top}
    )
    return data.get("value", [])


@read_tool
async def get_refresh_schedule(dataset_id: str, workspace_id: str | None = None) -> dict:
    """Get the scheduled refresh configuration of a dataset."""
    return await client().get(f"{_gp(workspace_id)}/datasets/{dataset_id}/refreshSchedule")


@read_tool
async def get_dataset_parameters(dataset_id: str, workspace_id: str | None = None) -> list[dict]:
    """List Power Query (M) parameters defined on a dataset."""
    data = await client().get(f"{_gp(workspace_id)}/datasets/{dataset_id}/parameters")
    return data.get("value", [])


@read_tool
async def get_dataset_datasources(dataset_id: str, workspace_id: str | None = None) -> list[dict]:
    """List the data sources (servers, databases, URLs) used by a dataset."""
    data = await client().get(f"{_gp(workspace_id)}/datasets/{dataset_id}/datasources")
    return data.get("value", [])


@write_tool()
async def refresh_dataset(
    dataset_id: str,
    workspace_id: str | None = None,
    wait_for_completion: bool = False,
    timeout_seconds: int = 600,
    notify_option: Literal["NoNotification", "MailOnFailure", "MailOnCompletion"] = "NoNotification",
    tables: list[str] | None = None,
) -> dict:
    """Trigger a dataset refresh, optionally waiting for it to finish.

    Args:
        tables: Optional list of table names to refresh (enhanced refresh; requires
            Premium/PPU/Fabric capacity). Omit to refresh the whole model.
        notify_option: Email notification (only valid for user auth, ignored for enhanced refresh).
    """
    path = f"{_gp(workspace_id)}/datasets/{dataset_id}/refreshes"
    if tables:
        body: dict[str, Any] = {
            "type": "Full",
            "objects": [{"table": t} for t in tables],
        }
    elif settings.auth_mode == "service_principal":
        body = {}
    else:
        body = {"notifyOption": notify_option}
    resp = await client().request("POST", path, json_body=body, raw=True)
    request_id = resp.headers.get("RequestId") or resp.headers.get("x-ms-request-id")
    result: dict[str, Any] = {"triggered": True, "requestId": request_id}
    if not wait_for_completion:
        return result

    deadline = time.monotonic() + timeout_seconds
    await asyncio.sleep(5)
    while time.monotonic() < deadline:
        hist = await client().get(path, params={"$top": 1})
        latest = (hist.get("value") or [{}])[0]
        status = latest.get("status")
        if status and status not in ("Unknown", "NotStarted", "InProgress"):
            result.update({"status": status, "refresh": latest})
            return result
        await asyncio.sleep(10)
    result["status"] = "StillRunning"
    result["note"] = f"Refresh did not finish within {timeout_seconds}s; check get_refresh_history."
    return result


@write_tool()
async def cancel_refresh(dataset_id: str, refresh_id: str, workspace_id: str | None = None) -> dict:
    """Cancel an in-progress enhanced refresh (refresh_id = requestId from refresh history)."""
    return await client().delete(
        f"{_gp(workspace_id)}/datasets/{dataset_id}/refreshes/{refresh_id}"
    )


@write_tool()
async def update_refresh_schedule(
    dataset_id: str,
    workspace_id: str | None = None,
    enabled: bool | None = None,
    days: list[str] | None = None,
    times: list[str] | None = None,
    local_time_zone_id: str | None = None,
    notify_option: Literal["NoNotification", "MailOnFailure"] | None = None,
) -> dict:
    """Update a dataset's scheduled refresh.

    Args:
        days: e.g. ["Monday", "Wednesday", "Friday"].
        times: 24h "HH:MM" values on the hour or half hour, e.g. ["07:00", "16:30"].
        local_time_zone_id: Windows time zone id, e.g. "UTC", "Arab Standard Time", "Pacific Standard Time".
    """
    value: dict[str, Any] = {}
    if enabled is not None:
        value["enabled"] = enabled
    if days is not None:
        value["days"] = days
    if times is not None:
        value["times"] = times
    if local_time_zone_id is not None:
        value["localTimeZoneId"] = local_time_zone_id
    if notify_option is not None:
        value["NotifyOption"] = notify_option
    if not value:
        raise ValueError("Provide at least one schedule field to update.")
    await client().patch(
        f"{_gp(workspace_id)}/datasets/{dataset_id}/refreshSchedule", {"value": value}
    )
    return await client().get(f"{_gp(workspace_id)}/datasets/{dataset_id}/refreshSchedule")


@write_tool()
async def update_dataset_parameters(
    dataset_id: str, parameters: dict[str, str], workspace_id: str | None = None
) -> dict:
    """Update Power Query parameter values, e.g. {"ServerName": "prod-sql"}. Refresh afterwards to apply."""
    body = {"updateDetails": [{"name": k, "newValue": v} for k, v in parameters.items()]}
    await client().post(f"{_gp(workspace_id)}/datasets/{dataset_id}/Default.UpdateParameters", body)
    return {"updated": list(parameters.keys())}


@write_tool()
async def take_over_dataset(dataset_id: str, workspace_id: str) -> dict:
    """Become the owner of a dataset (needed before updating parameters/credentials)."""
    return await client().post(
        f"/groups/{workspace_id}/datasets/{dataset_id}/Default.TakeOver", {}
    )


@write_tool(destructive=True)
async def delete_dataset(dataset_id: str, workspace_id: str | None = None) -> dict:
    """Permanently delete a dataset (and reports bound to it). Only on explicit user request."""
    return await client().delete(f"{_gp(workspace_id)}/datasets/{dataset_id}")


# =====================================================================
# Reports
# =====================================================================


@read_tool
async def list_reports(workspace_id: str | None = None) -> list[dict]:
    """List reports in a workspace (omit workspace_id for My workspace)."""
    items = await client().get_values(f"{_gp(workspace_id)}/reports")
    return _pick(items, ["id", "name", "reportType", "datasetId", "webUrl", "embedUrl"])


@read_tool
async def get_report(report_id: str, workspace_id: str | None = None) -> dict:
    """Get report metadata."""
    return await client().get(f"{_gp(workspace_id)}/reports/{report_id}")


@read_tool
async def get_report_pages(report_id: str, workspace_id: str | None = None) -> list[dict]:
    """List pages of a report (name, display name, order)."""
    data = await client().get(f"{_gp(workspace_id)}/reports/{report_id}/pages")
    return data.get("value", [])


_SAFE = re.compile(r"[^A-Za-z0-9._ -]+")


@read_tool
async def export_report(
    report_id: str,
    workspace_id: str | None = None,
    file_format: Literal["PDF", "PPTX", "PNG"] = "PDF",
    page_names: list[str] | None = None,
    report_filter: str | None = None,
    timeout_seconds: int = 600,
) -> dict:
    """Export a report to PDF/PPTX/PNG and save it locally (requires Premium/PPU/Fabric capacity).

    Args:
        page_names: Optional page *names* (from get_report_pages) to export.
        report_filter: Optional OData-style filter, e.g. "Store/Territory eq 'NC'".
    """
    gp = _gp(workspace_id)
    cfg: dict[str, Any] = {}
    if page_names:
        cfg["pages"] = [{"pageName": p} for p in page_names]
    if report_filter:
        cfg["reportLevelFilters"] = [{"filter": report_filter}]
    body: dict[str, Any] = {"format": file_format}
    if cfg:
        body["powerBIReportConfiguration"] = cfg
    job = await client().post(f"{gp}/reports/{report_id}/ExportTo", body)
    export_id = job["id"]

    deadline = time.monotonic() + timeout_seconds
    status = job.get("status")
    while status not in ("Succeeded", "Failed") and time.monotonic() < deadline:
        await asyncio.sleep(5)
        job = await client().get(f"{gp}/reports/{report_id}/exports/{export_id}")
        status = job.get("status")
    if status != "Succeeded":
        return {"status": status or "Timeout", "exportId": export_id, "details": job}

    ext = job.get("resourceFileExtension") or f".{file_format.lower()}"
    name = _SAFE.sub("_", job.get("reportName") or report_id).strip() or report_id
    dest = settings.export_dir / f"{name}_{int(time.time())}{ext}"
    await client().download(f"{gp}/reports/{report_id}/exports/{export_id}/file", dest)
    return {"status": "Succeeded", "file": str(dest.resolve()), "bytes": dest.stat().st_size}


@write_tool()
async def clone_report(
    report_id: str,
    new_name: str,
    workspace_id: str | None = None,
    target_workspace_id: str | None = None,
    target_dataset_id: str | None = None,
) -> dict:
    """Copy a report, optionally to another workspace and/or bound to another dataset."""
    body: dict[str, Any] = {"name": new_name}
    if target_workspace_id:
        body["targetWorkspaceId"] = target_workspace_id
    if target_dataset_id:
        body["targetModelId"] = target_dataset_id
    return await client().post(f"{_gp(workspace_id)}/reports/{report_id}/Clone", body)


@write_tool()
async def rebind_report(report_id: str, dataset_id: str, workspace_id: str | None = None) -> dict:
    """Point a report at a different dataset."""
    return await client().post(
        f"{_gp(workspace_id)}/reports/{report_id}/Rebind", {"datasetId": dataset_id}
    )


@write_tool(destructive=True)
async def delete_report(report_id: str, workspace_id: str | None = None) -> dict:
    """Permanently delete a report. Only on explicit user request."""
    return await client().delete(f"{_gp(workspace_id)}/reports/{report_id}")


@write_tool()
async def import_pbix(
    file_path: str,
    workspace_id: str | None = None,
    display_name: str | None = None,
    name_conflict: Literal["Abort", "CreateOrOverwrite", "Overwrite", "GenerateUniqueName"] = "Abort",
    timeout_seconds: int = 600,
) -> dict:
    """Upload a local .pbix file to a workspace (publishes its report and dataset)."""
    p = Path(file_path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"No such file: {p}")
    gp = _gp(workspace_id)
    params = {"datasetDisplayName": display_name or p.name, "nameConflict": name_conflict}
    with p.open("rb") as fh:
        job = await client().request(
            "POST",
            f"{gp}/imports",
            params=params,
            files={"file": (p.name, fh, "application/octet-stream")},
        )
    import_id = job["id"]
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = await client().get(f"{gp}/imports/{import_id}")
        if status.get("importState") in ("Succeeded", "Failed"):
            return status
        await asyncio.sleep(5)
    return {"importId": import_id, "importState": "StillPublishing"}


# =====================================================================
# Dashboards
# =====================================================================


@read_tool
async def list_dashboards(workspace_id: str | None = None) -> list[dict]:
    """List dashboards in a workspace."""
    items = await client().get_values(f"{_gp(workspace_id)}/dashboards")
    return _pick(items, ["id", "displayName", "isReadOnly", "webUrl"])


@read_tool
async def get_dashboard_tiles(dashboard_id: str, workspace_id: str | None = None) -> list[dict]:
    """List tiles on a dashboard (title, source report/dataset)."""
    data = await client().get(f"{_gp(workspace_id)}/dashboards/{dashboard_id}/tiles")
    return _pick(
        data.get("value", []), ["id", "title", "subTitle", "reportId", "datasetId", "embedUrl"]
    )


# =====================================================================
# Dataflows
# =====================================================================


@read_tool
async def list_dataflows(workspace_id: str) -> list[dict]:
    """List dataflows in a workspace."""
    return await client().get_values(f"/groups/{workspace_id}/dataflows")


@read_tool
async def get_dataflow_transactions(dataflow_id: str, workspace_id: str) -> list[dict]:
    """Get refresh transactions (history) for a dataflow."""
    data = await client().get(f"/groups/{workspace_id}/dataflows/{dataflow_id}/transactions")
    return data.get("value", [])


@write_tool()
async def refresh_dataflow(
    dataflow_id: str,
    workspace_id: str,
    notify_option: Literal["NoNotification", "MailOnFailure"] = "NoNotification",
) -> dict:
    """Trigger a dataflow refresh."""
    return await client().post(
        f"/groups/{workspace_id}/dataflows/{dataflow_id}/refreshes",
        {"notifyOption": notify_option},
    )


# =====================================================================
# Apps, gateways, capacities
# =====================================================================


@read_tool
async def list_apps() -> list[dict]:
    """List installed Power BI apps the caller can access."""
    return await client().get_values("/apps")


@read_tool
async def list_app_reports(app_id: str) -> list[dict]:
    """List reports contained in an installed app."""
    return await client().get_values(f"/apps/{app_id}/reports")


@read_tool
async def list_gateways() -> list[dict]:
    """List on-premises data gateways the caller administers."""
    return await client().get_values("/gateways")


@read_tool
async def list_gateway_datasources(gateway_id: str) -> list[dict]:
    """List data sources configured on a gateway."""
    return await client().get_values(f"/gateways/{gateway_id}/datasources")


@read_tool
async def list_capacities() -> list[dict]:
    """List Premium / Fabric / Embedded capacities the caller can access."""
    return await client().get_values("/capacities")


# =====================================================================
# Admin APIs (tenant-wide; need Fabric admin rights or admin-API-enabled SP)
# Enable with POWERBI_ENABLE_ADMIN_TOOLS=true
# =====================================================================


@admin_tool
async def admin_list_workspaces(
    top: int = 500, filter: str | None = None, expand: str | None = None
) -> list[dict]:
    """[Admin] List all workspaces in the tenant.

    Args:
        filter: OData filter, e.g. "state eq 'Active' and type eq 'Workspace'".
        expand: e.g. "users,reports,datasets".
    """
    params = {"$top": top, "$filter": filter, "$expand": expand}
    return await client().get_values(f"{settings.admin_api_base}/groups", params=params)


@admin_tool
async def admin_get_activity_events(
    start_datetime: str, end_datetime: str, filter: str | None = None, max_events: int = 5000
) -> dict:
    """[Admin] Get audit activity events. Start/end must be within the same UTC day.

    Args:
        start_datetime: e.g. "2026-11-01T00:00:00.000Z".
        end_datetime: e.g. "2026-11-01T23:59:59.999Z".
        filter: e.g. "Activity eq 'viewreport'" or "UserId eq 'a@b.com'".
    """
    params = {
        "startDateTime": f"'{start_datetime}'",
        "endDateTime": f"'{end_datetime}'",
        "$filter": filter,
    }
    events = await client().get_values(f"{settings.admin_api_base}/activityevents", params=params)
    return {"count": len(events), "truncated": len(events) > max_events, "events": events[:max_events]}


# =====================================================================
# APG BIM Intelligence
# =====================================================================


@read_tool
async def calculate_bim_health_score(
    metrics: dict[str, float], previous_metrics: dict[str, float] | None = None
) -> dict:
    """Score Revit model health / BIM compliance KPIs into a 0-100 BIM Health Score with
    Green/Yellow/Red status per KPI, category scores, trends and top risks.

    Args:
        metrics: KPI values keyed by framework key, e.g. {"warnings_count": 820,
            "cad_imports": 3, "file_size_mb": 410, "naming_compliance_pct": 91}.
            See get_bim_kpi_framework for all keys and targets.
        previous_metrics: Same keys from the previous period, to compute trends.
    """
    return bim.compute_bim_health(metrics, previous_metrics)


@read_tool
async def calculate_design_kpis(
    gfa: float,
    nsa: float | None = None,
    plot_area: float | None = None,
    building_footprint: float | None = None,
    permitted_far: float | None = None,
    permitted_plot_coverage_pct: float | None = None,
    parking_provided: int | None = None,
    parking_required: int | None = None,
    unit_mix: dict[str, int] | None = None,
) -> dict:
    """Design Development KPIs: Efficiency (NSA/GFA x 100), FAR, plot coverage, parking
    balance and unit mix, with flags for abnormal or non-compliant values.

    Args:
        unit_mix: e.g. {"Studio": 40, "1BR": 120, "2BR": 60}.
    """
    return bim.compute_design_kpis(
        gfa, nsa, plot_area, building_footprint, permitted_far,
        permitted_plot_coverage_pct, parking_provided, parking_required, unit_mix,
    )


@read_tool
async def get_bim_kpi_framework() -> str:
    """Return the APG BIM KPI framework: KPI keys and targets, dashboard hierarchy,
    DAX measure patterns and the required report format."""
    return bim.framework_markdown()


@mcp.resource(
    "apg://bim-kpi-framework",
    name="APG BIM KPI Framework",
    description="KPI definitions, targets, dashboard hierarchy, DAX patterns and report format",
    mime_type="text/markdown",
)
def bim_kpi_framework_resource() -> str:
    return bim.framework_markdown()


# =====================================================================
# Prompts
# =====================================================================


@mcp.prompt()
def bim_executive_report(project: str, dataset_name: str | None = None) -> str:
    """Produce an APG executive BIM / project health report from Power BI data."""
    src = f" from the Power BI semantic model '{dataset_name}'" if dataset_name else ""
    return (
        f"Prepare an executive BIM intelligence report for project '{project}'{src}.\n\n"
        "1) Read get_bim_kpi_framework. 2) Locate the dataset (list_workspaces / list_datasets) "
        "and read get_dataset_schema. 3) Pull current and previous-period values for model "
        "health, BIM compliance, design, delivery and team KPIs with execute_dax_query. "
        "4) Score them with calculate_bim_health_score and calculate_design_kpis. "
        "5) Write the report in the required format:\n" + bim.REPORT_FORMAT
    )


@mcp.prompt()
def design_power_bi_dashboard(
    audience: str = "Executive", project: str | None = None
) -> str:
    """Design an APG Power BI dashboard (Executive, BIM Manager, Design or Delivery)."""
    p = f" for project '{project}'" if project else ""
    return (
        f"Design the APG {audience} Power BI dashboard{p}. Use the dashboard hierarchy and "
        "design rules in get_bim_kpi_framework. Specify: page layout, each visual (type, "
        "fields, measure), KPI cards with targets and RAG colouring, trend charts, drill-through "
        "pages, slicers (Project, Discipline, Stage, Building, Date), and the DAX measures "
        "needed. If a dataset exists, check its schema with get_dataset_schema first and map "
        "visuals to real tables/columns; list any missing data/shared parameters to add in Revit."
    )


@mcp.prompt()
def analyze_dataset(dataset_name: str, question: str) -> str:
    """Guide Claude through answering a business question from a Power BI dataset."""
    return (
        f"Answer this question using the Power BI semantic model '{dataset_name}': {question}\n\n"
        "Steps: 1) find the workspace and dataset ids with list_workspaces / list_datasets; "
        "2) call get_dataset_schema to learn tables, columns, measures and relationships; "
        "3) write DAX that prefers existing measures, and run it with execute_dax_query; "
        "4) iterate if needed, then report using this format, and show the DAX you used:\n"
        + bim.REPORT_FORMAT
    )


def run(transport: str = "stdio", **kwargs: Any) -> None:
    mcp.run(transport=transport, **kwargs)  # type: ignore[arg-type]
