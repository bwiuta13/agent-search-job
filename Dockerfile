# syntax=docker/dockerfile:1

FROM python:3.13-slim

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" app \
    && useradd --create-home --uid "${APP_UID}" --gid "${APP_GID}" app

# Cài thư viện + Chromium chỉ từ pyproject.toml (package tạm rỗng), chưa chép code: sửa code/static (widget thỏ,
# character.glb, Three.js...) không làm mất cache lớp này nên build lại không phải tải lại Chromium. Cài editable
# (trỏ vào /app/src) nên code thật chép vào ở bước COPY . . bên dưới được dùng ngay.
COPY pyproject.toml README.md ./
RUN mkdir -p src/agent_search_job \
    && touch src/agent_search_job/__init__.py \
    && python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --editable . \
    && python -m playwright install --with-deps chromium

COPY --chown=app:app . .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN mkdir -p /app/data /app/backups \
    && chown -R app:app /app /ms-playwright \
    && chmod 0755 /usr/local/bin/docker-entrypoint.sh

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3).read()"]

ENTRYPOINT ["docker-entrypoint.sh"]

CMD ["python", "scripts/serve.py", "--db", "data/jobs.db", "--host", "0.0.0.0", "--port", "8765"]
