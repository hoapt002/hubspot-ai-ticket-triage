"""Inbound payload formats, normalised into one shape before storage."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field

MAX_BODY_CHARS = 50_000


class NormalizedTicket(BaseModel):
    external_id: str
    channel: str
    subject: str
    body: str
    sender_email: str | None
    sender_name: str | None


class GenericCustomer(BaseModel):
    email: EmailStr | None = None
    name: str | None = Field(default=None, max_length=255)


class GenericTicketPayload(BaseModel):
    """Payload for ``POST /webhooks/tickets/{source}``."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1, max_length=255, description="Unique id in the source system")
    channel: str = Field(default="web_form", max_length=32)
    subject: str = Field(default="", max_length=1000)
    body: str = Field(min_length=1, max_length=MAX_BODY_CHARS)
    customer: GenericCustomer = Field(default_factory=GenericCustomer)

    def normalize(self) -> NormalizedTicket:
        return NormalizedTicket(
            external_id=self.id,
            channel=self.channel,
            subject=self.subject.strip(),
            body=self.body.strip(),
            sender_email=str(self.customer.email).lower() if self.customer.email else None,
            sender_name=self.customer.name,
        )


class PostmarkAddress(BaseModel):
    Email: str = ""
    Name: str = ""


class PostmarkInbound(BaseModel):
    """Subset of Postmark's inbound webhook JSON that we use."""

    model_config = ConfigDict(extra="allow")

    MessageID: str = Field(min_length=1)
    FromFull: PostmarkAddress = Field(default_factory=PostmarkAddress)
    From: str = ""
    FromName: str = ""
    Subject: str = ""
    TextBody: str = ""
    StrippedTextReply: str = ""
    HtmlBody: str = ""

    def normalize(self) -> NormalizedTicket:
        body = (self.StrippedTextReply or self.TextBody).strip()
        if not body and self.HtmlBody:
            body = _strip_html(self.HtmlBody)
        email = (self.FromFull.Email or self.From).strip().lower() or None
        return NormalizedTicket(
            external_id=self.MessageID,
            channel="email",
            subject=self.Subject.strip(),
            body=body[:MAX_BODY_CHARS] or "(empty message)",
            sender_email=email,
            sender_name=(self.FromFull.Name or self.FromName).strip() or None,
        )


def _strip_html(html: str) -> str:
    from html.parser import HTMLParser

    class _Text(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.parts: list[str] = []
            self._skip = 0

        def handle_starttag(self, tag: str, attrs: Any) -> None:
            if tag in {"script", "style"}:
                self._skip += 1
            elif tag in {"br", "p", "div", "li", "tr"}:
                self.parts.append("\n")

        def handle_endtag(self, tag: str) -> None:
            if tag in {"script", "style"} and self._skip:
                self._skip -= 1

        def handle_data(self, data: str) -> None:
            if not self._skip:
                self.parts.append(data)

    parser = _Text()
    parser.feed(html)
    lines = (line.strip() for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)
