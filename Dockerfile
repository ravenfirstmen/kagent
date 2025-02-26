FROM node:18-alpine AS ui-builder

WORKDIR /app/ui

COPY ui/package*.json ./
RUN npm ci

COPY ui/ .
RUN npm run build

FROM ghcr.io/astral-sh/uv:debian-slim AS final

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_SYSTEM_PYTHON=1 \
    PATH="/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/sbin:/bin:/root/.cargo/bin"

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    curl \
    ca-certificates \
    nginx \
    supervisor \
    gnupg \
    python3 \
    python3-pip \
    python3-venv \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_18.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y nodejs \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python

RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    uv --version

# Install kubectl
RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/

# Create groups and users
RUN groupadd -g 1001 nodejs \
    && groupadd -g 1002 pythongroup \
    && useradd -u 1001 -g nodejs -s /bin/bash -m nextjs \
    && useradd -u 1002 -g pythongroup -s /bin/bash -m python \
    && mkdir -p /app/python /app/ui /run/nginx \
    && chown -R python:pythongroup /app/python \
    && chown -R nextjs:nodejs /app/ui

# Set up Python backend
WORKDIR /app/python
COPY python/pyproject.toml .
COPY python/.python-version .
COPY python/uv.lock .
COPY python/src src
COPY python/README.md .
RUN uv sync --all-extras && \
    chown -R python:pythongroup /app/python

# Create the tools.json file, 
# create the ~/.autogenstudio/configs folder and copy the tools.json file there
RUN uv run tool_gen && \
    mkdir -p $HOME/.autogenstudio/configs && \
    cp tools.json $HOME/.autogenstudio/configs/tools.json

# Set up Next.js UI
WORKDIR /app/ui
COPY --from=ui-builder /app/ui/next.config.ts ./
COPY --from=ui-builder /app/ui/public ./public
COPY --from=ui-builder /app/ui/package.json ./package.json
COPY --from=ui-builder --chown=nextjs:nodejs /app/ui/.next/standalone ./
COPY --from=ui-builder --chown=nextjs:nodejs /app/ui/.next/static ./.next/static

# Set up Nginx and Supervisor
WORKDIR /app
COPY conf/nginx.conf /etc/nginx/nginx.conf
COPY conf/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Ensure correct permissions
RUN chown -R nextjs:nodejs /app/ui && \
    chown -R python:pythongroup /app/python && \
    chmod -R 755 /app

RUN mkdir -p /app/python/.cache/uv && \
    chown -R python:pythongroup /app/python/.cache

EXPOSE 80

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]