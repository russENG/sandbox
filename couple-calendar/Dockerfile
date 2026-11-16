FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# Cloud Run は PORT 環境変数でポートを指定する
CMD exec uvicorn couple_calendar.line_app:app --host 0.0.0.0 --port ${PORT:-8080}
