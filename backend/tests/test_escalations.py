from datetime import timedelta

from app.database import utcnow
from app.integrations.ai_agents import AIAgentService, set_ai_service
from app.models import AuditLog, Escalation, Referral
from app.models.enums import EscalationStatus
from app.services import scheduling_service
from tests.test_ai_agents import ScriptedProvider


def _escalate(client, world, ref, user="hw1"):
    return client.post("/api/escalations/", json={"referral_id": ref["id"]}, headers=world.h[user])


def test_create_escalation_is_pending_and_human_gated(client, world, make_referral):
    ref = make_referral()
    r = _escalate(client, world, ref)
    assert r.status_code == 201
    esc = r.json()
    assert esc["status"] == "pending_review" and esc["reviewed_by"] is None and esc["reviewed_at"] is None
    assert esc["ai_generated_brief"] and esc["ai_recommendation"]["recommended_action"]
    assert client.get(f"/api/referrals/{ref['id']}", headers=world.h["hw1"]).json()["status"] == "escalation_pending"
    assert _escalate(client, world, ref).status_code == 409  # already pending
    pending = client.get("/api/escalations/pending", headers=world.h["hw1"]).json()
    assert [e["id"] for e in pending] == [esc["id"]]
    assert client.get("/api/escalations/pending", headers=world.h["hw2"]).json() == []
    assert client.get("/api/escalations/pending", headers=world.h["fs_b"]).status_code == 403


def test_client_cannot_pre_approve_or_set_fields(client, world, make_referral):
    ref = make_referral()
    r = client.post("/api/escalations/", json={"referral_id": ref["id"], "status": "approved", "reviewed_by": 1},
                    headers=world.h["hw1"])
    assert r.status_code == 422


def test_approve_escalation_records_reviewer_and_preserves_ai_recommendation(client, world, make_referral):
    ref = make_referral()
    esc = _escalate(client, world, ref).json()
    r = client.post(f"/api/escalations/{esc['id']}/approve", json={"reviewer_notes": "Agree"}, headers=world.h["admin"])
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "approved" and body["reviewed_by"] == world.admin.id and body["reviewed_at"]
    assert body["ai_recommendation"] == esc["ai_recommendation"] and body["ai_generated_brief"] == esc["ai_generated_brief"]
    assert client.get(f"/api/referrals/{ref['id']}", headers=world.h["hw1"]).json()["status"] == "escalated"
    assert client.post(f"/api/escalations/{esc['id']}/approve", headers=world.h["admin"]).status_code == 409
    assert client.post(f"/api/escalations/{esc['id']}/reject", json={"reviewer_notes": "changed mind"},
                       headers=world.h["admin"]).status_code == 409


def test_reject_escalation_restores_status_and_requires_reason(client, world, make_referral):
    ref = make_referral()
    client.post("/api/followups/", json={"referral_id": ref["id"]}, headers=world.h["hw1"])  # -> follow_up_in_progress
    esc = _escalate(client, world, ref).json()
    assert client.post(f"/api/escalations/{esc['id']}/reject", json={}, headers=world.h["hw1"]).status_code == 422
    r = client.post(f"/api/escalations/{esc['id']}/reject", json={"reviewer_notes": "Not needed yet"}, headers=world.h["hw1"])
    assert r.status_code == 200 and r.json()["status"] == "rejected" and r.json()["reviewed_by"] == world.hw1.id
    assert r.json()["ai_recommendation"] == esc["ai_recommendation"]
    assert client.get(f"/api/referrals/{ref['id']}", headers=world.h["hw1"]).json()["status"] == "follow_up_in_progress"


def test_unauthorized_users_cannot_review_escalation(client, world, make_referral):
    ref = make_referral()
    esc = _escalate(client, world, ref).json()
    for who in ("fs_b", "fs_c", "hw2"):
        assert client.post(f"/api/escalations/{esc['id']}/approve", headers=world.h[who]).status_code in (403, 404)
        assert client.post(f"/api/escalations/{esc['id']}/reject", json={"reviewer_notes": "not allowed"},
                           headers=world.h[who]).status_code in (403, 404)
    assert client.post(f"/api/escalations/{esc['id']}/approve").status_code == 401
    assert client.get(f"/api/escalations/{esc['id']}", headers=world.h["hw2"]).status_code == 404
    assert client.get(f"/api/escalations/{esc['id']}", headers=world.h["fs_b"]).status_code == 403
    still = client.get(f"/api/escalations/{esc['id']}", headers=world.h["hw1"]).json()
    assert still["status"] == "pending_review" and still["reviewed_by"] is None


def test_ai_cannot_downgrade_human_urgency_in_escalation(client, world, make_referral, db):
    bad = {"case_summary": "s", "escalation_reason": "r", "recommended_action": "supervisor_review",
           "urgency_recommendation": "routine", "supporting_evidence": [], "uncertainty_flag": False}
    set_ai_service(AIAgentService(ScriptedProvider(escalation=bad)))
    ref = make_referral(human_assigned_urgency="emergency")
    esc = _escalate(client, world, ref).json()
    assert esc["urgency_level"] == "emergency"
    assert esc["ai_recommendation"]["urgency_recommendation"] == "routine"  # original preserved
    assert client.get(f"/api/referrals/{ref['id']}", headers=world.h["hw1"]).json()["human_assigned_urgency"] == "emergency"
    assert db.query(AuditLog).filter_by(action="safety.urgency_downgrade_blocked", referral_id=ref["id"]).count() == 1


def test_ai_may_raise_urgency_in_escalation_but_referral_urgency_unchanged(client, world, make_referral):
    up = {"case_summary": "s", "escalation_reason": "r", "recommended_action": "supervisor_review",
          "urgency_recommendation": "emergency", "supporting_evidence": [], "uncertainty_flag": False}
    set_ai_service(AIAgentService(ScriptedProvider(escalation=up)))
    ref = make_referral(human_assigned_urgency="routine")
    assert _escalate(client, world, ref).json()["urgency_level"] == "emergency"
    assert client.get(f"/api/referrals/{ref['id']}", headers=world.h["hw1"]).json()["human_assigned_urgency"] == "routine"


def test_escalation_survives_ai_failure_with_flagged_fallback(client, world, make_referral):
    set_ai_service(AIAgentService(ScriptedProvider(escalation={"nonsense": True})))
    ref = make_referral()
    esc = _escalate(client, world, ref).json()
    assert esc["status"] == "pending_review" and esc["uncertainty_flag"] is True
    assert esc["ai_recommendation"]["fallback"] is True


def test_scheduler_sweep_creates_only_pending_recommendations(client, world, make_referral, db):
    ref = make_referral()
    r = db.get(Referral, ref["id"])
    r.deadline = utcnow() - timedelta(hours=2)
    db.commit()
    assert scheduling_service.is_escalation_mandatory(r)
    assert scheduling_service.run_escalation_sweep(db) == 1
    esc = db.query(Escalation).one()
    assert esc.status == EscalationStatus.PENDING_REVIEW and esc.created_by is None and esc.reviewed_by is None
    db.refresh(r)
    assert r.status.value == "escalation_pending"
    assert scheduling_service.run_escalation_sweep(db) == 0  # idempotent


def test_closing_referral_supersedes_pending_escalation(client, world, make_referral):
    ref = make_referral()
    esc = _escalate(client, world, ref).json()
    assert client.post(f"/api/referrals/{ref['id']}/confirm-arrival", headers=world.h["fs_b"]).status_code == 200
    assert client.get(f"/api/escalations/{esc['id']}", headers=world.h["hw1"]).json()["status"] == "superseded"
    assert client.post(f"/api/escalations/{esc['id']}/approve", headers=world.h["admin"]).status_code == 409
