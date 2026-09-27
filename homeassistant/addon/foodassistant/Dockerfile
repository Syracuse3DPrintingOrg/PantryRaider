ARG BUILD_FROM
FROM ${BUILD_FROM}

# HA-specific defaults layered on top of the published image:
#  - DATA_DIR=/data    persist settings/db in the add-on's data volume
#  - AUTH_REQUIRED=false  HA Ingress already authenticates the user, so the
#                         app's own login is off by default (no double prompt).
#                         If you map the optional LAN port, set a password in
#                         the setup wizard to re-enable app-level auth.
ENV DATA_DIR=/data \
    AUTH_REQUIRED=false

WORKDIR /app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
