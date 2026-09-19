import argparse
import getpass
import os
import sys
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from marketmind.auth import password_hasher
from marketmind.config import get_settings
from marketmind.db import make_engine
from marketmind.models import AdminSession, AdminUser, AuditEvent


def read_password() -> str:
    password = os.environ.get("MM_ADMIN_PASSWORD")
    if password is None:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Confirm password: "):
            raise ValueError("Passwords do not match")
    return password


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the single MarketMind administrator")
    subcommands = parser.add_subparsers(dest="command", required=True)
    create = subcommands.add_parser("create-admin")
    create.add_argument("--username", default="admin")
    subcommands.add_parser("change-password")
    args = parser.parse_args()
    if args.command == "create-admin" and (
        not args.username.strip() or len(args.username) > 100 or args.username != args.username.strip()
    ):
        parser.error("Username must be nonblank, at most 100 characters, without surrounding whitespace")
    engine = None
    try:
        password = read_password()
        engine = make_engine(get_settings())
        with Session(engine) as db, db.begin():
            user = db.scalar(select(AdminUser).with_for_update())
            if args.command == "create-admin":
                if user is not None:
                    raise ValueError("An administrator already exists; use change-password")
                user = AdminUser(username=args.username, password_hash=password_hasher.hash(password))
                db.add(user)
                db.flush()
                action = "admin.created"
            else:
                if user is None:
                    raise ValueError("No administrator exists; use create-admin")
                user.password_hash = password_hasher.hash(password)
                db.execute(
                    update(AdminSession)
                    .where(
                        AdminSession.admin_id == user.id,
                        AdminSession.revoked_at.is_(None),
                    )
                    .values(revoked_at=datetime.now(UTC))
                )
                action = "admin.password_changed"
            db.add(
                AuditEvent(
                    admin_id=user.id,
                    action=action,
                    target_type="admin",
                    target_id=user.id,
                    outcome="succeeded",
                )
            )
        print(
            "Administrator created."
            if args.command == "create-admin"
            else "Password changed; all old sessions revoked."
        )
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except IntegrityError:
        print(
            "Administrator creation conflicted with the existing single administrator.",
            file=sys.stderr,
        )
        return 1
    except SQLAlchemyError:
        print(
            "Database operation failed; check database availability and migrations.",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
