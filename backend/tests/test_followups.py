from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.database import utcnow
from app.models.enums import UrgencyLevel
from app.models.referral import Referral
from app.services import scheduling_service


def _create(client, world, ref, **kw):
    return client.post("/api/followups/", json={"referral_id": ref["id"], **kw}, headers=world.h["hw1"])


def test_followup_created_with_ai_strategy_and_schedule_cap(client, world, make_referral):
    ref = make_referral(human_assigned_urgency="urgent")
    r = _create(client, world, ref)
    assert r.status_code == 201
    fu = r.json()
    assert fu["attempt_number"] == 1 and fu["message_template_id"] in fu["ai_recommendation"]["message_template_id"]
    assert fu["message_preview"]  # approved template text only
    scheduled = datetime.fromisoformat(fu["scheduled_at"])
    assert scheduled <= utcnow() + timedelta(hours=get_settings().followup_interval_hours_urgent, seconds=5)
    # referral moved to follow_up_in_progress
    assert client.get(f"/api/referrals/{ref['id']}", headers=world.h["hw1"]).json()["status"] == "follow_up_in_progress"


def test_only_one_pending_followup_and_role_restrictions(client, world, make_referral):
    ref = make_referral()
    assert _create(client, world, ref).status_code == 201
    assert _create(client, world, ref).status_code == 409
    assert client.post("/api/followups/", json={"referral_id": ref["id"]}, headers=world.h["fs_b"]).status_code == 403
    assert client.post("/api/followups/", json={"referral_id": ref["id"]}, headers=world.h["hw2"]).status_code == 404


def test_next_attempt_never_later_than_policy_and_ai_can_shorten():
    now = utcnow()
    cap = get_settings().followup_interval_hours_urgent
    assert scheduling_service.next_attempt_time(UrgencyLevel.URGENT, now, ai_delay_hours=100) == now + timedelta(hours=cap)
    assert scheduling_service.next_attempt_time(UrgencyLevel.URGENT, now, ai_delay_hours=1) == now + timedelta(hours=1)
    near = now + timedelta(minutes=10)
    assert scheduling_service.next_attempt_time(UrgencyLevel.URGENT, now, None, near) == near


def test_record_outcome_schedules_next_attempt(client, world, make_referral):
    ref = make_referral()
    fu = _create(client, world, ref).json()
    r = client.patch(f"/api/followups/{fu['id']}", headers=world.h["hw1"],
                     json={"contact_outcome": "no_answer", "conversation_summary": "Phone rang out"})
    assert r.status_code == 200
    body = r.json()
    assert body["followup"]["attempted_at"] and body["followup"]["next_recommended_action"] == "schedule_next_attempt"
    nxt = body["next_followup"]
    assert nxt["attempt_number"] == 2 and nxt["channel"] != fu["channel"]  # strategy rotated channel
    assert nxt["message_template_id"] == "TPL_REQUEST_CALLBACK"
    listing = client.get(f"/api/followups/referral/{ref['id']}", headers=world.h["hw1"]).json()
    assert [f["attempt_number"] for f in listing] == [1, 2]


def test_will_attend_outcome_does_not_close_referral(client, world, make_referral):
    ref = make_referral()
    fu = _create(client, world, ref).json()
    body = client.patch(f"/api/followups/{fu['id']}", headers=world.h["hw1"],
                        json={"contact_outcome": "reached_will_attend"}).json()
    assert body["followup"]["next_recommended_action"] == "await_facility_confirmation"
    assert body["next_followup"] is None
    assert client.get(f"/api/referrals/{ref['id']}", headers=world.h["hw1"]).json()["status"] == "follow_up_in_progress"


def test_attempts_exhausted_recommends_escalation(client, world, make_referral):
    ref = make_referral()
    fu = _create(client, world, ref).json()
    for _ in range(get_settings().max_followup_attempts):
        body = client.patch(f"/api/followups/{fu['id']}", headers=world.h["hw1"], json={"contact_outcome": "no_answer"}).json()
        if body["next_followup"] is None:
            break
        fu = body["next_followup"]
    assert body["followup"]["next_recommended_action"] == "recommend_escalation"
    # recommendation only: no escalation row exists and status is not escalated
    assert client.get("/api/escalations/pending", headers=world.h["hw1"]).json() == []


def test_completed_attempt_cannot_be_rescheduled_or_misrecorded(client, world, make_referral):
    ref = make_referral()
    fu = _create(client, world, ref).json()
    h = world.h["hw1"]
    assert client.patch(f"/api/followups/{fu['id']}", headers=h, json={"contact_outcome": "pending"}).status_code == 400
    future = (utcnow() + timedelta(days=1)).isoformat()
    assert client.patch(f"/api/followups/{fu['id']}", headers=h, json={"attempted_at": future, "contact_outcome": "no_answer"}).status_code == 400
    client.patch(f"/api/followups/{fu['id']}", headers=h, json={"contact_outcome": "message_delivered"})
    assert client.patch(f"/api/followups/{fu['id']}", headers=h, json={"scheduled_at": future}).status_code == 409


def test_pending_followups_and_overdue_detection(client, world, make_referral, db):
    ref = make_referral()
    fu = _create(client, world, ref, scheduled_at=(utcnow() - timedelta(hours=1)).isoformat()).json()
    ref2 = make_referral(patient_identifier="PT-2")
    _create(client, world, ref2, scheduled_at=(utcnow() + timedelta(hours=5)).isoformat())
    pending = client.get("/api/followups/pending", headers=world.h["hw1"]).json()
    assert [f["id"] for f in pending] == [fu["id"]]
    assert len(client.get("/api/followups/pending?include_upcoming=true", headers=world.h["hw1"]).json()) == 2
    assert client.get("/api/followups/pending", headers=world.h["hw2"]).json() == []
    assert client.get("/api/followups/pending", headers=world.h["fs_b"]).status_code == 403
    # overdue detection
    assert scheduling_service.find_overdue_referrals(db) == []
    r = db.get(Referral, ref["id"])
    r.deadline = utcnow() - timedelta(hours=1)
    db.commit()
    assert [x.id for x in scheduling_service.find_overdue_referrals(db)] == [ref["id"]]
    assert client.get("/api/referrals/?overdue=true", headers=world.h["hw1"]).json()["total"] == 1
    assert client.get("/api/referrals/?overdue=true", headers=world.h["hw1"]).json()["items"][0]["is_overdue"] is True


def test_naive_datetimes_rejected(client, world, make_referral):
    ref = make_referral()
    r = _create(client, world, ref, scheduled_at="2030-01-01T10:00:00")
    assert r.status_code == 400
