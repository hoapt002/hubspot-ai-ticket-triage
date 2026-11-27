"""Ticket lifecycle: intake, AI triage, review routing, human decisions, CRM sync."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ticket_triage import jobs
from ticket_triage.crm.hubspot import HubSpotClient, HubSpotError, TicketDraft, split_name
from ticket_triage.llm.prompt import PROMPT_VERSION, system_prompt, user_prompt
from ticket_triage.llm.providers import LLMError, LLMResult, TriageModel
from ticket_triage.llm.schema import output_model_for
from ticket_triage.models import (
    ProcessingPath,
    ReviewKind,
    ReviewSession,
    ReviewStatus,
    ReviewTask,
    Ticket,
    TicketStatus,
    TriageRun,
)
from ticket_triage.settings import Settings
from ticket_triage.taxonomy import Taxonomy
from ticket_triage.webhooks.payloads import NormalizedTicket

CUSTOMER_FIELDS = ("name", "email", "phone", "company", "order_id", "account_id", "product")
REASON_BASELINE = "baseline_sample"
REASON_AI_FAILED = "ai_failed"

HUBSPOT_SOURCE_TYPES = {"email": "EMAIL", "chat": "CHAT", "phone": "PHONE"}


@dataclass(frozen=True)
class Retry:
    """Returned by a job handler to request a retry with backoff. Changes made
    in the session (e.g. the failed attempt's audit row) are still committed."""

    error: str


class ReviewConflictError(Exception):
    pass


# --------------------------------------------------------------------------- intake


def ingest(
    session: Session,
    *,
    source: str,
    ticket: NormalizedTicket,
    raw_payload: dict[str, Any],
    settings: Settings,
) -> tuple[uuid.UUID, bool]:
    """Store a ticket and enqueue triage. Returns (ticket_id, created). A
    redelivered webhook (same source + external id) is a no-op."""
    ticket_id = uuid.uuid4()
    inserted = session.execute(
        insert(Ticket)
        .values(
            id=ticket_id,
            source=source,
            external_id=ticket.external_id,
            channel=ticket.channel,
            subject=ticket.subject,
            body=ticket.body,
            sender_email=ticket.sender_email,
            sender_name=ticket.sender_name,
            raw_payload=raw_payload,
            status=TicketStatus.RECEIVED,
            customer={},
        )
        .on_conflict_do_nothing(constraint="uq_tickets_source_external_id")
        .returning(Ticket.id)
    ).scalar_one_or_none()
    if inserted is None:
        existing = session.execute(
            select(Ticket.id).where(
                Ticket.source == source, Ticket.external_id == ticket.external_id
            )
        ).scalar_one()
        return existing, False
    jobs.enqueue(session, jobs.TRIAGE, ticket_id, settings.job_max_attempts)
    return ticket_id, True


# --------------------------------------------------------------------------- policy


def in_manual_baseline(ticket_id: uuid.UUID, rate: float) -> bool:
    """Deterministic sampling so a retried job makes the same decision."""
    return (ticket_id.int >> 96) / float(1 << 32) < rate


def review_reasons(output: dict[str, Any], customer_email: str | None, tx: Taxonomy) -> list[str]:
    policy = tx.review_policy
    reasons: list[str] = []
    if float(output["confidence"]) < policy.min_confidence:
        reasons.append("low_confidence")
    if output["priority"] in policy.always_review_priorities:
        reasons.append(f"priority_{output['priority']}")
    if output["category"] in policy.always_review_categories:
        reasons.append(f"category_{output['category']}")
    customer = dict(output.get("customer") or {})
    if customer_email:
        customer["email"] = customer_email
    missing = [f for f in policy.required_customer_fields if not customer.get(f)]
    reasons.extend(f"missing_{f}" for f in missing)
    if output.get("needs_human"):
        reasons.append("model_flagged")
    return reasons


def merged_customer(extracted: dict[str, Any] | None, ticket: Ticket) -> dict[str, Any]:
    customer = {f: (extracted or {}).get(f) for f in CUSTOMER_FIELDS}
    # Sender metadata from the channel is more reliable than text extraction.
    if ticket.sender_email:
        customer["email"] = ticket.sender_email
    if ticket.sender_name and not customer.get("name"):
        customer["name"] = ticket.sender_name
    return customer


# --------------------------------------------------------------------------- services


@dataclass
class Pipeline:
    settings: Settings
    taxonomy: Taxonomy
    model_factory: Callable[[], TriageModel]
    hubspot_factory: Callable[[], HubSpotClient] | None = None
    _model: TriageModel | None = field(default=None, init=False, repr=False)

    @property
    def model(self) -> TriageModel:
        if self._model is None:
            self._model = self.model_factory()
        return self._model

    # ----- triage job

    def run_triage(self, session: Session, job: jobs.ClaimedJob) -> Retry | None:
        ticket = session.get(Ticket, job.ticket_id, with_for_update=True)
        if ticket is None or ticket.status != TicketStatus.RECEIVED:
            return None  # already handled (redelivery or a previous attempt committed)

        baseline = in_manual_baseline(ticket.id, self.settings.manual_baseline_rate)
        try:
            result = self._classify(ticket)
        except LLMError as exc:
            self._record_run(session, ticket, None, shadow=baseline, error=str(exc))
            if job.attempts < job.max_attempts:
                return Retry(str(exc))
            # Out of retries: a person triages it so the ticket is never lost.
            self._open_manual_task(session, ticket, [REASON_AI_FAILED])
            return None

        output = result.output
        output["confidence"] = min(max(float(output["confidence"]), 0.0), 1.0)
        self._record_run(session, ticket, result, shadow=baseline)

        if baseline:
            self._open_manual_task(session, ticket, [REASON_BASELINE])
            return None

        reasons = review_reasons(output, ticket.sender_email, self.taxonomy)
        if reasons:
            session.add(
                ReviewTask(
                    ticket_id=ticket.id,
                    kind=ReviewKind.VERIFY_AI,
                    reasons=reasons,
                    proposal=self._proposal(output, ticket),
                )
            )
            ticket.ai_confidence = output["confidence"]
            ticket.status = TicketStatus.AWAITING_REVIEW
            return None

        self._finalize(session, ticket, self._proposal(output, ticket), ProcessingPath.AI_AUTO)
        ticket.ai_confidence = output["confidence"]
        return None

    def _classify(self, ticket: Ticket) -> LLMResult:
        return self.model.classify(
            system_prompt(self.taxonomy),
            user_prompt(
                channel=ticket.channel,
                subject=ticket.subject,
                body=ticket.body,
                sender_email=ticket.sender_email,
                sender_name=ticket.sender_name,
            ),
            output_model_for(self.taxonomy),
        )

    def _record_run(
        self,
        session: Session,
        ticket: Ticket,
        result: LLMResult | None,
        *,
        shadow: bool,
        error: str | None = None,
    ) -> None:
        run = TriageRun(
            ticket_id=ticket.id,
            provider=result.provider if result else self.settings.llm_provider,
            model=result.model if result else self.settings.resolved_llm_model,
            prompt_version=PROMPT_VERSION,
            shadow=shadow,
            output=result.output if result else None,
            error=error,
            input_tokens=result.input_tokens if result else 0,
            output_tokens=result.output_tokens if result else 0,
            latency_ms=result.latency_ms if result else 0,
            cost_usd=self._cost(result) if result else Decimal("0"),
        )
        session.add(run)

    def _cost(self, result: LLMResult) -> Decimal:
        s = self.settings
        return (
            Decimal(result.input_tokens) * s.llm_price_input_per_mtok
            + Decimal(result.output_tokens) * s.llm_price_output_per_mtok
        ) / Decimal(1_000_000)

    def _proposal(self, output: dict[str, Any], ticket: Ticket) -> dict[str, Any]:
        return {
            "category": output["category"],
            "priority": output["priority"],
            "summary": output["summary"],
            "language": output["language"],
            "sentiment": output["sentiment"],
            "customer": merged_customer(output.get("customer"), ticket),
            "rationale": output.get("rationale"),
            "confidence": output["confidence"],
            "needs_human_reason": output.get("needs_human_reason"),
        }

    def _open_manual_task(self, session: Session, ticket: Ticket, reasons: list[str]) -> None:
        session.add(
            ReviewTask(
                ticket_id=ticket.id, kind=ReviewKind.MANUAL_TRIAGE, reasons=reasons, proposal=None
            )
        )
        ticket.status = TicketStatus.AWAITING_MANUAL

    def _finalize(
        self, session: Session, ticket: Ticket, decision: dict[str, Any], path: ProcessingPath
    ) -> None:
        category = decision["category"]
        ticket.category = category
        ticket.priority = decision["priority"]
        ticket.team = self.taxonomy.categories[category].team
        ticket.summary = decision.get("summary")
        ticket.language = decision.get("language")
        ticket.sentiment = decision.get("sentiment")
        ticket.customer = {f: decision.get("customer", {}).get(f) for f in CUSTOMER_FIELDS}
        ticket.processing_path = path
        ticket.status = TicketStatus.TRIAGED
        ticket.triaged_at = datetime.now(UTC)
        jobs.enqueue(session, jobs.CRM_SYNC, ticket.id, self.settings.job_max_attempts)

    def on_job_dead(self, session: Session, job: jobs.ClaimedJob, error: str) -> None:
        """A job failed permanently with an unexpected error: make sure the
        ticket still reaches a person instead of silently stalling."""
        ticket = session.get(Ticket, job.ticket_id, with_for_update=True)
        if ticket is None:
            return
        if job.kind == jobs.TRIAGE and ticket.status == TicketStatus.RECEIVED:
            self._open_manual_task(session, ticket, [REASON_AI_FAILED])
        elif job.kind == jobs.CRM_SYNC and ticket.status == TicketStatus.TRIAGED:
            ticket.status = TicketStatus.FAILED
            ticket.crm_error = error[:4000]

    # ----- human review

    def open_review(self, session: Session, task_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
        review_session = ReviewSession(task_id=task_id, user_id=user_id)
        session.add(review_session)
        session.flush()
        return review_session.id

    def submit_review(
        self,
        session: Session,
        *,
        task_id: uuid.UUID,
        review_session_id: uuid.UUID,
        user_id: uuid.UUID,
        decision: dict[str, Any],
    ) -> Ticket:
        task = session.get(ReviewTask, task_id, with_for_update=True)
        if task is None:
            raise ReviewConflictError("review task not found")
        if task.status != ReviewStatus.OPEN:
            raise ReviewConflictError("this ticket was already decided by another reviewer")
        if decision["category"] not in self.taxonomy.categories:
            raise ValueError(f"unknown category {decision['category']!r}")
        if decision["priority"] not in self.taxonomy.priorities:
            raise ValueError(f"unknown priority {decision['priority']!r}")

        review_session = session.get(ReviewSession, review_session_id)
        if (
            review_session is None
            or review_session.task_id != task.id
            or review_session.user_id != user_id
        ):
            raise ReviewConflictError("review session does not belong to this task")
        now = datetime.now(UTC)
        review_session.submitted_at = now

        task.status = ReviewStatus.DONE
        task.decision = decision
        task.decided_by = user_id
        task.decided_at = now
        if task.proposal:
            task.changed_fields = changed_fields(task.proposal, decision)

        ticket = task.ticket
        path = (
            ProcessingPath.AI_REVIEWED
            if task.kind == ReviewKind.VERIFY_AI
            else ProcessingPath.MANUAL
        )
        self._finalize(session, ticket, decision, path)
        return ticket

    # ----- CRM sync job

    def run_crm_sync(self, session: Session, job: jobs.ClaimedJob) -> Retry | None:
        ticket = session.get(Ticket, job.ticket_id, with_for_update=True)
        if ticket is None or ticket.status not in (TicketStatus.TRIAGED, TicketStatus.FAILED):
            return None
        if ticket.crm_ticket_id:
            ticket.status = TicketStatus.SYNCED
            return None
        if self.hubspot_factory is None:
            ticket.status = TicketStatus.CRM_DISABLED
            return None

        draft = self.ticket_draft(ticket)
        client = self.hubspot_factory()
        try:
            result = client.sync(draft)
        except HubSpotError as exc:
            ticket.crm_error = str(exc)[:4000]
            if exc.retryable and job.attempts < job.max_attempts:
                return Retry(str(exc))
            ticket.status = TicketStatus.FAILED
            return None
        finally:
            client.close()
        ticket.crm_ticket_id = result.ticket_id
        ticket.crm_contact_id = result.contact_id
        ticket.crm_synced_at = datetime.now(UTC)
        ticket.crm_error = None
        ticket.status = TicketStatus.SYNCED
        return None

    def ticket_draft(self, ticket: Ticket) -> TicketDraft:
        assert ticket.category and ticket.priority
        category = self.taxonomy.categories[ticket.category]
        priority = self.taxonomy.priorities[ticket.priority]
        customer = ticket.customer or {}
        first, last = split_name(customer.get("name"))
        details = [
            f"{label}: {customer[key]}"
            for key, label in (
                ("order_id", "Order"),
                ("account_id", "Account"),
                ("product", "Product"),
                ("company", "Company"),
                ("phone", "Phone"),
            )
            if customer.get(key)
        ]
        content = "\n".join(
            [
                f"Summary: {ticket.summary or ''}",
                f"Category: {ticket.category} | Priority: {ticket.priority} "
                f"(first response within {priority.sla_hours}h) | Team: {ticket.team}",
                f"Sentiment: {ticket.sentiment or 'n/a'} | Language: {ticket.language or 'n/a'}",
                *details,
                f"Triage: {ticket.processing_path.value if ticket.processing_path else ''} "
                f"| Ref: {ticket.source}/{ticket.external_id}",
                "",
                "--- Original message ---",
                ticket.body,
            ]
        )
        return TicketDraft(
            subject=ticket.subject or (ticket.summary or "Support request"),
            content=content,
            priority=priority.hubspot_priority,
            category=category.hubspot_category,
            source_type=HUBSPOT_SOURCE_TYPES.get(ticket.channel, "FORM"),
            contact_email=customer.get("email"),
            contact_firstname=first,
            contact_lastname=last,
            contact_phone=customer.get("phone"),
            contact_company=customer.get("company"),
        )


def changed_fields(proposal: dict[str, Any], decision: dict[str, Any]) -> list[str]:
    changed = [
        key
        for key in ("category", "priority", "summary", "language", "sentiment")
        if (proposal.get(key) or None) != (decision.get(key) or None)
    ]
    p_customer = proposal.get("customer") or {}
    d_customer = decision.get("customer") or {}
    changed.extend(
        f"customer.{f}"
        for f in CUSTOMER_FIELDS
        if (p_customer.get(f) or None) != (d_customer.get(f) or None)
    )
    return changed
