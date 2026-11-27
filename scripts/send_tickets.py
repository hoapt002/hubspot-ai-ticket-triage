"""Post tickets to a running instance with a valid signature.

Used for smoke tests after deploying and for replaying exported tickets:

    uv run python scripts/send_tickets.py --url http://localhost:8000 \
        --source website-contact-form tickets.json

The file holds one ticket object or a list of them in the generic webhook format.
The source's secret is read from WEBHOOK_SECRETS (.env).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from ticket_triage.settings import get_settings
from ticket_triage.webhooks.signature import HEADER, sign


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("file", type=Path)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--source", required=True)
    args = parser.parse_args()

    secret = get_settings().webhook_secrets.get(args.source)
    if secret is None:
        print(f"no WEBHOOK_SECRETS entry for {args.source!r}", file=sys.stderr)
        return 2
    data = json.loads(args.file.read_text(encoding="utf-8"))
    tickets = data if isinstance(data, list) else [data]
    failures = 0
    with httpx.Client(base_url=args.url, timeout=30) as client:
        for ticket in tickets:
            body = json.dumps(ticket, ensure_ascii=False).encode()
            response = client.post(
                f"/webhooks/tickets/{args.source}",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    HEADER: sign(secret.get_secret_value(), body),
                },
            )
            ok = response.status_code in (200, 202)
            failures += not ok
            print(f"{ticket.get('id')}: {response.status_code} {response.text}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
