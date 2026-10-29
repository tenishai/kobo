# kobo-base: the tools the room's work needs (git, gh, kubectl, gcloud) on the Python
# base, built on its own cadence and published as ghcr.io/tenishai/kobo-base. The
# product image FROMs it by digest, so a release build does only the Python and UI
# layers instead of re-installing these for both architectures every time.
#
# Built by .github/workflows/base-image.yml: on demand, and when this file changes.
# A bump is a line here with the reason next to it; the new digest then lands in the
# product Dockerfile's BASE_IMAGE default in its own PR.
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
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
