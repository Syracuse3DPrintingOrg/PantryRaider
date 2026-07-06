FROM python:3.12-slim

WORKDIR /app

# ca-certificates for outbound HTTPS (Stripe, the upstream AI provider).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# The app never binds a public port; Caddy in front terminates TLS.
# --proxy-headers so request.client.host reflects the real client from
# Caddy's X-Forwarded-For (the app is only reachable through Caddy on the
# internal network, so trusting the forwarded header is safe). The rate
# limiter also reads the header directly via deps.client_ip; this keeps the
# ASGI-level client correct for logs and any other consumer.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
