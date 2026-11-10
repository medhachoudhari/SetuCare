import os

os.environ.setdefault("DATABASE_URL", "sqlite://")  # in-memory; never touches real data
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["BCRYPT_ROUNDS"] = "4"  # fast hashing for tests only

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db, make_engine
from app.integrations.ai_agents import AIAgentService, MockAIAgentProvider, set_ai_service
from app.main import app
from app.models import Facility, User
from app.models.enums import FacilityType, UserRole
from app.security import authentication as auth

PASSWORD = "StrongPass123"


@pytest.fixture(autouse=True)
def _reset_state():
    auth.clear_login_failures()
    set_ai_service(AIAgentService(MockAIAgentProvider()))
    yield
    set_ai_service(None)


@pytest.fixture()
def engine():
    eng = make_engine("sqlite://")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def SessionTest(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def db(SessionTest):
    with SessionTest() as session:
        yield session


@pytest.fixture()
def client(SessionTest):
    def _get_db():
        with SessionTest() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _facility(db, name, ftype=FacilityType.PHC):
    f = Facility(facility_name=name, facility_type=ftype, location="Test District")
    db.add(f)
    db.commit()
    return f


def _user(db, email, role, facility=None, active=True):
    u = User(full_name=email.split("@")[0].title(), email=email, hashed_password=auth.hash_password(PASSWORD),
             role=role, facility_id=facility.id if facility else None, is_active=active)
    db.add(u)
    db.commit()
    return u


def _headers(user):
    token, _ = auth.create_access_token(user)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def world(db):
    """Synthetic data only. phc_a refers to hospital_b / hospital_c."""
    w = type("World", (), {})()
    w.phc_a = _facility(db, "Test PHC A")
    w.hospital_b = _facility(db, "Test Hospital B", FacilityType.DISTRICT_HOSPITAL)
    w.hospital_c = _facility(db, "Test Hospital C", FacilityType.DISTRICT_HOSPITAL)
    w.hw1 = _user(db, "hw1@example.com", UserRole.HEALTH_WORKER, w.phc_a)
    w.hw2 = _user(db, "hw2@example.com", UserRole.HEALTH_WORKER, w.phc_a)
    w.fs_b = _user(db, "fs_b@example.com", UserRole.FACILITY_STAFF, w.hospital_b)
    w.fs_c = _user(db, "fs_c@example.com", UserRole.FACILITY_STAFF, w.hospital_c)
    w.admin = _user(db, "admin@example.com", UserRole.ADMINISTRATOR)
    w.h = {k: _headers(getattr(w, k)) for k in ("hw1", "hw2", "fs_b", "fs_c", "admin")}
    return w


def referral_payload(world, **overrides):
    data = {"patient_identifier": "PT-0001", "age": 34, "receiving_facility_id": world.hospital_b.id,
            "human_assigned_urgency": "urgent",
            "referral_note": "Needs specialist review. Family cannot afford the fee and has no transport."}
    data.update(overrides)
    return data


@pytest.fixture()
def make_referral(client, world):
    def _make(user="hw1", **overrides):
        r = client.post("/api/referrals/", json=referral_payload(world, **overrides), headers=world.h[user])
        assert r.status_code == 201, r.text
        return r.json()

    return _make
