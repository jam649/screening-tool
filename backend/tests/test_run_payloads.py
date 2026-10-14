"""Large run manifests remain durable without flooding routine API views."""

from app.models import CSLVersion, ScreeningRun, utcnow


def test_routine_run_lists_bound_large_manifests_but_detail_preserves_provenance(
    client, app, admin
):
    scope = {
        "party_ids": [f"synthetic-party-{n}" for n in range(20000)],
        "party_hashes": {f"synthetic-party-{n}": "a" * 64 for n in range(20000)},
        "entry_ids": [f"public-entry-{n}" for n in range(25000)],
        "entry_hashes": {f"public-entry-{n}": "b" * 64 for n in range(25000)},
    }
    with app.state.session_factory() as db:
        version = CSLVersion(file_sha256="c" * 64, entry_count=25000)
        db.add(version)
        db.flush()
        run = ScreeningRun(
            trigger="manual",
            csl_version_id=version.id,
            finished_at=utcnow(),
            parties_screened=20000,
            settings_snapshot={"hash_rule_version": 2, "candidate_min": 50, "scope": scope},
        )
        db.add(run)
        db.commit()
        run_id = run.id
    for path in ("/api/v1/runs", "/api/v1/screen/runs"):
        response = client.get(path)
        assert response.status_code == 200
        assert len(response.content) < 3000
        body = response.json()
        row = body[0] if isinstance(body, list) else body["items"][0]
        assert row["settings_snapshot"]["scope"] == {"party_count": 20000, "entry_count": 25000}
        assert row["settings_snapshot"]["hash_rule_version"] == 2
    detail = client.get(f"/api/v1/screen/runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["settings_snapshot"]["scope"] == scope
    with app.state.session_factory() as db:
        assert db.get(ScreeningRun, run_id).settings_snapshot["scope"] == scope
