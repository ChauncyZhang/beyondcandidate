from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from server.tests.test_interview_api import (
    create_interview,
    interview_payload,
    make_app,
    seed_application,
)
from server.tests.test_recruiting_api import login


EXISTING_START = datetime(2099, 7, 20, 7, 0, tzinfo=timezone.utc)
EXISTING_END = EXISTING_START + timedelta(hours=1)


def hour_payload(seed, start):
    return {
        **interview_payload(seed, starts_at=start),
        "ends_at": (start + timedelta(hours=1)).isoformat(),
    }


@pytest.mark.parametrize("direction", ["before", "after"])
@pytest.mark.parametrize("existing_check", [False, True])
def test_conflict_checks_allow_default_adjacency_but_preserve_explicit_buffer(
    tmp_path, direction, existing_check
):
    app = make_app(tmp_path)
    seed = seed_application(app)
    adjacent_start = EXISTING_START - timedelta(hours=1) if direction == "before" else EXISTING_END
    with TestClient(app) as client:
        existing, headers = create_interview(
            client, seed, key="adjacency-existing", payload=hour_payload(seed, EXISTING_START)
        )
        existing_id = existing.json()["data"]["id"]
        schedule = {
            "starts_at": adjacent_start.isoformat(),
            "ends_at": (adjacent_start + timedelta(hours=1)).isoformat(),
            "participant_ids": [str(seed["interviewer_id"])],
        }
        if existing_check:
            movable, headers = create_interview(
                client, seed, key="adjacency-movable",
                payload=hour_payload(seed, EXISTING_START + timedelta(hours=4)),
            )
            path = f"/api/v1/interviews/{movable.json()['data']['id']}/conflicts"
        else:
            path = "/api/v1/interview-conflicts"
            schedule["application_id"] = str(seed["application_id"])

        default = client.post(path, json=schedule, headers=headers)
        buffered = client.post(path, json={**schedule, "buffer_minutes": 15}, headers=headers)
        overlap_start = EXISTING_START + timedelta(minutes=30)
        overlap = client.post(path, json={
            **schedule,
            "starts_at": overlap_start.isoformat(),
            "ends_at": (overlap_start + timedelta(hours=1)).isoformat(),
        }, headers=headers)

    assert default.status_code == 200
    assert default.json()["data"] == {
        "hard": [], "soft": [], "calendar_hard": [], "calendar_soft": [], "unconfirmed": [],
    }
    assert buffered.status_code == 200
    assert buffered.json()["data"] == {
        "hard": [], "soft": [existing_id], "calendar_hard": [],
        "calendar_soft": [str(seed["interviewer_id"])], "unconfirmed": [],
    }
    assert overlap.status_code == 200
    assert overlap.json()["data"] == {
        "hard": [existing_id], "soft": [],
        "calendar_hard": [str(seed["interviewer_id"])], "calendar_soft": [], "unconfirmed": [],
    }


@pytest.mark.parametrize("direction", ["before", "after"])
def test_availability_defaults_to_no_buffer_and_honors_explicit_buffer(tmp_path, direction):
    app = make_app(tmp_path)
    seed = seed_application(app)
    adjacent_start = EXISTING_START - timedelta(hours=1) if direction == "before" else EXISTING_END
    with TestClient(app) as client:
        _, headers = create_interview(
            client, seed, key="availability-existing", payload=hour_payload(seed, EXISTING_START)
        )
        params = {
            "from": adjacent_start.isoformat(),
            "to": (adjacent_start + timedelta(hours=1)).isoformat(),
            "participant_ids": str(seed["interviewer_id"]),
            "timezone": "Asia/Shanghai",
        }
        default = client.get("/api/v1/interview-availability", params=params, headers=headers)
        buffered = client.get(
            "/api/v1/interview-availability", params={**params, "buffer": 15}, headers=headers
        )

    assert default.status_code == 200
    assert default.json()["data"]["buffer_minutes"] == 0
    assert default.json()["data"]["participants"] == [{
        "participant_id": str(seed["interviewer_id"]), "status": "confirmed", "busy": [],
    }]
    assert buffered.status_code == 200
    assert buffered.json()["data"]["buffer_minutes"] == 15
    assert buffered.json()["data"]["participants"][0]["busy"] == [{
        "starts_at": EXISTING_START.isoformat(), "ends_at": EXISTING_END.isoformat(),
    }]


@pytest.mark.parametrize("direction", ["before", "after"])
@pytest.mark.parametrize("operation", ["create", "reschedule"])
@pytest.mark.parametrize("conflict_source", ["internal", "calendar"])
def test_authoritative_writes_allow_adjacency_and_reject_overlap(
    tmp_path, direction, operation, conflict_source
):
    app = make_app(tmp_path)
    seed = seed_application(app)

    class BusyCalendar:
        def availability(self, **kwargs):
            return [{
                "participant_id": str(participant_id), "status": "confirmed",
                "busy": [{"starts_at": EXISTING_START.isoformat(), "ends_at": EXISTING_END.isoformat()}],
            } for participant_id in kwargs["participant_ids"]]

    adjacent_start = EXISTING_START - timedelta(hours=1) if direction == "before" else EXISTING_END
    with TestClient(app) as client:
        headers = login(client, "interview-admin@example.test")
        if conflict_source == "internal":
            _, headers = create_interview(
                client, seed, key="write-existing", payload=hour_payload(seed, EXISTING_START)
            )
        if operation == "reschedule":
            movable, headers = create_interview(
                client, seed, key="write-movable",
                payload=hour_payload(seed, EXISTING_START + timedelta(hours=4)),
            )
            movable_id = movable.json()["data"]["id"]
        if conflict_source == "calendar":
            app.state.interview_availability_provider = BusyCalendar()

        def write(start, key):
            payload = hour_payload(seed, start)
            if operation == "create":
                return client.post("/api/v1/interviews", json=payload, headers={
                    **headers, "Idempotency-Key": key,
                })
            return client.patch(f"/api/v1/interviews/{movable_id}", json={
                "starts_at": payload["starts_at"], "ends_at": payload["ends_at"],
                "participants": payload["participants"],
            }, headers={**headers, "If-Match": '"1"'})

        overlapping = write(EXISTING_START + timedelta(minutes=30), "write-overlap")
        assert overlapping.status_code == 409
        assert overlapping.json()["code"] == "schedule_hard_conflict"
        adjacent = write(adjacent_start, "write-adjacent")
        assert adjacent.status_code == (201 if operation == "create" else 200), adjacent.text
        assert adjacent.json()["data"]["starts_at"] == adjacent_start.isoformat()
        assert adjacent.json()["data"]["ends_at"] == (adjacent_start + timedelta(hours=1)).isoformat()
