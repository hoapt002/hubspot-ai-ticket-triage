"""End-to-end flows through HTTP, the Postgres queue and the worker."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from tests.conftest import (
    FakeHubSpot,
    ScriptedModel,
    drain,
    extract_csrf,
    extract_value,
    login,
    post_ticket,
    ticket_payload,
    triage_output,
)
from ticket_triage import metrics
from ticket_triage.llm.providers import LLMError
from ticket_triage.models import (
    Job,
    ProcessingPath,
    ReviewKind,
    ReviewSession,
    ReviewStatus,
    ReviewTask,
    Ticket,
    TicketStatus,
    TriageRun,
    User,
)
from ticket_triage.settings import Settings
from ticket_triage.webhooks.signature import HEADER, sign
from ticket_triage.worker import Worker


def _ticket(db: Session) -> Ticket:
    db.expire_all()
    return db.scalars(select(Ticket)).one()


def test_confident_ticket_goes_straight_to_hubspot(
    client: TestClient, worker: Worker, db: Session, hubspot: FakeHubSpot
) -> None:
    response = post_ticket(client, ticket_payload())
    assert response.status_code == 202

    assert drain(worker) == 2  # triage + crm_sync

    ticket = _ticket(db)
    assert ticket.status == TicketStatus.SYNCED
    assert ticket.processing_path == ProcessingPath.AI_AUTO
    assert (ticket.category, ticket.priority, ticket.team) == ("billing", "P3", "billing")
    assert ticket.customer["email"] == "dana@northwind.example"  # sender metadata wins
    assert ticket.customer["order_id"] == "INV-20391"
    assert ticket.crm_ticket_id == "9001"
    assert ticket.crm_contact_id == "501"

    hs_ticket = hubspot.bodies("/crm/v3/objects/tickets")[0]["properties"]
    assert hs_ticket["hs_ticket_category"] == "BILLING_ISSUE"
    assert hs_ticket["hs_ticket_priority"] == "MEDIUM"
    assert "Order: INV-20391" in hs_ticket["content"]
    assert "I was charged twice for invoice INV-20391" in hs_ticket["content"]

    run = db.scalars(select(TriageRun)).one()
    assert (run.input_tokens, run.output_tokens) == (1200, 180)
    assert float(run.cost_usd) == (1200 * 0.75 + 180 * 4.5) / 1_000_000


def test_duplicate_delivery_is_idempotent(
    client: TestClient, worker: Worker, db: Session, model: ScriptedModel
) -> None:
    payload = ticket_payload(id="form-dup-1")
    first = post_ticket(client, payload)
    second = post_ticket(client, payload)
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json() == {"ticket_id": first.json()["ticket_id"], "duplicate": True}
    drain(worker)
    assert len(model.calls) == 1
    assert len(db.scalars(select(Ticket)).all()) == 1


def test_webhook_rejects_bad_signature_unknown_source_and_bad_payload(
    client: TestClient,
) -> None:
    body = json.dumps(ticket_payload()).encode()
    bad = client.post("/webhooks/tickets/web", content=body, headers={HEADER: sign("wrong", body)})
    assert bad.status_code == 401
    unknown = client.post("/webhooks/tickets/nope", content=body, headers={HEADER: sign("x", body)})
    assert unknown.status_code == 404
    invalid = b'{"id": "x"}'
    from tests.conftest import WEBHOOK_SECRET

    assert (
        client.post(
            "/webhooks/tickets/web",
            content=invalid,
            headers={HEADER: sign(WEBHOOK_SECRET, invalid)},
        ).status_code
        == 422
    )


def test_postmark_inbound(client: TestClient, worker: Worker, db: Session) -> None:
    payload = {
        "MessageID": "73e6d360-66eb-11e1-8e72-a8904824019b",
        "FromFull": {"Email": "kim@example.com", "Name": "Kim Lee"},
        "Subject": "Where is my parcel?",
        "TextBody": "Order 5521 has not arrived after 10 days.",
    }
    unauthorized = client.post("/webhooks/postmark", json=payload)
    assert unauthorized.status_code == 401
    token = base64.b64encode(b"postmark:pm-password-123").decode()
    ok = client.post(
        "/webhooks/postmark", json=payload, headers={"Authorization": f"Basic {token}"}
    )
    assert ok.status_code == 202
    ticket = _ticket(db)
    assert (ticket.channel, ticket.sender_email, ticket.source) == (
        "email",
        "kim@example.com",
        "postmark",
    )


def test_low_confidence_ticket_is_verified_by_a_person(
    client: TestClient,
    worker: Worker,
    db: Session,
    model: ScriptedModel,
    reviewer: User,
    hubspot: FakeHubSpot,
) -> None:
    model.responses.append(triage_output(confidence=0.55, priority="P4"))
    post_ticket(client, ticket_payload())
    assert drain(worker) == 1

    ticket = _ticket(db)
    assert ticket.status == TicketStatus.AWAITING_REVIEW
    task = db.scalars(select(ReviewTask)).one()
    assert task.kind == ReviewKind.VERIFY_AI
    assert task.reasons == ["low_confidence"]

    # Reviewer UI requires login.
    assert client.get("/queue", follow_redirects=False).status_code == 303
    login(client)
    queue = client.get("/queue")
    assert "Charged twice for March" in queue.text

    form = client.get(f"/reviews/{task.id}")
    assert form.status_code == 200
    assert 'value="billing" title=' in form.text
    response = client.post(
        f"/reviews/{task.id}",
        data={
            "csrf": extract_csrf(form.text),
            "review_session_id": extract_value(form.text, "review_session_id"),
            "category": "billing",
            "priority": "P2",
            "summary": "Duplicate charge on INV-20391; refund one payment.",
            "language": "en",
            "sentiment": "negative",
            "customer_name": "Dana Whitfield",
            "customer_email": "dana@northwind.example",
            "customer_order_id": "INV-20391",
            "customer_company": "Northwind Labs",
            "customer_product": "Pro plan",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    db.expire_all()
    task = db.get(ReviewTask, task.id)
    assert task is not None
    assert task.status == ReviewStatus.DONE
    assert task.decided_by == reviewer.id
    assert task.changed_fields == ["priority", "summary"]
    session = db.scalars(select(ReviewSession)).one()
    assert session.submitted_at is not None

    assert drain(worker) == 1
    ticket = _ticket(db)
    assert ticket.status == TicketStatus.SYNCED
    assert ticket.processing_path == ProcessingPath.AI_REVIEWED
    assert ticket.priority == "P2"
    assert (
        hubspot.bodies("/crm/v3/objects/tickets")[0]["properties"]["hs_ticket_priority"] == "HIGH"
    )

    # A second submit of the same task is refused.
    again = client.post(
        f"/reviews/{task.id}",
        data={
            "csrf": extract_csrf(form.text),
            "review_session_id": extract_value(form.text, "review_session_id"),
            "category": "other",
            "priority": "P4",
            "summary": "x",
        },
    )
    assert "already decided" in again.text


def test_review_post_requires_csrf(
    client: TestClient, worker: Worker, db: Session, model: ScriptedModel, reviewer: User
) -> None:
    model.responses.append(triage_output(confidence=0.2))
    post_ticket(client, ticket_payload())
    drain(worker)
    task = db.scalars(select(ReviewTask)).one()
    login(client)
    form = client.get(f"/reviews/{task.id}")
    response = client.post(
        f"/reviews/{task.id}",
        data={
            "csrf": "forged",
            "review_session_id": extract_value(form.text, "review_session_id"),
            "category": "billing",
            "priority": "P3",
            "summary": "x",
        },
    )
    assert response.status_code == 403


def test_manual_baseline_hides_ai_and_runs_shadow(
    client: TestClient,
    settings: Settings,
    worker: Worker,
    db: Session,
    reviewer: User,
) -> None:
    settings.manual_baseline_rate = 1.0
    post_ticket(client, ticket_payload())
    drain(worker)
    ticket = _ticket(db)
    assert ticket.status == TicketStatus.AWAITING_MANUAL
    run = db.scalars(select(TriageRun)).one()
    assert run.shadow is True
    task = db.scalars(select(ReviewTask)).one()
    assert (task.kind, task.reasons, task.proposal) == (
        ReviewKind.MANUAL_TRIAGE,
        ["baseline_sample"],
        None,
    )

    login(client)
    form = client.get(f"/reviews/{task.id}")
    assert "AI reasoning" not in form.text
    assert "Duplicate" not in form.text  # AI summary is not leaked into the form
    assert 'value="dana@northwind.example"' in form.text  # channel metadata is


def test_llm_failure_retries_then_falls_back_to_manual(
    client: TestClient, worker: Worker, db: Session, model: ScriptedModel
) -> None:
    model.responses.extend([LLMError("timeout")] * 3)
    post_ticket(client, ticket_payload())

    assert drain(worker) == 1
    job = db.scalars(select(Job)).one()
    assert job.attempts == 1 and job.done_at is None and job.last_error == "timeout"

    for _ in range(2):  # make the backed-off job runnable now
        db.execute(update(Job).values(run_after=datetime.now(UTC) - timedelta(seconds=1)))
        db.commit()
        drain(worker)

    ticket = _ticket(db)
    assert ticket.status == TicketStatus.AWAITING_MANUAL
    task = db.scalars(select(ReviewTask)).one()
    assert task.reasons == ["ai_failed"]
    assert len(db.scalars(select(TriageRun)).all()) == 3


def test_crm_permanent_error_marks_ticket_failed(
    client: TestClient, worker: Worker, db: Session, hubspot: FakeHubSpot
) -> None:
    hubspot.fail_with = 400
    post_ticket(client, ticket_payload())
    drain(worker)
    ticket = _ticket(db)
    assert ticket.status == TicketStatus.FAILED
    assert ticket.crm_error and "HTTP 400" in ticket.crm_error


def test_without_hubspot_ticket_is_marked_crm_disabled(
    client: TestClient, worker: Worker, db: Session
) -> None:
    worker.pipeline.hubspot_factory = None
    post_ticket(client, ticket_payload())
    drain(worker)
    assert _ticket(db).status == TicketStatus.CRM_DISABLED


def test_crashed_job_lock_is_reclaimed(
    client: TestClient, worker: Worker, db: Session, settings: Settings
) -> None:
    post_ticket(client, ticket_payload())
    db.execute(
        update(Job).values(
            locked_at=datetime.now(UTC) - timedelta(seconds=settings.worker_stale_lock_seconds + 5),
            locked_by="dead-worker",
            attempts=1,
        )
    )
    db.commit()
    assert drain(worker) == 2
    assert _ticket(db).status == TicketStatus.SYNCED


def test_metrics_compare_manual_and_automated(
    client: TestClient,
    worker: Worker,
    db: Session,
    settings: Settings,
    model: ScriptedModel,
    reviewer: User,
) -> None:
    login(client)
    # 5 manual baseline tickets, each taking a person 4 minutes.
    settings.manual_baseline_rate = 1.0
    for i in range(5):
        model.responses.append(triage_output(category="billing" if i < 4 else "other"))
        post_ticket(client, ticket_payload())
    drain(worker)
    for task in db.scalars(select(ReviewTask)).all():
        form = client.get(f"/reviews/{task.id}")
        db.execute(
            update(ReviewSession)
            .where(ReviewSession.task_id == task.id)
            .values(opened_at=ReviewSession.opened_at - timedelta(minutes=4))
        )
        db.commit()
        client.post(
            f"/reviews/{task.id}",
            data={
                "csrf": extract_csrf(form.text),
                "review_session_id": extract_value(form.text, "review_session_id"),
                "category": "billing",
                "priority": "P3",
                "summary": "Duplicate charge",
                "customer_email": "dana@northwind.example",
            },
        )
    # 10 fully automatic tickets.
    settings.manual_baseline_rate = 0.0
    for _ in range(10):
        post_ticket(client, ticket_payload())
    drain(worker, limit=100)

    report = metrics.build_report(db)
    assert report.paths["manual"].tickets == 5
    assert report.paths["ai_auto"].tickets == 10
    assert report.baseline_ready
    assert report.manual_touch_s is not None and 239 < report.manual_touch_s < 245
    assert report.hours_saved is not None
    assert abs(report.hours_saved - 10 * report.manual_touch_s / 3600) < 1e-9
    assert report.automation_rate == 1.0
    assert report.shadow_accuracy.sample == 5
    assert report.shadow_accuracy.category == 0.8
    assert report.llm_cost_per_ticket_usd is not None

    page = client.get("/")
    assert page.status_code == 200
    assert "Agent time saved" in page.text
    assert client.get("/api/metrics").json()["paths"]["ai_auto"]["tickets"] == 10
