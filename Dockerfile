# syntax=docker/dockerfile:1
#
# Kobo — the agent assembly room, UI + API in one deployable.
#
# Two stages:
#   1. ui-build  — build the Svelte UI's static assets.
#   2. runtime   — install the API and the CLIs it shells out to
#                  (git, gh, kubectl, gcloud), copy in the built UI, run
#                  as a non-root user.
#
# The image bakes NO config and NO secrets. Configuration arrives at
# container start via a mounted file referenced by the KOBO_CONFIG env
# var; that file is expected to set ui_dist: /srv/ui to serve the UI
# built here alongside the API.

# ---------------------------------------------------------------------------
# Stage 1: build the UI
# ---------------------------------------------------------------------------
FROM node:22-slim AS ui-build

WORKDIR /ui

# Install with the lockfile first so this layer only invalidates when
# dependencies change, not on every source edit.
COPY ui/package.json ui/package-lock.json ./
RUN npm ci

COPY ui/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: the API runtime, plus the CLIs it shells out to
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# git      — the room clones/commits against agent and org-config repos.
# curl     — fetches the kubectl binary and CLI signing keys below.
# ca-certificates, gnupg — TLS + apt key verification for the gh/gcloud repos.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git \
      curl \
      ca-certificates \
      gnupg \
    && rm -rf /var/lib/apt/lists/*

# gh (GitHub CLI) — official apt repo. kobo shells `gh`/`git` with GH_TOKEN
# in the subprocess env when authenticated as a GitHub App (Task 3), or via
# the operator's ambient `gh auth` session otherwise.
RUN install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
         -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
         > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# kubectl — stable channel binary, matched to this image's own arch.
RUN curl -fsSL -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/$(dpkg --print-architecture)/kubectl" \
    && chmod +x /usr/local/bin/kubectl

# Google Cloud SDK CLI (gcloud) — official apt repo. Used for the token
# seam's CLI fallback and group-membership operations.
RUN curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
      | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
         > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-cloud-cli \
    && rm -rf /var/lib/apt/lists/*

# Bake the credential helper so a plain `git` invocation picks up GH_TOKEN
# from `gh` — no per-call credential wiring needed by the app.
RUN git config --system credential.helper '!gh auth git-credential'

WORKDIR /app

# Install the API. Copy only what the build needs (pyproject + package),
# not tests/, the Makefile, or any config file — those never belong in
# the image.
COPY api/pyproject.toml ./api/pyproject.toml
COPY api/kobo_api ./api/kobo_api
RUN pip install --no-cache-dir ./api

# The built UI. The mounted runtime config points ui_dist at this path.
COPY --from=ui-build /ui/dist /srv/ui

# Run as a non-root user.
RUN groupadd --system kobo && useradd --system --gid kobo --home-dir /app --shell /usr/sbin/nologin kobo \
    && chown -R kobo:kobo /app /srv/ui
USER kobo

ENV PORT=8080
EXPOSE 8080

ENTRYPOINT ["uvicorn", "--factory", "kobo_api.main:build_default_app", "--host", "0.0.0.0", "--port", "8080"]
