from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.models.enums import UrgencyLevel
from app.services import scheduling_service
from tests.conftest import referral_payload


def test_create_referral_server_side_ownership(client, world, make_referral):
    ref = make_referral()
    assert ref["status"] == "referred"
    assert ref["assigned_health_worker_id"] == world.hw1.id
    assert ref["referring_facility_id"] == world.phc_a.id
    assert ref["referral_reference"].startswith("SC-")
    assert ref["barrier_type"] in ("financial", "transportation")  # mock Agent 1 ran
    assert ref["patient_name"] is None  # none supplied


def test_client_cannot_override_ownership_or_unknown_fields(client, world):
    r = client.post("/api/referrals/", headers=world.h["hw1"],
                    json=referral_payload(world, assigned_health_worker_id=world.hw2.id))
    assert r.status_code == 403
    r = client.post("/api/referrals/", headers=world.h["hw1"], json=referral_payload(world, status="confirmed"))
    assert r.status_code == 422
    r = client.post("/api/referrals/", headers=world.h["fs_b"], json=referral_payload(world))
    assert r.status_code == 403


def test_validation_errors_have_standard_format(client, world):
    r = client.post("/api/referrals/", headers=world.h["hw1"],
                    json=referral_payload(world, human_assigned_urgency="whenever", age=500))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error" and {d["field"] for d in err["details"]} >= {"human_assigned_urgency", "age"}
    r = client.post("/api/referrals/", headers=world.h["hw1"],
                    json=referral_payload(world, receiving_facility_id=world.phc_a.id))
    assert r.status_code == 400  # same facility
    r = client.post("/api/referrals/", headers=world.h["hw1"], json=referral_payload(world, receiving_facility_id=999))
    assert r.status_code == 400


def test_unique_reference_generation(client, world, make_referral):
    refs = {make_referral(patient_identifier=f"PT-{i}")["referral_reference"] for i in range(15)}
    assert len(refs) == 15


def test_deadline_calculation_by_urgency():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    hours = get_settings().deadline_hours
    for urgency in UrgencyLevel:
        d = scheduling_service.calculate_deadline(urgency, start)
        assert d == start + timedelta(hours=hours[urgency.value]) and d.tzinfo is not None
    assert (scheduling_service.calculate_deadline(UrgencyLevel.EMERGENCY, start)
            < scheduling_service.calculate_deadline(UrgencyLevel.ROUTINE, start))


def test_referral_deadline_is_stored_and_timezone_aware(client, world, make_referral):
    ref = make_referral(human_assigned_urgency="emergency")
    created = datetime.fromisoformat(ref["created_at"])
    deadline = datetime.fromisoformat(ref["deadline"])
    assert deadline.tzinfo is not None
    assert abs((deadline - created) - timedelta(hours=get_settings().deadline_hours_emergency)) < timedelta(seconds=2)


def test_urgency_update_recomputes_deadline_deterministically(client, world, make_referral):
    ref = make_referral(human_assigned_urgency="routine")
    r = client.patch(f"/api/referrals/{ref['id']}", json={"human_assigned_urgency": "emergency"}, headers=world.h["hw1"])
    assert r.status_code == 200
    created = datetime.fromisoformat(r.json()["created_at"])
    assert datetime.fromisoformat(r.json()["deadline"]) - created == timedelta(hours=get_settings().deadline_hours_emergency)


def test_list_filter_search_pagination(client, world, make_referral):
    make_referral(patient_identifier="AAA-1", human_assigned_urgency="routine")
    make_referral(patient_identifier="BBB-2", human_assigned_urgency="emergency")
    make_referral(patient_identifier="BBB-3", human_assigned_urgency="emergency")
    h = world.h["hw1"]
    assert client.get("/api/referrals/", headers=h).json()["total"] == 3
    assert client.get("/api/referrals/?urgency=emergency", headers=h).json()["total"] == 2
    assert client.get("/api/referrals/?q=bbb", headers=h).json()["total"] == 2
    page = client.get("/api/referrals/?page=2&page_size=2", headers=h).json()
    assert len(page["items"]) == 1 and page["pages"] == 2 and page["total"] == 3
    assert client.get("/api/referrals/?page_size=1000", headers=h).status_code == 422
    ref = client.get("/api/referrals/", headers=h).json()["items"][0]["referral_reference"]
    assert client.get(f"/api/referrals/?reference={ref}", headers=h).json()["total"] == 1


def test_status_changes_and_history(client, world, make_referral):
    ref = make_referral()
    h = world.h["hw1"]
    r = client.patch(f"/api/referrals/{ref['id']}/status", json={"status": "follow_up_in_progress", "reason": "calling"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "follow_up_in_progress"
    r = client.patch(f"/api/referrals/{ref['id']}/status", json={"status": "cancelled", "reason": "moved away"}, headers=h)
    assert r.json()["status"] == "cancelled"
    # terminal: cannot reopen
    r = client.patch(f"/api/referrals/{ref['id']}/status", json={"status": "follow_up_in_progress"}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_status_transition"
    hist = client.get(f"/api/referrals/{ref['id']}/history", headers=h).json()["status_history"]
    assert [x["to_status"] for x in hist] == ["referred", "follow_up_in_progress", "cancelled"]
    assert hist[1]["changed_by"] == world.hw1.id and hist[1]["reason"] == "calling"


def test_escalation_statuses_cannot_be_set_directly(client, world, make_referral):
    ref = make_referral()
    for st in ("escalated", "escalation_pending"):
        r = client.patch(f"/api/referrals/{ref['id']}/status", json={"status": st}, headers=world.h["admin"])
        assert r.status_code == 403


def test_unauthorized_referral_access(client, world, make_referral):
    ref = make_referral()
    url = f"/api/referrals/{ref['id']}"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=world.h["hw2"]).status_code == 404  # other health worker
    assert client.get(url, headers=world.h["fs_c"]).status_code == 404  # other facility
    assert client.get(url, headers=world.h["fs_b"]).status_code == 200
    assert client.get(url, headers=world.h["admin"]).status_code == 200
    assert client.patch(url, json={"age": 40}, headers=world.h["hw2"]).status_code == 404
    assert client.patch(url + "/status", json={"status": "cancelled"}, headers=world.h["hw2"]).status_code == 404
    assert client.get(url + "/history", headers=world.h["fs_c"]).status_code == 404
    assert client.get("/api/referrals/", headers=world.h["hw2"]).json()["total"] == 0
    assert client.get("/api/referrals/", headers=world.h["fs_c"]).json()["total"] == 0
    assert client.get("/api/referrals/", headers=world.h["fs_b"]).json()["total"] == 1


def test_patient_name_access_control(client, world, make_referral):
    ref = make_referral(patient_name="Synthetic Testname")
    url = f"/api/referrals/{ref['id']}"
    assert client.get(url, headers=world.h["hw1"]).json()["patient_name"] == "Synthetic Testname"
    assert client.get(url, headers=world.h["admin"]).json()["patient_name"] == "Synthetic Testname"
    fs = client.get(url, headers=world.h["fs_b"]).json()
    assert fs["patient_name"] is None and fs["barrier_type"] is None  # minimum necessary for facility staff
    assert "Synthetic Testname" not in client.get("/api/referrals/", headers=world.h["fs_b"]).text


def test_admin_can_create_referral_for_a_worker(client, world):
    body = referral_payload(world, referring_facility_id=world.phc_a.id, assigned_health_worker_id=world.hw1.id)
    r = client.post("/api/referrals/", json=body, headers=world.h["admin"])
    assert r.status_code == 201 and r.json()["assigned_health_worker_id"] == world.hw1.id
    r = client.post("/api/referrals/", json=referral_payload(world), headers=world.h["admin"])
    assert r.status_code == 400  # missing referring facility
