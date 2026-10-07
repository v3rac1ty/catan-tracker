FROM python:3.12-slim

# MPLCONFIGDIR: the runtime root filesystem is read-only and matplotlib needs a
# writable config/cache directory at import time; /tmp is a tmpfs at runtime.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

RUN useradd --create-home --no-log-init --shell /usr/sbin/nologin appuser
USER appuser

CMD ["python", "-m", "catan_bot"]
