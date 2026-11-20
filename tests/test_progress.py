from concurrent.futures import ThreadPoolExecutor

from support import bearer, report, watch_to


def test_exact_80_percent_unlocks_only_the_linked_task(client, register, clock):
    _, _, login = register()
    headers = bearer(login)
    lecture = client.get("/api/v1/lectures", headers=headers).json()["lectures"][0]
    task = client.get("/api/v1/tasks", headers=headers).json()[0]
    assert not task["available"]
    assert client.get(f"/api/v1/tasks/{task['id']}", headers=headers).status_code == 403
    progress = watch_to(client, headers, clock, lecture["id"], 479)
    assert progress == {
        "progress_rate": 0.79,
        "completed": False,
        "just_completed": False,
    }
    assert client.get(f"/api/v1/tasks/{task['id']}", headers=headers).status_code == 403
    clock.advance(seconds=1)
    assert report(client, headers, lecture["id"], [[479, 480]], 480).json()[
        "just_completed"
    ]
    detail = client.get(f"/api/v1/lectures/{lecture['id']}", headers=headers).json()
    assert (
        detail["completed"]
        and detail["task_id"] == task["id"]
        and detail["last_position_sec"] == 480
    )
    details = client.get(f"/api/v1/tasks/{task['id']}", headers=headers).json()
    assert details["criteria"][0]["max_score"] == 50
    assert all("content" not in material for material in details["materials"])
    assert "rule" not in details["pass_requirements"]["required_checks"][0]


def test_report_budget_seek_and_concurrent_overlap(client, register, clock):
    _, _, login = register()
    headers = bearer(login)
    lecture = client.get("/api/v1/lectures", headers=headers).json()["lectures"][0][
        "id"
    ]
    first = report(client, headers, lecture, [[0, 600]], 600).json()
    assert (
        first["progress_rate"] == 0.03
    )  # First report can claim only fifteen seconds.
    clock.advance(seconds=10)

    def send(_):
        return report(client, headers, lecture, [[100, 110]], 110)

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(send, range(2)))
    assert all(response.status_code == 200 for response in responses)
    detail = client.get(f"/api/v1/lectures/{lecture}", headers=headers).json()
    assert detail["progress_rate"] == 0.04 and not detail["completed"]


def test_repeated_reports_cannot_multiply_timing_tolerance(client, register, clock):
    _, _, login = register()
    headers = bearer(login)
    lecture = client.get("/api/v1/lectures", headers=headers).json()["lectures"][0][
        "id"
    ]
    report(client, headers, lecture, [[0, 15]], 15)
    for start in range(15, 115, 5):
        progress = report(
            client, headers, lecture, [[start, start + 5]], start + 5
        ).json()
    assert progress["progress_rate"] == 0.03
    clock.advance(seconds=10)
    assert (
        report(client, headers, lecture, [[20, 30]], 30).json()["progress_rate"] == 0.05
    )
