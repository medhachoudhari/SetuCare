import pytest

from app.models import AuditLog, Referral
from app.models.enums import ReferralStatus
from app.services import referral_service
from app.services.errors import ServiceError


def test_facility_staff_find_by_reference_and_confirm_arrival(client, world, make_referral):
    ref = make_referral()
    h = world.h["fs_b"]
    found = client.get(f"/api/referrals/?reference={ref['referral_reference']}", headers=h).json()
    assert found["total"] == 1 and found["items"][0]["id"] == ref["id"]
    assert client.get(f"/api/referrals/?reference={ref['referral_reference']}", headers=world.h["fs_c"]).json()["total"] == 0
    r = client.post(f"/api/referrals/{ref['id']}/confirm-arrival", json={"notes": "Arrived at OPD"}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "confirmed" and body["confirmed_at"]


def test_confirmation_restricted_to_receiving_facility_staff(client, world, make_referral):
    ref = make_referral()
    url = f"/api/referrals/{ref['id']}/confirm-arrival"
    assert client.post(url, headers=world.h["fs_c"]).status_code == 404  # other facility
    assert client.post(url, headers=world.h["hw1"]).status_code == 403  # health worker cannot confirm
    assert client.post(url).status_code == 401
    assert client.get(f"/api/referrals/{ref['id']}", headers=world.h["hw1"]).json()["status"] == "referred"


def test_arrival_time_cannot_be_future_or_naive(client, world, make_referral):
    ref = make_referral()
    url = f"/api/referrals/{ref['id']}/confirm-arrival"
    assert client.post(url, json={"arrival_time": "2999-01-01T00:00:00+00:00"}, headers=world.h["fs_b"]).status_code == 400
    assert client.post(url, json={"arrival_time": "2020-01-01T00:00:00"}, headers=world.h["fs_b"]).status_code == 400


def test_confirm_twice_is_rejected(client, world, make_referral):
    ref = make_referral()
    url = f"/api/referrals/{ref['id']}/confirm-arrival"
    assert client.post(url, headers=world.h["fs_b"]).status_code == 200
    assert client.post(url, headers=world.h["fs_b"]).status_code == 409


def test_complete_requires_confirmation_and_outcome(client, world, make_referral):
    ref = make_referral()
    url = f"/api/referrals/{ref['id']}/status"
    h = world.h["fs_b"]
    assert client.patch(url, json={"status": "completed", "outcome": "admitted"}, headers=h).status_code == 409  # not confirmed yet
    client.post(f"/api/referrals/{ref['id']}/confirm-arrival", headers=h)
    assert client.patch(url, json={"status": "completed"}, headers=h).status_code == 400  # outcome required
    r = client.patch(url, json={"status": "completed", "outcome": "treated_and_discharged", "outcome_notes": "ok"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "completed" and r.json()["outcome"] == "treated_and_discharged"
    assert client.patch(url, json={"status": "cancelled"}, headers=world.h["hw1"]).status_code == 409


def test_unauthorized_closure_prevented(client, world, make_referral):
    ref = make_referral()
    url = f"/api/referrals/{ref['id']}/status"
    for target, payload in (("confirmed", {}), ("completed", {"outcome": "admitted"})):
        r = client.patch(url, json={"status": target, **payload}, headers=world.h["hw1"])
        assert r.status_code == 403, target
    assert client.patch(url, json={"status": "cancelled"}, headers=world.h["fs_b"]).status_code == 403
    assert client.patch(url, json={"status": "lost_to_follow_up"}, headers=world.h["fs_b"]).status_code == 403
    assert client.get(f"/api/referrals/{ref['id']}", headers=world.h["hw1"]).json()["status"] == "referred"


def test_ai_or_system_actor_can_never_confirm_or_complete(db, world, make_referral):
    ref = make_referral()
    referral = db.get(Referral, ref["id"])
    for target in (ReferralStatus.CONFIRMED, ReferralStatus.COMPLETED):
        with pytest.raises(ServiceError) as exc:
            referral_service.transition(db, referral, target, actor=None, reason="AI says close it")
        assert exc.value.status_code == 403
    assert referral.status == ReferralStatus.REFERRED


def test_ai_barrier_analysis_cannot_change_status_or_urgency(client, world, make_referral):
    ref = make_referral(human_assigned_urgency="emergency")
    r = client.post(f"/api/referrals/{ref['id']}/analyze-barrier", headers=world.h["hw1"])
    assert r.status_code == 200
    assert r.json()["status"] == "referred" and r.json()["human_assigned_urgency"] == "emergency"
    assert client.post(f"/api/referrals/{ref['id']}/analyze-barrier", headers=world.h["fs_b"]).status_code == 403


def test_audit_log_created_for_key_actions(client, world, make_referral, db):
    ref = make_referral(patient_name="Synthetic Testname")
    client.post(f"/api/followups/", json={"referral_id": ref["id"]}, headers=world.h["hw1"])
    esc = client.post("/api/escalations/", json={"referral_id": ref["id"]}, headers=world.h["hw1"]).json()
    client.post(f"/api/escalations/{esc['id']}/approve", headers=world.h["admin"])
    client.post(f"/api/referrals/{ref['id']}/confirm-arrival", headers=world.h["fs_b"])
    actions = {a.action for a in db.query(AuditLog).filter_by(referral_id=ref["id"])}
    assert {"referral.create", "ai.barrier_interpretation", "ai.contact_strategy", "followup.create",
            "ai.escalation_synthesis", "escalation.create", "escalation.approve",
            "referral.arrival_confirmed"} <= actions
    approve = db.query(AuditLog).filter_by(action="escalation.approve").one()
    assert approve.user_id == world.admin.id and approve.timestamp.tzinfo is not None
    # no patient name or free-text note in any audit snapshot
    dump = " ".join(str(a.input_snapshot) + str(a.output_snapshot) for a in db.query(AuditLog))
    assert "Synthetic Testname" not in dump


def test_audit_log_endpoint_admin_only(client, world, make_referral):
    ref = make_referral()
    assert client.get("/api/audit-logs/", headers=world.h["hw1"]).status_code == 403
    r = client.get(f"/api/audit-logs/?referral_id={ref['id']}", headers=world.h["admin"])
    assert r.status_code == 200 and r.json()["total"] >= 1


def test_auth_events_audited(client, world, db):
    client.post("/api/auth/login", json={"email": "hw1@example.com", "password": "StrongPass123"})
    client.post("/api/auth/login", json={"email": "hw1@example.com", "password": "WrongPass123"})
    actions = [a.action for a in db.query(AuditLog)]
    assert "auth.login" in actions and "auth.login_failed" in actions
