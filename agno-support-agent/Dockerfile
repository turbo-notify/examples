# A single stage: the image is small enough that a builder stage would add more
# Dockerfile than it saves in megabytes.
FROM python:3.13-slim

# Which model provider SDK gets installed, as a Poetry extra: `anthropic` or
# `openai`. Set `MODEL_PROVIDER` in `.env` to the SAME provider, since this
# build argument controls what is INSTALLED and `MODEL_PROVIDER` controls
# what the running agent USES.
ARG MODEL_EXTRA=anthropic

# Pinned rather than a floor: an unpinned `poetry` picks up whatever the
# latest release resolves dependencies with, on whatever day the image is
# built, which is exactly the kind of drift a Dockerfile exists to avoid.
ARG POETRY_VERSION=1.8.5

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    POETRY_VIRTUALENVS_CREATE=false

WORKDIR /app

RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"

# Dependencies first, so a code change does not re-resolve them.
COPY pyproject.toml poetry.lock* ./
RUN poetry install --no-root --no-interaction --without dev --extras "${MODEL_EXTRA}"

COPY src/ ./src/
COPY knowledge/ ./knowledge/
# `pyproject.toml` declares `readme = "README.md"`, so poetry refuses to install
# the project itself without it. Copied HERE and not beside pyproject.toml, or
# every edit to the README would invalidate the dependency layer above.
COPY README.md ./
RUN poetry install --only-root --no-interaction

ENV PYTHONPATH=/app/src
EXPOSE 8080

# Not run as root: this process handles requests from the open internet.
RUN useradd --create-home --uid 10001 attendant
USER attendant

CMD ["python", "-m", "support_agent"]
