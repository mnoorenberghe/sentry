FROM python:3.8.18-slim-bullseye as sdist

LABEL maintainer="oss@sentry.io"
LABEL org.opencontainers.image.title="Sentry Wheel Builder"
LABEL org.opencontainers.image.description="Python Wheel Builder for Sentry"
LABEL org.opencontainers.image.url="https://sentry.io/"
LABEL org.opencontainers.image.vendor="Functional Software, Inc."
LABEL org.opencontainers.image.authors="oss@sentry.io"

# Sane defaults for pip
ENV PIP_NO_CACHE_DIR=1 \
  PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
  # Needed for fetching stuff
  wget \
  && rm -rf /var/lib/apt/lists/*

# Get and set up Node for front-end asset building
ENV VOLTA_VERSION=1.1.0 \
  VOLTA_HOME=/.volta \
  PATH=/.volta/bin:$PATH

RUN wget "https://github.com/volta-cli/volta/releases/download/v$VOLTA_VERSION/volta-$VOLTA_VERSION-linux.tar.gz" \
  && tar -xzf "volta-$VOLTA_VERSION-linux.tar.gz" -C /usr/local/bin \
  # Running `volta -v` triggers setting up the shims in VOLTA_HOME (otherwise node won't work)
  && volta -v \
  && rm "volta-$VOLTA_VERSION-linux.tar.gz"

WORKDIR /js

COPY .volta.json package.json
# Running `node -v` and `yarn -v` triggers Volta to install the versions set in the project
RUN node -v && yarn -v

COPY .volta.json package.json yarn.lock .
RUN export YARN_CACHE_FOLDER="$(mktemp -d)" \
  && yarn install --frozen-lockfile --production --quiet \
  && rm -r "$YARN_CACHE_FOLDER"

WORKDIR /workspace
VOLUME ["/workspace/node_modules", "/workspace/build"]
COPY self-hosted/builder.sh /builder.sh
ENTRYPOINT [ "/builder.sh" ]

ARG SOURCE_COMMIT
ENV SENTRY_BUILD=${SOURCE_COMMIT:-unknown}
LABEL org.opencontainers.image.revision=$SOURCE_COMMIT
LABEL org.opencontainers.image.source="https://github.com/getsentry/sentry/tree/${SOURCE_COMMIT:-master}/"
LABEL org.opencontainers.image.licenses="https://github.com/getsentry/sentry/blob/${SOURCE_COMMIT:-master}/LICENSE"
