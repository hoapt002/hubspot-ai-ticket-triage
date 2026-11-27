"""HTTP application: webhook intake, reviewer UI, metrics."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import uuid
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.sessions import SessionMiddleware

from ticket_triage import auth, metrics
from ticket_triage.db import get_sessionmaker
from ticket_triage.models import ReviewKind, ReviewStatus, ReviewTask, Ticket, TicketStatus, User
from ticket_triage.pipeline import CUSTOMER_FIELDS, Pipeline, ReviewConflictError, ingest
from ticket_triage.settings import Settings, get_settings
from ticket_triage.taxonomy import Taxonomy, get_taxonomy
from ticket_triage.webhooks import signature
from ticket_triage.webhooks.payloads import (
    GenericTicketPayload,
    NormalizedTicket,
    PostmarkInbound,
)
from ticket_triage.worker import build_pipeline

WEB_DIR = Path(__file__).parent / "web"
templates = Jinja2Templates(directory=WEB_DIR / "templates")
MAX_WEBHOOK_BYTES = 256 * 1024


def get_db() -> Iterator[Session]:
    """Request-scoped session. Handlers that write must ``commit()`` before
    returning: the teardown below may run after the response is sent."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


DB = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[uuid.UUID, Depends(auth.current_user_id)]


def create_app(
    settings: Settings | None = None,
    taxonomy: Taxonomy | None = None,
    pipeline: Pipeline | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    taxonomy = taxonomy or get_taxonomy()
    pipeline = pipeline or build_pipeline(settings, taxonomy)
    secret = settings.session_secret.get_secret_value()
    if len(secret) < 32:
        raise RuntimeError("SESSION_SECRET must be at least 32 characters")

    app = FastAPI(title="HubSpot AI Ticket Triage", docs_url="/api/docs", redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="triage_session",
        same_site="lax",
        https_only=settings.secure_cookies,
        max_age=12 * 3600,
    )
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    templates.env.globals["taxonomy"] = taxonomy
    templates.env.globals["hubspot_portal_id"] = settings.hubspot_portal_id
    templates.env.globals["hubspot_enabled"] = settings.hubspot_enabled
    templates.env.filters["duration"] = format_duration
    templates.env.filters["timesince"] = format_timesince
    templates.env.filters["utc"] = format_utc
    templates.env.globals["icon"] = icon

    @app.exception_handler(auth.LoginRequiredError)
    async def _login_redirect(request: Request, exc: auth.LoginRequiredError) -> Response:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    def render(request: Request, name: str, **context: Any) -> HTMLResponse:
        context.setdefault("csrf", auth.csrf_token(request))
        context.setdefault("user_name", request.session.get("user_name"))
        return templates.TemplateResponse(request, name, context)

    # ------------------------------------------------------------------ webhooks

    async def read_body(request: Request) -> bytes:
        body = await request.body()
        if len(body) > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "payload too large")
        return body

    @app.post("/webhooks/tickets/{source}", status_code=202)
    async def generic_webhook(source: str, request: Request, db: DB) -> JSONResponse:
        secret_value = settings.webhook_secrets.get(source)
        if secret_value is None:
            raise HTTPException(404, "unknown source")
        body = await read_body(request)
        try:
            signature.verify(
                secret_value.get_secret_value(),
                body,
                request.headers.get(signature.HEADER),
                settings.webhook_tolerance_seconds,
            )
        except signature.SignatureError as exc:
            raise HTTPException(401, f"invalid signature: {exc}") from exc
        try:
            raw = json.loads(body)
            payload = GenericTicketPayload.model_validate(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise HTTPException(422, f"invalid payload: {exc}") from exc
        ticket_id, created = pipeline_ingest(db, source, payload.normalize(), raw)
        return JSONResponse(
            {"ticket_id": str(ticket_id), "duplicate": not created},
            status_code=202 if created else 200,
        )

    @app.post("/webhooks/postmark", status_code=202)
    async def postmark_webhook(request: Request, db: DB) -> JSONResponse:
        _check_basic_auth(request, settings)
        body = await read_body(request)
        try:
            raw = json.loads(body)
            payload = PostmarkInbound.model_validate(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise HTTPException(422, f"invalid payload: {exc}") from exc
        ticket_id, created = pipeline_ingest(db, "postmark", payload.normalize(), raw)
        return JSONResponse(
            {"ticket_id": str(ticket_id), "duplicate": not created},
            status_code=202 if created else 200,
        )

    def pipeline_ingest(
        db: Session, source: str, normalized: NormalizedTicket, raw: dict[str, Any]
    ) -> tuple[uuid.UUID, bool]:
        result = ingest(db, source=source, ticket=normalized, raw_payload=raw, settings=settings)
        # Commit before acknowledging: a 2xx tells the sender it may forget the ticket.
        db.commit()
        return result

    # ------------------------------------------------------------------ auth

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/") -> HTMLResponse:
        return render(request, "login.html", next=_safe_next(next), error=None)

    @app.post("/login")
    def login(
        request: Request,
        db: DB,
        email: Annotated[str, Form()],
        password: Annotated[str, Form()],
        csrf: Annotated[str, Form()],
        next: Annotated[str, Form()] = "/",
    ) -> Response:
        auth.check_csrf(request, csrf)
        user = auth.authenticate(db, email, password)
        if user is None:
            response = render(
                request, "login.html", next=_safe_next(next), error="Wrong email or password."
            )
            response.status_code = 401
            return response
        request.session.clear()
        request.session["user_id"] = str(user.id)
        request.session["user_name"] = user.name
        return RedirectResponse(_safe_next(next), status_code=303)

    @app.post("/logout")
    def logout(request: Request, csrf: Annotated[str, Form()]) -> Response:
        auth.check_csrf(request, csrf)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ------------------------------------------------------------------ pages

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, db: DB, _: CurrentUser, days: int = 30) -> HTMLResponse:
        days = min(max(days, 1), 365)
        report = metrics.build_report(db, days=days)
        return render(
            request,
            "dashboard.html",
            report=report,
            days=days,
            settings_baseline_rate=settings.manual_baseline_rate,
            min_baseline=metrics.MIN_BASELINE_SAMPLE,
            active="dashboard",
        )

    @app.get("/api/metrics")
    def metrics_json(db: DB, _: CurrentUser, days: int = 30) -> JSONResponse:
        report = metrics.build_report(db, days=min(max(days, 1), 365))
        return JSONResponse(json.loads(json.dumps(asdict(report), default=str)))

    @app.get("/queue", response_class=HTMLResponse)
    def queue(request: Request, db: DB, _: CurrentUser) -> HTMLResponse:
        tasks = db.scalars(
            select(ReviewTask)
            .where(ReviewTask.status == ReviewStatus.OPEN)
            .options(selectinload(ReviewTask.ticket))
            .order_by(ReviewTask.created_at)
        ).all()
        return render(request, "queue.html", tasks=tasks, active="queue")

    @app.get("/reviews/{task_id}", response_class=HTMLResponse)
    def review_form(request: Request, task_id: uuid.UUID, db: DB, uid: CurrentUser) -> HTMLResponse:
        task = db.get(ReviewTask, task_id)
        if task is None:
            raise HTTPException(404)
        if task.status != ReviewStatus.OPEN:
            return render(request, "review_done.html", task=task, active="queue")
        session_id = pipeline.open_review(db, task.id, uid)
        db.commit()
        prefill = task.proposal or {"customer": {}}
        if task.kind == ReviewKind.MANUAL_TRIAGE:
            # Manual triage starts from the raw message only; channel metadata
            # is what an agent sees in any helpdesk.
            prefill = {
                "customer": {"email": task.ticket.sender_email, "name": task.ticket.sender_name}
            }
        return render(
            request,
            "review.html",
            task=task,
            ticket=task.ticket,
            prefill=prefill,
            review_session_id=session_id,
            customer_fields=CUSTOMER_FIELDS,
            error=None,
            active="queue",
        )

    @app.post("/reviews/{task_id}")
    async def submit_review(
        request: Request, task_id: uuid.UUID, db: DB, uid: CurrentUser
    ) -> Response:
        form = await request.form()
        auth.check_csrf(request, _str(form.get("csrf")))
        decision = {
            "category": _str(form.get("category")),
            "priority": _str(form.get("priority")),
            "summary": _str(form.get("summary")).strip(),
            "language": _str(form.get("language")).strip() or None,
            "sentiment": _str(form.get("sentiment")) or None,
            "customer": {
                f: (_str(form.get(f"customer_{f}")).strip() or None) for f in CUSTOMER_FIELDS
            },
        }
        try:
            review_session_id = uuid.UUID(_str(form.get("review_session_id")))
            ticket = pipeline.submit_review(
                db,
                task_id=task_id,
                review_session_id=review_session_id,
                user_id=uid,
                decision=decision,
            )
        except (ReviewConflictError, ValueError) as exc:
            db.rollback()
            return render(
                request,
                "error.html",
                message=str(exc),
                back_url="/queue",
                active="queue",
            )
        db.commit()  # visible to the redirected GET, which may arrive first
        return RedirectResponse(f"/queue?done={ticket.id}", status_code=303)

    @app.get("/tickets", response_class=HTMLResponse)
    def tickets(
        request: Request, db: DB, _: CurrentUser, status: str | None = None
    ) -> HTMLResponse:
        query = select(Ticket).order_by(Ticket.received_at.desc()).limit(200)
        if status:
            try:
                query = query.where(Ticket.status == TicketStatus(status))
            except ValueError as exc:
                raise HTTPException(400, "unknown status") from exc
        rows = db.scalars(query).all()
        return render(
            request,
            "tickets.html",
            tickets=rows,
            status=status,
            statuses=list(TicketStatus),
            active="tickets",
        )

    @app.get("/tickets/{ticket_id}", response_class=HTMLResponse)
    def ticket_detail(
        request: Request, ticket_id: uuid.UUID, db: DB, _: CurrentUser
    ) -> HTMLResponse:
        ticket = db.scalar(
            select(Ticket)
            .where(Ticket.id == ticket_id)
            .options(selectinload(Ticket.triage_runs), selectinload(Ticket.review_tasks))
        )
        if ticket is None:
            raise HTTPException(404)
        reviewers = {
            u.id: u.name
            for u in db.scalars(
                select(User).where(
                    User.id.in_([t.decided_by for t in ticket.review_tasks if t.decided_by])
                )
            )
        }
        return render(request, "ticket.html", ticket=ticket, reviewers=reviewers, active="tickets")

    @app.get("/healthz")
    def healthz(db: DB) -> dict[str, str]:
        db.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _safe_next(target: str) -> str:
    return target if target.startswith("/") and not target.startswith("//") else "/"


def _check_basic_auth(request: Request, settings: Settings) -> None:
    password = settings.postmark_inbound_password.get_secret_value()
    if not settings.postmark_inbound_user or not password:
        raise HTTPException(404, "postmark inbound is not configured")
    header = request.headers.get("authorization", "")
    scheme, _, encoded = header.partition(" ")
    try:
        user, _, given = base64.b64decode(encoded).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError):
        user, given = "", ""
    ok = (
        scheme.lower() == "basic"
        and hmac.compare_digest(user, settings.postmark_inbound_user)
        and hmac.compare_digest(given, password)
    )
    if not ok:
        raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Basic"})


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 90:
        return f"{seconds:.1f} s"
    if seconds < 5400:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.1f} h"


def format_utc(moment: datetime, fmt: str) -> str:
    return moment.astimezone(UTC).strftime(fmt)


def format_timesince(moment: datetime) -> str:
    return format_duration((datetime.now(UTC) - moment).total_seconds())


@cache
def icon(name: str, size: int = 18) -> Markup:
    """Inline an Iconsax SVG from static/icons (stroke uses currentColor)."""
    svg = (WEB_DIR / "static" / "icons" / f"{name}.svg").read_text(encoding="utf-8")
    svg = svg.replace('width="24" height="24"', f'width="{size}" height="{size}"', 1)
    svg = svg.replace("<svg ", '<svg class="icon" aria-hidden="true" ', 1)
    if size <= 16:
        svg = svg.replace('stroke-width="1.5"', 'stroke-width="1.8"')
    return Markup(svg)  # noqa: S704 - trusted local asset
