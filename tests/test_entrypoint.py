from main import app


def test_repository_entrypoint_serves_the_backend_contract():
    paths = app.openapi()["paths"]
    assert "/health" in paths
    assert {"/health/live", "/health/ready", "/health/grading"} <= set(paths)
    assert "/api/v1/jobs" in paths
    assert "/api/v1/attempts/{attempt_id}/submit" in paths
