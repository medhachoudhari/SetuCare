# SetuCare Backend

FastAPI backend for **SetuCare**, a reasoning-driven healthcare referral closure agent.
Deterministic workflows + three pluggable AI agents + mandatory human approval + full audit trail.

> **Principle:** AI recommends, humans decide. AI output can never lower urgency, close a referral,
> approve an escalation, dispatch a visit, or send free-text patient messages.

## 1. Installation

Requires Python 3.13.

```bash
cd backend
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit; never commit .env
```

## 2. Configuration & database

All settings come from environment variables / `.env` (see `.env.example`).

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | `sqlite:///./setucare_dev.db` (dev fallback) or `postgresql+psycopg2://user:pass@host:5432/setucare` |
| `SECRET_KEY` | JWT signing key. **Required (>=32 chars) when `ENVIRONMENT=production`.** |
| `AUTO_CREATE_TABLES` | `true` creates tables on startup (dev). Use Alembic with PostgreSQL. |
| `BOOTSTRAP_ADMIN_EMAIL` / `_PASSWORD` | Seeds the first administrator at startup (remove afterwards). |
| `ALLOW_PUBLIC_REGISTRATION` | Disable to force admin-created accounts only. |
| `DEADLINE_HOURS_*`, `FOLLOWUP_INTERVAL_HOURS_*`, `MAX_FOLLOWUP_ATTEMPTS` | Deadline / follow-up policy (section 6). |
| `ENABLE_SCHEDULER`, `SCHEDULER_INTERVAL_MINUTES` | APScheduler sweep that *recommends* escalations for overdue referrals. |
| `AI_AGENT_PROVIDER`, `AI_CONFIDENCE_THRESHOLD` | `mock` or `package.module:Class` (section 5). |

### Migrations (Alembic)
```bash
alembic upgrade head                                   # apply
alembic revision --autogenerate -m "describe change"   # after changing models
alembic downgrade -1                                   # roll back one
```
PostgreSQL example: `createdb setucare` then set `DATABASE_URL` and run `alembic upgrade head`
(and set `AUTO_CREATE_TABLES=false`).

## 3. Run & test

```bash
uvicorn app.main:app --reload --port 8000      # Swagger UI: http://localhost:8000/docs  (ReDoc: /redoc)
python -m pytest -q                            # tests use in-memory SQLite and synthetic data only
```

First-time bootstrap: set `BOOTSTRAP_ADMIN_*`, start the server, log in as the admin, create facilities
(`POST /api/facilities/`), then create staff via `POST /api/auth/register` (admin token) or let users
self-register and assign them with `PATCH /api/auth/users/{id}`.

## 4. API reference

All paths are prefixed `/api`. Auth: `Authorization: Bearer <JWT>` (except register/login/health).
Pagination: `?page=1&page_size=20` -> `{"items":[...],"total":N,"page":1,"page_size":20,"pages":M}`.
Timestamps are ISO-8601 UTC (`2026-11-10T12:00:00Z`); datetimes you send **must** include a timezone.

### Error format (all errors)
```json
{"error": {"code": "forbidden", "message": "Your role is not permitted to perform this action", "details": null}}
```
Validation errors: HTTP 422, `code: "validation_error"`, `details: [{"field": "age", "message": "...", "type": "..."}]`.
Codes: `unauthorized`(401) `invalid_credentials`(401) `forbidden`(403) `not_found`(404)
`conflict`/`email_taken`/`invalid_status_transition`/`referral_closed`/`escalation_already_pending`/
`escalation_already_reviewed`/`followup_already_pending`(409) `validation_error`(422)
`too_many_attempts`(429) `internal_error`(500). Records outside your scope return **404**, not 403 (no existence leak).

### Auth
| Method & path | Auth | Notes |
|---|---|---|
| `POST /auth/register` | none / admin | Anonymous -> always an unassigned `health_worker`. `role`/`facility_id` honoured **only** for an admin caller (else 403). |
| `POST /auth/login` | none | JSON login -> token. |
| `POST /auth/token` | none | OAuth2 form login (Swagger "Authorize"). |
| `GET /auth/me` | any | Current user. |
| `GET /auth/users`, `PATCH /auth/users/{id}` | admin | List users; assign role/facility, activate/deactivate. |

```jsonc
// POST /api/auth/login
{"email": "hw@example.com", "password": "WorkerPass123"}
// 200
{"access_token": "<jwt>", "token_type": "bearer", "expires_in": 3600,
 "user": {"id": 2, "full_name": "Demo Worker", "email": "hw@example.com", "role": "health_worker",
          "facility_id": 1, "is_active": true, "created_at": "2026-11-10T12:00:00Z"}}
```

### Facilities
`GET /facilities/` (any user, `?q=`), `GET /facilities/{id}`, `POST /facilities/` (admin), `PATCH /facilities/{id}` (admin).
```json
{"facility_name": "District Hospital", "facility_type": "district_hospital", "location": "Demo", "contact_information": null}
```
`facility_type`: `primary_health_centre | community_health_centre | district_hospital | tertiary_hospital | other`.

### Referrals
| Method & path | Who | Notes |
|---|---|---|
| `POST /referrals/` | health_worker, admin | Referring facility + assigned worker derived from the token for health workers. Runs Agent 1. |
| `GET /referrals/` | scoped | Filters: `status, urgency, barrier_type, overdue, q, reference`; pagination. |
| `GET /referrals/{id}` | scoped | |
| `PATCH /referrals/{id}` | health_worker, admin | Edit open referral; re-grading urgency recomputes deadline; note change re-runs Agent 1. |
| `PATCH /referrals/{id}/status` | role-dependent | See status table. |
| `POST /referrals/{id}/confirm-arrival` | facility_staff (receiving facility), admin | Records `confirmed_at`. |
| `POST /referrals/{id}/analyze-barrier` | health_worker, admin | Re-run Agent 1. |
| `GET /referrals/{id}/history` | scoped | Status history. |

```jsonc
// POST /api/referrals/
{"patient_identifier": "PT-0001", "patient_name": null, "age": 34, "receiving_facility_id": 2,
 "human_assigned_urgency": "urgent",
 "referral_note": "Family cannot afford the fee and has no transport.",
 "communication_preferences": {"preferred_channel": "sms", "preferred_time_of_day": "morning",
                               "language": "en", "do_not_contact_channels": ["whatsapp"]}}
// 201
{"id": 1, "referral_reference": "SC-20261110-YFC3BG", "patient_identifier": "PT-0001", "patient_name": null,
 "age": 34, "referring_facility_id": 1, "receiving_facility_id": 2, "referring_facility_name": "Demo PHC",
 "receiving_facility_name": "Demo District Hospital", "assigned_health_worker_id": 2,
 "human_assigned_urgency": "urgent", "referral_note": "...", "communication_preferences": {"...": "..."},
 "barrier_type": "financial", "barrier_reasoning": "...", "barrier_confidence": 0.65, "barrier_needs_review": false,
 "status": "referred", "deadline": "2026-11-11T12:00:00Z", "is_overdue": false, "outcome": null,
 "outcome_notes": null, "created_at": "...", "updated_at": "...", "confirmed_at": null}
```
```jsonc
// PATCH /api/referrals/1/status         (facility completing a confirmed referral)
{"status": "completed", "outcome": "treated_and_discharged", "outcome_notes": "optional", "reason": "optional"}
// POST /api/referrals/1/confirm-arrival  (body optional)
{"arrival_time": "2026-11-10T10:30:00+00:00", "notes": "Arrived at OPD"}
```
Privacy: `patient_name` is returned only to the assigned health worker and administrators.
Facility staff additionally get no barrier analysis or family communication preferences.

**Referral statuses**

| Status | Meaning | Who/what can set it |
|---|---|---|
| `referred` | Created, no contact yet | system (creation / escalation rejection) |
| `follow_up_in_progress` | Follow-up under way | health_worker, admin; auto on first follow-up |
| `escalation_pending` | AI escalation recommendation awaiting human review | escalation workflow only |
| `escalated` | Human approved the escalation | escalation approval only |
| `confirmed` | Facility confirmed the patient arrived | facility_staff (receiving), admin — **never AI** |
| `completed` | Facility recorded final outcome (`outcome` required) | facility_staff (receiving), admin; only from `confirmed` |
| `cancelled` / `lost_to_follow_up` | Closed without arrival | health_worker, admin |

Outcomes: `treated_and_discharged | admitted | referred_onward | declined_care`.
Terminal: `completed`, `cancelled`, `lost_to_follow_up`. Open = referred, follow_up_in_progress, escalation_pending, escalated.

### Follow-ups (health_worker, admin; facility staff -> 403)
| Method & path | Notes |
|---|---|
| `POST /followups/` | `{"referral_id": 1}` (+ optional `channel`, `scheduled_at`). Agent 2 picks channel/template/timing; schedule is capped by policy. One pending follow-up per referral. |
| `GET /followups/referral/{referral_id}` | Attempts for a referral. |
| `GET /followups/pending` | Due now; `?include_upcoming=true` for all unattempted. |
| `PATCH /followups/{id}` | Record attempt: `{"contact_outcome": "no_answer", "conversation_summary": "..."}` -> response `{"followup": {...}, "next_followup": {...}|null}`; `next_recommended_action` is set and the next attempt is auto-scheduled when appropriate. |

`contact_outcome`: `pending | reached_will_attend | reached_needs_support | reached_declined | no_answer | number_unreachable | message_delivered | message_failed`.
`next_recommended_action`: `await_facility_confirmation | schedule_next_attempt | recommend_escalation | verify_contact_details | arrange_support_review`.
`channel`: `sms | phone_call | whatsapp`. Responses include `message_template_id` and `message_preview` (approved text only).

### Escalations
| Method & path | Who |
|---|---|
| `POST /escalations/` `{"referral_id": 1}` | health_worker (own), admin. Runs Agent 3; created as `pending_review`. |
| `GET /escalations/pending` | health_worker (own), admin |
| `GET /escalations/{id}` | health_worker (own), admin |
| `POST /escalations/{id}/approve` `{"reviewer_notes": "optional"}` | human reviewer: admin, or the referral's health worker |
| `POST /escalations/{id}/reject` `{"reviewer_notes": "required reason"}` | same |

Response includes the immutable `ai_recommendation` (original AI output), `ai_generated_brief`, `reason`,
`recommended_action`, `urgency_level` (= max(human, AI)), `uncertainty_flag`, `reviewed_by`, `reviewed_at`, `reviewer_notes`.
Statuses: `pending_review | approved | rejected | superseded` (superseded = referral confirmed/closed before review).
Approving moves the referral to `escalated`; rejecting restores the prior status. **Nothing is dispatched automatically.**

### Dashboard (all scoped to the caller)
`GET /dashboard/summary`, `/referral-statistics`, `/urgent-referrals?limit=`, `/pending-confirmations?limit=`.
```json
{"total_referrals": 4, "pending_referrals": 2, "confirmed_referrals": 1, "overdue_referrals": 1,
 "pending_escalations": 1, "referral_confirmation_rate": 0.3333,
 "followups": {"total": 2, "attempted": 1, "pending_due": 0, "pending_upcoming": 1,
               "successful_contacts": 1, "contact_success_rate": 1.0}}
```
`referral_confirmation_rate` = confirmed+completed / (total - cancelled). Overdue = open and past deadline.

### Audit
`GET /audit-logs/?referral_id=&action=&user_id=&page=` (admin). Entries: `user_id, referral_id, action, input_snapshot, output_snapshot, timestamp`.
Patient names and free-text notes are redacted from snapshots.

## 5. AI agent integration contract (for Member 2)

File: `app/integrations/ai_agents.py`. Implement `AIAgentProvider` (3 methods), then set
`AI_AGENT_PROVIDER=your_package.module:YourClass` (no frontend/API change needed). Methods receive a Pydantic
request and return the response model **or an equivalent dict**; the gateway (`AIAgentService`) re-validates
everything. Extra/unknown fields are rejected. Raise any exception for failure: the backend falls back safely
(flagged for human review) and audits the failure.

| Agent | Method | Request | Response |
|---|---|---|---|
| 1 Barrier interpretation | `interpret_barrier` | `referral_note`, `human_assigned_urgency`, `referral_context` | `barrier_type` (financial/transportation/fear/confusion/family_social/facility_access/other/unknown), `extracted_evidence[]`, `explanation`, `confidence` 0-1, `uncertainty_flag`, `recommended_logistical_action` |
| 2 Contact strategy | `recommend_contact_strategy` | `referral_context`, `barrier_type`, `previous_attempts[]` (attempt_number, channel, contact_outcome, attempted_at), `family_communication_preferences` | `recommended_channel`, `recommended_delay_hours` (0-168), `preferred_time_of_day`, `message_template_id` (must be in `APPROVED_TEMPLATES`), `reasoning`, `confidence`, `requires_human_review` |
| 3 Escalation synthesis | `synthesize_escalation` | `referral_context`, `referral_note`, `barrier_interpretation`, `followup_history[]`, `case_events[]` | `case_summary`, `escalation_reason`, `recommended_action` (supervisor_review/arrange_transport_support/request_financial_assistance/facility_direct_contact/home_visit_review/community_outreach_review), `urgency_recommendation`, `supporting_evidence[]`, `uncertainty_flag` |

`referral_context`: `{referral_reference, status, human_assigned_urgency, age, deadline, hours_until_deadline, referring_facility_type, receiving_facility_type}`.
**No patient name or direct identifier is ever sent to an agent**; follow-up free text is not sent either.

Backend-enforced safety on AI output (cannot be bypassed by a provider):
* invalid schema / unknown enum / unapproved template / extra field -> `InvalidAIResponseError` -> safe fallback;
* confidence below `AI_CONFIDENCE_THRESHOLD` or `unknown` barrier -> `uncertainty_flag` / `requires_human_review` forced on;
* recommending a channel on the family's `do_not_contact_channels` is rejected;
* escalation urgency = `max(human, AI)`; a lower AI value is ignored, logged as `safety.urgency_downgrade_blocked`;
* AI has no code path to change `human_assigned_urgency`, confirm/complete a referral, or approve an escalation;
* AI can only shorten (never lengthen) follow-up intervals; deadlines never involve AI.
Messages to families may only use `APPROVED_TEMPLATES` (logistical text, no medical advice). `home_visit_review` is a recommendation for humans only; the backend has no dispatch capability.

## 6. Deadline & scheduling policy (`services/scheduling_service.py`)
Deterministic, no AI. `deadline = created_at + DEADLINE_HOURS_<urgency>` (defaults: emergency 6h, urgent 24h,
semi_urgent 72h, routine 168h). Follow-up interval ceiling by urgency (1/6/24/48h). Once the deadline passes,
escalation is mandatory: the scheduler (and `escalation_candidates`) creates a **pending** recommendation; also when
`MAX_FOLLOWUP_ATTEMPTS` is reached without a "will attend" contact.

## 7. Security mechanisms
bcrypt password hashing; JWT (HS256, expiry enforced, `alg=none`/forged tokens rejected); role + facility re-read
from the DB on every request (token claims never trusted); `extra="forbid"` on all input models;
server-derived ownership; per-record scoping (404 on out-of-scope IDs); login throttling (in-memory, per email);
role-gated status transitions; audit logging with redaction; production refuses weak `SECRET_KEY`.

## 8. Known limitations
See the final report / project notes: login throttle is per-process; no refresh tokens; PostgreSQL DDL verified by
offline SQL generation only; real notification delivery (SMS etc.) is out of scope; 
Swagger UI is enabled by default (disable in production if desired).
