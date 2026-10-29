# kobo's image: the API from api/, the UI built into /srv/ui, and the tools the room's
# work needs (git, gh, kubectl, gcloud). Every input is pinned: base images by digest,
# Python packages by api/uv.lock (uv export --frozen writes requirements with hashes
# that pip verifies), UI packages by package-lock.json, kubectl by version, gh and
# gcloud by the versions their apt repos carry on the day the release is built (both
# repos are per-arch, so arm64 and amd64 each get their own). A bump is a line in a
# PR with the reason next to it (agora issue 58).
#
# Multi-arch: buildx sets TARGETARCH; a plain build uses the host's arch. The UI build
# and the lockfile export are arch-independent, so they run ONCE on the build host
# (--platform=$BUILDPLATFORM) and both images copy their output; under emulation they
# were most of a release's build time. BUILDPLATFORM needs BuildKit: buildx locally,
# the release workflow, and the dev path's buildx Cloud Build config (not `--tag`).

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

FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159 AS runtime
# buildx sets TARGETARCH per platform; a plain docker build gets the host's arch.
ARG TARGETARCH
ARG KUBECTL_VERSION=v1.34.1

RUN apt-get update && apt-get -y upgrade && apt-get install -y --no-install-recommends \
      git \
      curl \
      ca-certificates \
      gnupg \
    && rm -rf /var/lib/apt/lists/*

# gh from GitHub's apt repo (per-arch packages).
RUN install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
         -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=${TARGETARCH:-$(dpkg --print-architecture)} signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
         > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# kubectl by version, checksum verified.
RUN arch="${TARGETARCH:-$(dpkg --print-architecture)}" \
    && curl -fsSL -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" \
    && curl -fsSL -o /tmp/kubectl.sha256 \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl.sha256" \
    && echo "$(cat /tmp/kubectl.sha256)  /usr/local/bin/kubectl" | sha256sum -c - \
    && chmod +x /usr/local/bin/kubectl && rm /tmp/kubectl.sha256

# gcloud from Google's apt repo (per-arch packages).
RUN curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
      | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
    && echo "deb [arch=${TARGETARCH:-$(dpkg --print-architecture)} signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
         > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-cloud-cli \
    && rm -rf /var/lib/apt/lists/*

RUN git config --system credential.helper '!gh auth git-credential'

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
