from datetime import timedelta

from app.database import utcnow
from app.models import Referral


def test_dashboard_aggregates(client, world, make_referral, db):
    a = make_referral(patient_identifier="A", human_assigned_urgency="emergency")
    b = make_referral(patient_identifier="B", human_assigned_urgency="routine")
    c = make_referral(patient_identifier="C", human_assigned_urgency="urgent")
    d = make_referral(patient_identifier="D", human_assigned_urgency="routine")
    client.post(f"/api/referrals/{a['id']}/confirm-arrival", headers=world.h["fs_b"])
    client.patch(f"/api/referrals/{d['id']}/status", json={"status": "cancelled"}, headers=world.h["hw1"])
    r = db.get(Referral, b["id"])
    r.deadline = utcnow() - timedelta(hours=3)
    db.commit()
    client.post("/api/escalations/", json={"referral_id": c["id"]}, headers=world.h["hw1"])
    fu = client.post("/api/followups/", json={"referral_id": b["id"]}, headers=world.h["hw1"]).json()
    client.patch(f"/api/followups/{fu['id']}", json={"contact_outcome": "reached_needs_support"}, headers=world.h["hw1"])

    s = client.get("/api/dashboard/summary", headers=world.h["hw1"]).json()
    assert s["total_referrals"] == 4 and s["confirmed_referrals"] == 1 and s["pending_referrals"] == 2
    assert s["overdue_referrals"] == 1 and s["pending_escalations"] == 1
    assert s["referral_confirmation_rate"] == round(1 / 3, 4)  # cancelled excluded from denominator
    assert s["followups"]["attempted"] == 1 and s["followups"]["successful_contacts"] == 1
    assert s["followups"]["pending_upcoming"] == 1 and s["followups"]["total"] == 2

    st = client.get("/api/dashboard/referral-statistics", headers=world.h["hw1"]).json()
    assert st["by_status"]["confirmed"] == 1 and st["by_status"]["cancelled"] == 1
    assert st["by_urgency"]["emergency"] == 1 and st["by_urgency"]["semi_urgent"] == 0
    assert st["average_hours_to_confirmation"] is not None

    urgent = client.get("/api/dashboard/urgent-referrals", headers=world.h["hw1"]).json()
    assert [u["id"] for u in urgent] == [c["id"]]  # a is confirmed so no longer open
    pending = client.get("/api/dashboard/pending-confirmations", headers=world.h["hw1"]).json()
    assert {p["id"] for p in pending} == {b["id"], c["id"]} and pending[0]["id"] == b["id"]  # most overdue first
    assert pending[0]["is_overdue"] is True


def test_dashboard_is_scoped_to_caller(client, world, make_referral):
    make_referral()
    assert client.get("/api/dashboard/summary", headers=world.h["hw2"]).json()["total_referrals"] == 0
    assert client.get("/api/dashboard/summary", headers=world.h["fs_c"]).json()["total_referrals"] == 0
    assert client.get("/api/dashboard/summary", headers=world.h["fs_b"]).json()["total_referrals"] == 1
    assert client.get("/api/dashboard/summary", headers=world.h["admin"]).json()["total_referrals"] == 1
    assert client.get("/api/dashboard/pending-confirmations", headers=world.h["fs_c"]).json() == []
    assert client.get("/api/dashboard/summary").status_code == 401


def test_empty_dashboard(client, world):
    s = client.get("/api/dashboard/summary", headers=world.h["admin"]).json()
    assert s["total_referrals"] == 0 and s["referral_confirmation_rate"] == 0.0
