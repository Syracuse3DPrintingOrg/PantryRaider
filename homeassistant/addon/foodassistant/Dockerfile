# The add-on builds FROM the published Pantry Raider release whose number is
# the add-on's own version. The Supervisor passes that version as
# BUILD_VERSION, and the version in config.yaml is only ever set to a release
# that has been published, so the add-on always runs exactly the release its
# number names. An update offered in the minutes before that release's image
# is published fails to build (visibly, and it can be retried), instead of
# quietly installing the previous release under the new number.
ARG BUILD_VERSION
FROM ghcr.io/syracuse3dprintingorg/pantryraider:${BUILD_VERSION}

# HA-specific defaults layered on top of the published image:
#  - DATA_DIR=/data    persist settings/db in the add-on's data volume
#  - AUTH_REQUIRED=false  HA Ingress already authenticates the user, so the
#                         app's own login is off by default (no double prompt).
#                         If you map the optional LAN port, set a password in
#                         the setup wizard to re-enable app-level auth.
#  - PANTRY_RAIDER_ADDON=1  tells the app it runs as a Home Assistant add-on,
#                         so it trusts the sign-in Home Assistant already did
#                         for the sidebar page (Ingress) and nothing else.
ENV DATA_DIR=/data \
    AUTH_REQUIRED=false \
    PANTRY_RAIDER_ADDON=1

WORKDIR /app

# --workers 1: the app runs as one process, always (see service/Dockerfile).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
