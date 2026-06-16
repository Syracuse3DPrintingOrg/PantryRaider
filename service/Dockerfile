FROM python:3.12-slim

WORKDIR /app

# rclone powers the optional "push backup to a cloud remote" feature
# (Settings > Security > Backup). ca-certificates is needed for outbound HTTPS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends rclone ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN mkdir -p /app/data

# Production default: no --reload, code is baked into the image. The dev
# docker-compose.yml overrides this command to add --reload alongside a
# volume mount of ./service/app so `git pull` applies changes live.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
