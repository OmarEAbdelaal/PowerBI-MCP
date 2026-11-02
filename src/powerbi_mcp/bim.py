"""APG BIM Intelligence layer.

Based on the "APG BIM Intelligence & Power BI Assistant" project instructions:
turns Revit / BIM data held in Power BI into KPIs, a 0-100 BIM Health Score,
RAG (Green / Yellow / Red) statuses and executive reports.

Default targets below are starting points - tune them per project/company by
pointing APG_KPI_TARGETS_FILE at a JSON file with the same shape as
DEFAULT_TARGETS (only the keys you want to change are needed).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# KPI targets
#   direction "lower": value <= green is Green, <= yellow is Yellow, else Red
#   direction "higher": value >= green is Green, >= yellow is Yellow, else Red
#   weight: share in the BIM Health Score
# --------------------------------------------------------------------------
DEFAULT_TARGETS: dict[str, dict[str, Any]] = {
    # Model health
    "file_size_mb": {"label": "File Size (MB)", "category": "Model Health", "direction": "lower", "green": 300, "yellow": 500, "weight": 1},
    "warnings_count": {"label": "Warnings", "category": "Model Health", "direction": "lower", "green": 500, "yellow": 1500, "weight": 3},
    "cad_imports": {"label": "CAD Imports", "category": "Model Health", "direction": "lower", "green": 0, "yellow": 5, "weight": 2},
    "linked_cad_files": {"label": "Linked CAD Files", "category": "Model Health", "direction": "lower", "green": 10, "yellow": 25, "weight": 1},
    "in_place_families": {"label": "In-Place Families", "category": "Model Health", "direction": "lower", "green": 10, "yellow": 25, "weight": 2},
    "groups_count": {"label": "Groups", "category": "Model Health", "direction": "lower", "green": 50, "yellow": 150, "weight": 1},
    "unused_views": {"label": "Unused Views", "category": "Model Health", "direction": "lower", "green": 100, "yellow": 300, "weight": 1},
    "unused_sheets": {"label": "Unused Sheets", "category": "Model Health", "direction": "lower", "green": 10, "yellow": 30, "weight": 1},
    "duplicate_types": {"label": "Duplicate Types", "category": "Model Health", "direction": "lower", "green": 20, "yellow": 60, "weight": 1},
    "family_count": {"label": "Family Count", "category": "Model Health", "direction": "lower", "green": 600, "yellow": 1000, "weight": 1},
    "workset_count": {"label": "Worksets", "category": "Model Health", "direction": "lower", "green": 30, "yellow": 50, "weight": 0.5},
    "model_open_time_min": {"label": "Model Open Time (min)", "category": "Model Health", "direction": "lower", "green": 3, "yellow": 6, "weight": 1},
    "sync_time_min": {"label": "Synchronization Time (min)", "category": "Model Health", "direction": "lower", "green": 2, "yellow": 5, "weight": 1},
    # BIM compliance (percent compliant, 0-100)
    "naming_compliance_pct": {"label": "Naming Compliance %", "category": "BIM Compliance", "direction": "higher", "green": 95, "yellow": 85, "weight": 2},
    "shared_parameter_compliance_pct": {"label": "Shared Parameter Compliance %", "category": "BIM Compliance", "direction": "higher", "green": 95, "yellow": 85, "weight": 2},
    "view_template_compliance_pct": {"label": "View Template Compliance %", "category": "BIM Compliance", "direction": "higher", "green": 90, "yellow": 75, "weight": 1.5},
    "workset_compliance_pct": {"label": "Workset Compliance %", "category": "BIM Compliance", "direction": "higher", "green": 95, "yellow": 85, "weight": 1.5},
    "family_standards_compliance_pct": {"label": "Family Standards Compliance %", "category": "BIM Compliance", "direction": "higher", "green": 90, "yellow": 75, "weight": 1.5},
    "sheet_standards_compliance_pct": {"label": "Sheet Standards Compliance %", "category": "BIM Compliance", "direction": "higher", "green": 95, "yellow": 85, "weight": 1.5},
    "model_audit_score": {"label": "Model Audit Score", "category": "BIM Compliance", "direction": "higher", "green": 85, "yellow": 70, "weight": 2},
}

# Design KPI sanity ranges (used to flag abnormal values)
DESIGN_RANGES: dict[str, tuple[float, float]] = {
    "efficiency_pct": (70.0, 90.0),  # NSA / GFA x 100
}


def load_targets() -> dict[str, dict[str, Any]]:
    targets = json.loads(json.dumps(DEFAULT_TARGETS))
    path = os.environ.get("APG_KPI_TARGETS_FILE")
    if path and Path(path).expanduser().is_file():
        overrides = json.loads(Path(path).expanduser().read_text())
        for key, cfg in overrides.items():
            targets.setdefault(key, {}).update(cfg)
    return targets


def rag_status(value: float, cfg: dict[str, Any]) -> str:
    green, yellow = cfg["green"], cfg["yellow"]
    if cfg.get("direction", "lower") == "lower":
        return "Green" if value <= green else "Yellow" if value <= yellow else "Red"
    return "Green" if value >= green else "Yellow" if value >= yellow else "Red"


def _kpi_score(value: float, cfg: dict[str, Any]) -> float:
    """0-100 score for a single KPI: 100 at/inside green, 60 at the yellow limit, 0 at 2x past it."""
    green, yellow = float(cfg["green"]), float(cfg["yellow"])
    lower = cfg.get("direction", "lower") == "lower"
    if (lower and value <= green) or (not lower and value >= green):
        return 100.0
    span = abs(yellow - green) or 1.0
    dist = (value - green) if lower else (green - value)
    if dist <= span:  # between green and yellow -> 100..60
        return 100.0 - 40.0 * dist / span
    # past yellow -> 60..0 over another span
    return max(0.0, 60.0 - 60.0 * (dist - span) / span)


def compute_bim_health(
    metrics: dict[str, float],
    previous: dict[str, float] | None = None,
    targets: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    targets = targets or load_targets()
    rows, unknown = [], []
    weighted, total_w = 0.0, 0.0
    cat_scores: dict[str, list[tuple[float, float]]] = {}
    for key, value in metrics.items():
        cfg = targets.get(key)
        if cfg is None or value is None:
            unknown.append(key)
            continue
        value = float(value)
        score = _kpi_score(value, cfg)
        w = float(cfg.get("weight", 1))
        weighted += score * w
        total_w += w
        cat_scores.setdefault(cfg.get("category", "Other"), []).append((score, w))
        row: dict[str, Any] = {
            "kpi": cfg.get("label", key),
            "key": key,
            "category": cfg.get("category"),
            "current": value,
            "target": cfg["green"],
            "status": rag_status(value, cfg),
            "score": round(score, 1),
        }
        if previous and key in previous and previous[key] is not None:
            delta = value - float(previous[key])
            better = delta < 0 if cfg.get("direction", "lower") == "lower" else delta > 0
            row["trend"] = "flat" if delta == 0 else ("improving" if better else "worsening")
            row["change"] = round(delta, 2)
        rows.append(row)

    overall = round(weighted / total_w, 1) if total_w else None
    by_cat = {
        c: round(sum(s * w for s, w in v) / sum(w for _, w in v), 1) for c, v in cat_scores.items()
    }
    status = (
        None
        if overall is None
        else "Green" if overall >= 80 else "Yellow" if overall >= 60 else "Red"
    )
    rows.sort(key=lambda r: ({"Red": 0, "Yellow": 1, "Green": 2}[r["status"]], r["score"]))
    return {
        "bim_health_score": overall,
        "status": status,
        "category_scores": by_cat,
        "kpis": rows,
        "top_risks": [r for r in rows if r["status"] == "Red"][:5],
        "ignored_metrics": unknown,
        "scoring": "Weighted 0-100: KPI at/inside target = 100, at warning limit = 60, "
        "twice past the warning limit = 0. Health >= 80 Green, >= 60 Yellow, else Red.",
    }


def compute_design_kpis(
    gfa: float,
    nsa: float | None = None,
    plot_area: float | None = None,
    building_footprint: float | None = None,
    permitted_far: float | None = None,
    permitted_plot_coverage_pct: float | None = None,
    parking_provided: int | None = None,
    parking_required: int | None = None,
    unit_mix: dict[str, int] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"gfa": gfa}
    flags: list[str] = []
    if gfa <= 0:
        raise ValueError("GFA must be greater than 0")
    if nsa is not None:
        eff = round(nsa / gfa * 100, 2)
        out["efficiency_pct"] = eff
        lo, hi = DESIGN_RANGES["efficiency_pct"]
        if nsa > gfa:
            flags.append("NSA is larger than GFA - check area schemes / area boundaries.")
        elif eff < lo:
            flags.append(f"Efficiency {eff}% is below the {lo:.0f}% benchmark - review cores, circulation and back-of-house.")
        elif eff > hi:
            flags.append(f"Efficiency {eff}% is above {hi:.0f}% - unusually high, verify NSA/GFA measurement rules.")
    if plot_area:
        far = round(gfa / plot_area, 3)
        out["far"] = far
        if permitted_far is not None:
            out["far_permitted"] = permitted_far
            out["far_compliant"] = far <= permitted_far
            out["far_headroom_gfa"] = round(permitted_far * plot_area - gfa, 2)
            if far > permitted_far:
                flags.append(f"FAR {far} exceeds permitted {permitted_far} by {round(gfa - permitted_far * plot_area, 2)} of GFA.")
        if building_footprint is not None:
            cov = round(building_footprint / plot_area * 100, 2)
            out["plot_coverage_pct"] = cov
            if permitted_plot_coverage_pct is not None:
                out["plot_coverage_compliant"] = cov <= permitted_plot_coverage_pct
                if cov > permitted_plot_coverage_pct:
                    flags.append(f"Plot coverage {cov}% exceeds permitted {permitted_plot_coverage_pct}%.")
    if parking_provided is not None:
        out["parking_provided"] = parking_provided
        if parking_required is not None:
            out["parking_required"] = parking_required
            out["parking_balance"] = parking_provided - parking_required
            if parking_provided < parking_required:
                flags.append(f"Parking shortfall of {parking_required - parking_provided} spaces.")
    if unit_mix:
        total = sum(unit_mix.values())
        out["unit_count"] = total
        out["unit_mix_pct"] = {k: round(v / total * 100, 1) for k, v in unit_mix.items()} if total else {}
    out["flags"] = flags
    out["status"] = "Red" if any("exceeds" in f or "shortfall" in f for f in flags) else "Yellow" if flags else "Green"
    return out


# --------------------------------------------------------------------------
# Reference text exposed to Claude as an MCP resource
# --------------------------------------------------------------------------
REPORT_FORMAT = """\
Every analysis uses this structure:
## Summary - concise executive summary.
## Key Findings - major observations.
## Risks - project risks.
## Recommendations - prioritised actions.
## KPI Table - KPI | Current | Target | Trend | Status (Green / Yellow / Red).
Interpret the numbers (trends, risks, anomalies, benchmark comparison, KPI impact);
never just report raw data. Audience: BIM Managers, Design Directors, Project
Managers and executive leadership - focus on decisions.
"""


def framework_markdown() -> str:
    t = load_targets()
    lines = [
        "# APG BIM Intelligence KPI Framework",
        "",
        "Company: APG - Architecture, Interior Design, Landscape Architecture.",
        "Stages: Concept, SD, DD, IFC/Tender, Construction Documentation, Construction Administration.",
        "Platforms: Revit, Autodesk Construction Cloud, Power BI, Excel, SharePoint.",
        "Standards: ISO 19650, APG BIM Standards, APG Naming Conventions, APG Shared Parameters.",
        "",
        "## Scored KPIs (used by calculate_bim_health_score)",
        "",
        "| Key | KPI | Category | Better | Green | Yellow limit | Weight |",
        "|---|---|---|---|---|---|---|",
    ]
    for k, c in t.items():
        lines.append(
            f"| `{k}` | {c.get('label', k)} | {c.get('category', '')} | {c.get('direction', 'lower')} "
            f"| {c['green']} | {c['yellow']} | {c.get('weight', 1)} |"
        )
    lines += [
        "",
        "## Design Development KPIs",
        "Rooms, Areas, GFA, NSA, Efficiency = NSA / GFA x 100 (benchmark 70-90%), Parking,",
        "Unit Mix, Building Areas, Plot Coverage, FAR compliance -> calculate_design_kpis.",
        "",
        "## Project Delivery KPIs",
        "Sheets produced / approved, Views created / placed, RFIs, Issues open / closed, Clash count,",
        "Coordination status.",
        "",
        "## Team Performance KPIs",
        "Modelling progress, Documentation progress, Sheets per user, Model ownership,",
        "Weekly production rate, workload distribution.",
        "",
        "## Dashboard hierarchy",
        "1. Executive (Directors, Design/Project Managers): Project Health Score, BIM Health Score,",
        "   Design Progress, Key Risks, Issue Summary, Project Stage, Area Statistics.",
        "2. BIM Manager: Warnings, CAD Imports, Family Health, Worksets, Naming Compliance, Audit Results.",
        "3. Design: Area Analysis, Unit Mix, Efficiency, Program Distribution, Space Utilisation.",
        "4. Delivery: Sheets, Issues, Milestones, Progress Tracking.",
        "Design rules: minimal clutter, executive readability, KPI cards, trend charts, drill-through",
        "pages, slicers for Project / Discipline / Stage / Building / Date.",
        "",
        "## Useful DAX measure patterns",
        "```DAX",
        "Efficiency % = DIVIDE ( [NSA], [GFA] ) * 100",
        "FAR = DIVIDE ( [GFA], [Plot Area] )",
        "Plot Coverage % = DIVIDE ( [Building Footprint], [Plot Area] ) * 100",
        "Sheets Approved % = DIVIDE ( [Sheets Approved], [Sheets Produced] ) * 100",
        "Views Placed % = DIVIDE ( [Views Placed], [Views Created] ) * 100",
        "Issue Closure % = DIVIDE ( [Issues Closed], [Issues Closed] + [Issues Open] ) * 100",
        "Warnings Trend = [Warnings] - CALCULATE ( [Warnings], DATEADD ( 'Date'[Date], -7, DAY ) )",
        "KPI Status = SWITCH ( TRUE (), [Value] <= [Green Target], \"Green\", [Value] <= [Yellow Limit], \"Yellow\", \"Red\" )",
        "```",
        "",
        "## Reporting format",
        REPORT_FORMAT,
    ]
    return "\n".join(lines)
