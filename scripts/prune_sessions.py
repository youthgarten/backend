"""Preview or prune a batch of expired refresh tokens."""

import argparse
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.clock import utc_now
from app.db import database_url, make_engine
from app.models import RefreshToken

RETENTION = timedelta(days=7)


def prune_sessions(session: Session, now: datetime, limit: int, *, apply: bool = False) -> int:
    expired_ids = session.scalars(
        select(RefreshToken.id).where(RefreshToken.expires_at < now - RETENTION)
        .order_by(RefreshToken.expires_at, RefreshToken.id)
        .limit(limit)
    ).all()
    if apply and expired_ids:
        result = session.execute(delete(RefreshToken).where(
            RefreshToken.id.in_(expired_ids), RefreshToken.expires_at < now - RETENTION
        ))
        return result.rowcount
    return len(expired_ids)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    if not 1 <= args.limit <= 10000:
        parser.error("--limit must be within 1 to 10000")
    engine = make_engine(database_url())
    try:
        with Session(engine) as session, session.begin():
            count = prune_sessions(session, utc_now(), args.limit, apply=args.apply)
        print(f"{'Removed' if args.apply else 'Eligible'} refresh tokens: {count}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
