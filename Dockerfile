FROM python:3.14-slim

WORKDIR /srv/backend
ENV PYTHONUNBUFFERED=1 UV_PYTHON_DOWNLOADS=never PATH=/srv/backend/.venv/bin:$PATH

RUN python -m pip install --no-cache-dir uv==0.12.12
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts
COPY main.py alembic.ini ./
RUN useradd --system --no-create-home backend
USER backend
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
