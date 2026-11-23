# kobo's image: the API from api/, the UI built into /srv/ui, on kobo-base, which
# carries the tools the room's work needs (git, gh, kubectl, gcloud; see
# docker/base/Dockerfile). Every input is pinned: base images by digest, Python
# packages by api/uv.lock (uv export --frozen writes requirements with hashes that pip
# verifies), UI packages by package-lock.json. A bump is a line in a PR with the reason
# next to it (agora issue 58).
#
# Multi-arch: buildx sets TARGETARCH; a plain build uses the host's arch. The UI build
# and the lockfile export are arch-independent, so they run ONCE on the build host
# (--platform=$BUILDPLATFORM) and both images copy their output; under emulation they
# were most of a release's build time. BUILDPLATFORM needs BuildKit: buildx locally,
# the release workflow, and the dev path's buildx Cloud Build config (not `--tag`).

# kobo-base (docker/base/Dockerfile): the Python base with git, gh, kubectl and gcloud
# already installed, published by base-image.yml and pinned here by digest; the runtime
# stage below builds on it, so a release does only the Python and UI layers. Declared
# before the first FROM, as BuildKit requires of an ARG a FROM reads. The dev path passes
# its own registry's copy of the same digest.
ARG BASE_IMAGE=ghcr.io/tenishai/kobo-base:20261029-dbbf463@sha256:d6dc96c8e7e241ee1b8472a1cd2e0c66b98ec6e4e6d7e25b3e10a2b903c8479e

FROM --platform=$BUILDPLATFORM node:22-slim@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da AS ui-build
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

FROM --platform=$BUILDPLATFORM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159 AS deps
ARG UV_VERSION=0.11.21
WORKDIR /lock
COPY api/pyproject.toml api/uv.lock ./
RUN pip install --no-cache-dir "uv==${UV_VERSION}" \
 && uv export --frozen --no-dev --no-emit-project -o requirements.txt

# The runtime stage builds on kobo-base (BASE_IMAGE, declared at the top of this file
# because an ARG a FROM reads must come before the first FROM).
FROM ${BASE_IMAGE} AS runtime

# The base is built on its own cadence, so its OS packages are as old as its last build.
# A release passes the day here and this layer runs again; without the argument the layer
# is cached like any other.
ARG APT_REFRESH=
RUN echo "apt refresh: ${APT_REFRESH}" \
 && apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=deps /lock/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY api/pyproject.toml ./api/pyproject.toml
COPY api/kobo_api ./api/kobo_api
RUN pip install --no-cache-dir --no-deps ./api
COPY api/alembic.ini ./api/alembic.ini
COPY api/migrations ./api/migrations

COPY docker/entrypoint.sh /usr/local/bin/kobo-entrypoint
RUN chmod +x /usr/local/bin/kobo-entrypoint

COPY --from=ui-build /ui/dist /srv/ui

RUN groupadd --system --gid 1001 kobo \
    && useradd --system --uid 1001 --gid kobo --home-dir /app --shell /usr/sbin/nologin kobo \
    && chown -R 1001:1001 /app /srv/ui
USER 1001

ENV PORT=8080
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/kobo-entrypoint"]
