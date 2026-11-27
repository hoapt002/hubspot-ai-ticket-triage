"""HubSpot CRM client: upsert the contact, create the ticket, associate them.

Uses a private-app access token with scopes ``tickets``,
``crm.objects.contacts.read`` and ``crm.objects.contacts.write``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class HubSpotError(Exception):
    def __init__(self, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass(frozen=True)
class TicketDraft:
    subject: str
    content: str
    priority: str  # HubSpot enum value: LOW / MEDIUM / HIGH
    category: str  # HubSpot hs_ticket_category value
    source_type: str  # EMAIL / FORM / CHAT / PHONE
    contact_email: str | None
    contact_firstname: str | None
    contact_lastname: str | None
    contact_phone: str | None
    contact_company: str | None


@dataclass(frozen=True)
class SyncResult:
    ticket_id: str
    contact_id: str | None


class HubSpotClient:
    def __init__(
        self,
        access_token: str,
        *,
        pipeline_id: str,
        pipeline_stage_id: str,
        base_url: str = "https://api.hubapi.com",
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._pipeline_id = pipeline_id
        self._stage_id = pipeline_stage_id
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, json: Any = None) -> dict[str, Any]:
        try:
            response = self._http.request(method, path, json=json)
        except httpx.TransportError as exc:
            raise HubSpotError(f"{method} {path}: {exc}", retryable=True) from exc
        if response.status_code >= 400:
            raise HubSpotError(
                f"{method} {path}: HTTP {response.status_code} {response.text[:500]}",
                retryable=response.status_code in RETRYABLE_STATUS,
                status=response.status_code,
            )
        return response.json() if response.content else {}

    def find_contact_id(self, email: str) -> str | None:
        data = self._request(
            "POST",
            "/crm/v3/objects/contacts/search",
            {
                "filterGroups": [
                    {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
                ],
                "properties": ["email"],
                "limit": 1,
            },
        )
        results = data.get("results") or []
        return str(results[0]["id"]) if results else None

    def upsert_contact(self, draft: TicketDraft) -> str | None:
        if not draft.contact_email:
            return None
        properties = {
            key: value
            for key, value in {
                "email": draft.contact_email,
                "firstname": draft.contact_firstname,
                "lastname": draft.contact_lastname,
                "phone": draft.contact_phone,
                "company": draft.contact_company,
            }.items()
            if value
        }
        existing = self.find_contact_id(draft.contact_email)
        if existing:
            # Only fill in; never overwrite what an agent already edited in HubSpot.
            return existing
        try:
            created = self._request("POST", "/crm/v3/objects/contacts", {"properties": properties})
        except HubSpotError as exc:
            # Created concurrently between search and create.
            if exc.status == 409:
                return self.find_contact_id(draft.contact_email)
            raise
        return str(created["id"])

    def create_ticket(self, draft: TicketDraft, contact_id: str | None) -> str:
        body: dict[str, Any] = {
            "properties": {
                "subject": draft.subject[:250],
                "content": draft.content,
                "hs_pipeline": self._pipeline_id,
                "hs_pipeline_stage": self._stage_id,
                "hs_ticket_priority": draft.priority,
                "hs_ticket_category": draft.category,
                "source_type": draft.source_type,
            }
        }
        if contact_id:
            body["associations"] = [
                {
                    "to": {"id": contact_id},
                    # HubSpot-defined association type 16 = ticket -> contact.
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 16}],
                }
            ]
        created = self._request("POST", "/crm/v3/objects/tickets", body)
        return str(created["id"])

    def sync(self, draft: TicketDraft) -> SyncResult:
        contact_id = self.upsert_contact(draft)
        ticket_id = self.create_ticket(draft, contact_id)
        return SyncResult(ticket_id=ticket_id, contact_id=contact_id)


def split_name(full_name: str | None) -> tuple[str | None, str | None]:
    if not full_name or not full_name.strip():
        return None, None
    parts = full_name.strip().split(maxsplit=1)
    return parts[0], parts[1] if len(parts) > 1 else None
