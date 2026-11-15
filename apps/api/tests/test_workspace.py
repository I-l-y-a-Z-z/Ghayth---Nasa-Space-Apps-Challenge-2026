from agrix.models import Workspace


def test_empty_workspace_is_read_only_and_seed_is_explicit(client):
    workspace = client.get("/workspace")
    assert workspace.status_code == 200
    empty = Workspace.model_validate(workspace.json())
    assert not empty.plots and not empty.plans and not empty.weather_samples
    assert len(empty.catalog.crops) == 3
    populated = client.post("/demo/seed")
    assert populated.status_code == 200, populated.text
    data = Workspace.model_validate(populated.json())
    assert len(data.plots) == len(data.plans) == len(data.scenarios) == 6
    assert len(data.weather_samples) == 3
    assert sum(len(s.rows) for s in data.weather_samples) == 93
    assert {p.evaluation_status for p in data.plans} == {
        "fits_current_inputs",
        "needs_confirmation",
        "doesnt_fit",
    }
    assert all(not s.synthetic for s in data.weather_samples)
    assert client.get("/workspace").json() == populated.json()
    assert client.post("/demo/seed").json() == populated.json()


def test_workspace_project_scope_and_public_weather_context(client):
    first = client.post("/demo/seed").json()
    other_header = {"X-Project-ID": "separate-testing-project"}
    empty = client.get("/workspace", headers=other_header).json()
    assert not empty["plots"] and not empty["plans"] and not empty["scenarios"]
    assert len(empty["weather_samples"]) == 3
    second = client.post("/demo/seed", headers=other_header).json()
    assert {p["id"] for p in first["plots"]}.isdisjoint(p["id"] for p in second["plots"])
    assert {p["id"] for p in first["plans"]}.isdisjoint(p["id"] for p in second["plans"])
    assert all(p["project_id"] == "separate-testing-project" for p in second["plots"])


def test_workspace_keeps_saved_plans_without_loading_full_snapshots(client):
    seeded = client.post("/demo/seed").json()
    summary = seeded["plans"][0]
    assert "evaluation_snapshot" not in summary
    plan = client.get("/plans/" + summary["id"]).json()
    assert plan["evaluation_snapshot"]["synthetic"] is True
    assert (
        plan["evaluation_snapshot"]["input_snapshot"]["catalog"]["version"]
        == "synthetic-catalog-1.1.0"
    )
    assert plan["evaluation_id"] == summary["evaluation_id"]
    assert all(e["synthetic"] for e in plan["evaluation_snapshot"]["evidence"])


def test_testing_ui_assets_are_served_by_the_same_app(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Ghayth" in response.text
    for file in ("app.js", "styles.css"):
        asset = client.get("/assets/" + file)
        assert asset.status_code == 200 and len(asset.content) > 100
    assert client.get("/assets/../../pyproject.toml").status_code == 404


def test_reopening_reads_analysis_without_restarting_or_crossing_scope(client):
    from agrix.demo import demo_plot

    plot = client.post("/plots", json=demo_plot().model_dump(mode="json")).json()
    path = f"/plots/{plot['id']}/analyses/latest"
    assert client.get(path).status_code == 404
    analysis = client.post(
        f"/plots/{plot['id']}/analyses", json={"plot_revision": plot["revision"]}
    ).json()
    for _ in range(2):
        assert client.get(path).json()["id"] == analysis["id"]
    assert client.get(path, headers={"X-Project-ID": "another-project"}).status_code == 404
    client.patch(
        f"/plots/{plot['id']}", json={"expected_revision": plot["revision"], "name": "Changed plot"}
    ).raise_for_status()
    assert client.get(path).status_code == 404
