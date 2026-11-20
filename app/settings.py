import os
from pathlib import Path

from dotenv import dotenv_values

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def setting(name: str) -> str | None:
    return os.environ.get(name) or dotenv_values(ENV_FILE).get(name)
