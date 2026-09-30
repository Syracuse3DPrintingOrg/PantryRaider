# The base is pinned by digest so a rebuild of the same commit starts from the
# same bytes. The tag stays in front of the digest so Dependabot can see which
# line this tracks and propose the next digest as a reviewable change.
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

WORKDIR /app

# ca-certificates for outbound HTTPS (Stripe, the upstream AI provider).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# requirements.txt is the hand-edited pin list; requirements.lock is its full
# transitive resolution with a hash per wheel, and it is what the image
# installs, so every build resolves the same versions and a wheel that changed
# underneath its version number fails the build instead of shipping. Regenerate
# the lock whenever requirements.txt changes:
#   uv pip compile cloud/requirements.txt --universal --python-version 3.12 \
#     --generate-hashes -o cloud/requirements.lock
# cloud/tests/test_requirements_lock.py catches a forgotten one.
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY app/ ./app/

# Alembic config and migration scripts, so both startup (database.init_db) and
# the `alembic` CLI inside the container resolve the same versioned schema.
COPY alembic.ini ./alembic.ini
COPY migrations/ ./migrations/

# The app never binds a public port; Caddy in front terminates TLS.
# --proxy-headers so request.client.host reflects the real client from
# Caddy's X-Forwarded-For (the app is only reachable through Caddy on the
# internal network, so trusting the forwarded header is safe). The rate
# limiter also reads the header directly via deps.client_ip; this keeps the
# ASGI-level client correct for logs and any other consumer.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
