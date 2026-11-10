import time

import jwt

from app.config import get_settings
from tests.conftest import PASSWORD


def _register(client, **kw):
    body = {"full_name": "New Person", "email": "new@example.com", "password": PASSWORD}
    body.update(kw)
    return client.post("/api/auth/register", json=body)


def test_register_creates_unassigned_health_worker(client):
    r = _register(client)
    assert r.status_code == 201
    body = r.json()
    assert body["role"] == "health_worker" and body["facility_id"] is None
    assert "password" not in body and "hashed_password" not in body


def test_register_cannot_self_assign_privileged_role(client, world):
    assert _register(client, role="administrator").status_code == 403
    assert _register(client, role="facility_staff", facility_id=world.hospital_b.id).status_code == 403
    assert _register(client, facility_id=world.phc_a.id).status_code == 403


def test_admin_can_register_with_role_and_facility(client, world):
    r = _register(client, role="facility_staff", facility_id=world.hospital_b.id, email="staff@example.com")
    assert r.status_code == 403  # anonymous
    r = client.post("/api/auth/register", headers=world.h["admin"],
                    json={"full_name": "Staff Member", "email": "staff@example.com", "password": PASSWORD,
                          "role": "facility_staff", "facility_id": world.hospital_b.id})
    assert r.status_code == 201 and r.json()["role"] == "facility_staff"


def test_register_duplicate_email_and_weak_password(client):
    assert _register(client).status_code == 201
    r = _register(client, email="NEW@example.com")
    assert r.status_code == 409 and r.json()["error"]["code"] == "email_taken"
    r = _register(client, email="x@example.com", password="alllettersnodigits")
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_login_success_and_me(client):
    _register(client)
    r = client.post("/api/auth/login", json={"email": "new@example.com", "password": PASSWORD})
    assert r.status_code == 200
    token = r.json()["access_token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and me.json()["email"] == "new@example.com"


def test_login_wrong_password_and_unknown_user(client):
    _register(client)
    for email, pw in (("new@example.com", "WrongPass123"), ("ghost@example.com", PASSWORD)):
        r = client.post("/api/auth/login", json={"email": email, "password": pw})
        assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"


def test_login_throttling(client):
    _register(client)
    for _ in range(get_settings().login_max_attempts):
        client.post("/api/auth/login", json={"email": "new@example.com", "password": "WrongPass123"})
    r = client.post("/api/auth/login", json={"email": "new@example.com", "password": PASSWORD})
    assert r.status_code == 429


def test_jwt_missing_malformed_tampered_expired(client, world):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401
    s = get_settings()
    forged = jwt.encode({"sub": str(world.admin.id), "exp": int(time.time()) + 600}, "wrong-key" * 5, algorithm="HS256")
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    expired = jwt.encode({"sub": str(world.hw1.id), "exp": int(time.time()) - 10}, s.secret_key, algorithm=s.jwt_algorithm)
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    none_alg = jwt.encode({"sub": str(world.admin.id), "exp": int(time.time()) + 600}, None, algorithm="none")
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {none_alg}"}).status_code == 401


def test_token_role_claim_is_not_trusted(client, world):
    s = get_settings()
    tok = jwt.encode({"sub": str(world.hw1.id), "role": "administrator", "exp": int(time.time()) + 600},
                     s.secret_key, algorithm=s.jwt_algorithm)
    r = client.get("/api/audit-logs/", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


def test_inactive_user_token_rejected(client, world, db):
    world.hw1.is_active = False
    db.add(world.hw1)
    db.commit()
    assert client.get("/api/auth/me", headers=world.h["hw1"]).status_code == 401


def test_role_permissions_admin_endpoints(client, world):
    assert client.get("/api/auth/users", headers=world.h["hw1"]).status_code == 403
    assert client.get("/api/auth/users", headers=world.h["admin"]).status_code == 200
    body = {"facility_name": "New Clinic", "facility_type": "primary_health_centre", "location": "Test Town"}
    assert client.post("/api/facilities/", json=body, headers=world.h["hw1"]).status_code == 403
    assert client.post("/api/facilities/", json=body, headers=world.h["admin"]).status_code == 201
    assert client.get("/api/facilities/").status_code == 401


def test_admin_assigns_facility_to_self_registered_user(client, world):
    uid = _register(client).json()["id"]
    r = client.patch(f"/api/auth/users/{uid}", json={"facility_id": world.phc_a.id}, headers=world.h["admin"])
    assert r.status_code == 200 and r.json()["facility_id"] == world.phc_a.id
    # unknown fields (e.g. client-supplied is_admin) are rejected
    r = client.patch(f"/api/auth/users/{uid}", json={"is_superuser": True}, headers=world.h["admin"])
    assert r.status_code == 422
