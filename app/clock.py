from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return UTC at the precision of the database's DATETIME columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
