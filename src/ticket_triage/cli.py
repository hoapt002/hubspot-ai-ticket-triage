"""Command line: ``triage serve | worker | migrate | create-user | sign``."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from pathlib import Path

from ticket_triage.settings import PROJECT_ROOT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="triage")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    worker = sub.add_parser("worker", help="run the background job worker")
    worker.add_argument("--concurrency", type=int, default=4, help="parallel job loops")
    sub.add_parser("migrate", help="apply database migrations")

    user = sub.add_parser("create-user", help="create a reviewer account")
    user.add_argument("--email", required=True)
    user.add_argument("--name", required=True)
    user.add_argument("--password-stdin", action="store_true", help="read the password from stdin")

    sign = sub.add_parser("sign", help="print a signature header for a payload file")
    sign.add_argument("source")
    sign.add_argument("file", type=Path)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "ticket_triage.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            proxy_headers=True,
        )
        return 0

    if args.command == "worker":
        from ticket_triage.db import get_sessionmaker
        from ticket_triage.settings import get_settings
        from ticket_triage.taxonomy import get_taxonomy
        from ticket_triage.worker import build_pipeline, run_workers

        settings = get_settings()
        pipeline = build_pipeline(settings, get_taxonomy())
        run_workers(pipeline, get_sessionmaker(), settings, max(1, args.concurrency))
        return 0

    if args.command == "migrate":
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")
        return 0

    if args.command == "create-user":
        from ticket_triage.auth import hash_password
        from ticket_triage.db import session_scope
        from ticket_triage.models import User

        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = getpass.getpass("Password: ")
        if len(password) < 12:
            print("password must be at least 12 characters", file=sys.stderr)
            return 2
        with session_scope() as session:
            session.add(
                User(
                    email=args.email.strip().lower(),
                    name=args.name,
                    password_hash=hash_password(password),
                )
            )
        print(f"created reviewer {args.email}")
        return 0

    if args.command == "sign":
        from ticket_triage.settings import get_settings
        from ticket_triage.webhooks.signature import HEADER
        from ticket_triage.webhooks.signature import sign as make_signature

        secret = get_settings().webhook_secrets.get(args.source)
        if secret is None:
            print(f"no WEBHOOK_SECRETS entry for {args.source!r}", file=sys.stderr)
            return 2
        body = args.file.read_bytes()
        json.loads(body)  # fail early on invalid JSON
        print(f"{HEADER}: {make_signature(secret.get_secret_value(), body)}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
