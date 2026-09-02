FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

EXPOSE 8000

# Applies pending Alembic migrations, then exec's the CMD below. Render
# runs no migration step of its own, so without this a deploy that adds a
# table/column starts the app against a schema that doesn't have it yet.
ENTRYPOINT ["./docker-entrypoint.sh"]

# --proxy-headers/--forwarded-allow-ips: Render (like most PaaS) sits the
# app behind a proxy, so request.client.host is otherwise the proxy's own
# address for every request, not the real client - which would make
# IP-based rate limiting bucket all traffic together instead of per-client.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
