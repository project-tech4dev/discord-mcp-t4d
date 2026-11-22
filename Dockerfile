# discord-mcp server image. Built on the instance by deploy/setup-instance.sh.
# Only the package and its pinned dependencies go in; .dockerignore keeps
# everything else (secrets, local config, tests, git) out of the build context.
FROM python:3.12.16-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONWARNINGS=ignore \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt

COPY discord_mcp ./discord_mcp
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
# The checkout's modes depend on the umask it was written with; make the
# code readable (not writable) by the app whatever they were.
RUN chmod -R a+rX,go-w /app \
    && chmod 0555 /usr/local/bin/entrypoint.sh \
    && useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin app

USER 10001:10001
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
