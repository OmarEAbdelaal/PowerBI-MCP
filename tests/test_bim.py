import json

from powerbi_mcp import bim


def test_health_score_all_green():
    r = bim.compute_bim_health({"warnings_count": 100, "cad_imports": 0, "naming_compliance_pct": 99})
    assert r["bim_health_score"] == 100.0 and r["status"] == "Green"
    assert all(k["status"] == "Green" for k in r["kpis"])


def test_health_score_rag_trend_and_risks():
    r = bim.compute_bim_health(
        {"warnings_count": 4000, "cad_imports": 3, "naming_compliance_pct": 97, "bogus": 1},
        previous={"warnings_count": 3000, "cad_imports": 5},
    )
    by = {k["key"]: k for k in r["kpis"]}
    assert by["warnings_count"]["status"] == "Red" and by["warnings_count"]["trend"] == "worsening"
    assert by["cad_imports"]["status"] == "Yellow" and by["cad_imports"]["trend"] == "improving"
    assert by["naming_compliance_pct"]["status"] == "Green"
    assert r["top_risks"][0]["key"] == "warnings_count"
    assert r["ignored_metrics"] == ["bogus"]
    assert 0 < r["bim_health_score"] < 100
    assert r["kpis"][0]["status"] == "Red"  # sorted worst first


def test_kpi_score_boundaries():
    cfg = {"direction": "lower", "green": 500, "yellow": 1500}
    assert bim._kpi_score(500, cfg) == 100
    assert bim._kpi_score(1500, cfg) == 60
    assert bim._kpi_score(2500, cfg) == 0
    hi = {"direction": "higher", "green": 95, "yellow": 85}
    assert bim._kpi_score(85, hi) == 60 and bim._kpi_score(99, hi) == 100


def test_targets_override_file(tmp_path, monkeypatch):
    f = tmp_path / "t.json"
    f.write_text(json.dumps({"warnings_count": {"green": 50, "yellow": 100}}))
    monkeypatch.setenv("APG_KPI_TARGETS_FILE", str(f))
    t = bim.load_targets()
    assert t["warnings_count"]["green"] == 50 and t["warnings_count"]["weight"] == 3


def test_design_kpis():
    r = bim.compute_design_kpis(
        gfa=10000, nsa=8000, plot_area=4000, building_footprint=2400, permitted_far=2.0,
        permitted_plot_coverage_pct=50, parking_provided=90, parking_required=100,
        unit_mix={"1BR": 60, "2BR": 40},
    )
    assert r["efficiency_pct"] == 80.0
    assert r["far"] == 2.5 and r["far_compliant"] is False
    assert r["plot_coverage_pct"] == 60.0 and r["plot_coverage_compliant"] is False
    assert r["parking_balance"] == -10
    assert r["unit_mix_pct"] == {"1BR": 60.0, "2BR": 40.0}
    assert r["status"] == "Red" and len(r["flags"]) == 3


def test_design_kpis_low_efficiency_flag():
    r = bim.compute_design_kpis(gfa=10000, nsa=6000)
    assert r["status"] == "Yellow" and "below" in r["flags"][0]


def test_framework_markdown_has_sections():
    md = bim.framework_markdown()
    for s in ("warnings_count", "Dashboard hierarchy", "Efficiency % = DIVIDE", "## Risks"):
        assert s in md
