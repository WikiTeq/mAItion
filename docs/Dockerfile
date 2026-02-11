FROM node:24-slim

ARG TZ
ARG TARGETOS
ARG TARGETARCH
ENV TZ="$TZ"

RUN apt-get update && apt-get install -y --no-install-recommends \
  curl \
  git \
  jq \
  && rm -rf /var/lib/apt/lists/*

# Ensure default node user has access to /usr/local/share
RUN mkdir -p /usr/local/share/npm-global && \
  	chown -R node:node /usr/local/share

RUN chown -R node:node /usr/local/share/npm-global

ARG USERNAME=node
# Set up non-root user
USER node

# Install global packages
ENV NPM_CONFIG_PREFIX=/usr/local/share/npm-global
ENV PATH=$PATH:/usr/local/share/npm-global/bin

# Install codex
RUN npm install -g mint

RUN npm cache clean --force

RUN echo 'export PATH="/usr/local/share/npm-global/bin:$PATH"' >> ~/.profile
