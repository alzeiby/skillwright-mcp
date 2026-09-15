# syntax=docker/dockerfile:1

FROM node:22-bookworm-slim AS node
FROM ghcr.io/astral-sh/uv:0.11.21 AS uv

FROM python:3.12-slim-bookworm AS runtime

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY --from=uv /uv /uvx /usr/local/bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/skillwright \
    PATH=/opt/skillwright/bin:$PATH \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    SKILLWRIGHT_PLAYWRIGHT_COMMAND=/usr/local/bin/skillwright-playwright-mcp \
    SKILLWRIGHT_PLAYWRIGHT_PACKAGE=@playwright/mcp@0.0.81 \
    SKILLWRIGHT_PLAYWRIGHT_OUTPUT_DIR=/var/lib/skillwright/playwright-output \
    SKILLWRIGHT_API_HOST=0.0.0.0 \
    SKILLWRIGHT_ALLOW_UNAUTHENTICATED_LOCAL=false

# Install the exact MCP package and its matching Chromium during the image build.
# npm/npx are removed afterwards so browser startup cannot fetch packages at runtime.
RUN npm install --global --omit=dev @playwright/mcp@0.0.81 \
    && test "$(node -p "require('/usr/local/lib/node_modules/@playwright/mcp/package.json').version")" = "0.0.81" \
    && test -f /usr/local/lib/node_modules/@playwright/mcp/node_modules/playwright/cli.js \
    && node /usr/local/lib/node_modules/@playwright/mcp/node_modules/playwright/cli.js install --with-deps chromium \
    && browser_executable="$(find /ms-playwright -path '*/chrome-linux64/chrome' -type f -print -quit)" \
    && test -n "$browser_executable" \
    && test -x "$browser_executable" \
    && ln -s "$browser_executable" /usr/local/bin/skillwright-chromium \
    && rm -rf /root/.npm /usr/local/lib/node_modules/npm /var/lib/apt/lists/* \
    && rm -f /usr/local/bin/npm /usr/local/bin/npx

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts/container-entrypoint.sh /usr/local/bin/skillwright-container-entrypoint
COPY scripts/playwright-mcp-offline.sh /usr/local/bin/skillwright-playwright-mcp

RUN chmod 0755 /usr/local/bin/skillwright-container-entrypoint /usr/local/bin/skillwright-playwright-mcp \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin skillwright \
    && mkdir -p /var/lib/skillwright/playwright-output \
    && chown -R skillwright:skillwright /var/lib/skillwright /home/skillwright \
    && chmod -R a+rX /ms-playwright

EXPOSE 8766 8767

USER 10001:10001
ENTRYPOINT ["/usr/local/bin/skillwright-container-entrypoint"]
CMD ["api"]
