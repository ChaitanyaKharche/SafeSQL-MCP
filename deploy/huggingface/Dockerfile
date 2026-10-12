# Hugging Face Space: Postgres 17 and SafeSQL in one container.
#
# The Space repo only needs this file and README.md. The build clones the public GitHub
# repo, so "Factory rebuild" in the Space settings picks up the latest main. The Spider
# data and fixtures are loaded at build time; the server only ever gets the read-only
# role, whose password is generated during the build.
FROM postgres:17-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Spaces run the container as uid 1000.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/venv/bin:/usr/lib/postgresql/17/bin:$PATH \
    PGDATA=/home/user/pgdata \
    PGSOCKETS=/home/user/run \
    PYTHONUNBUFFERED=1
WORKDIR /home/user

ARG REPO=https://github.com/ChaitanyaKharche/SafeSQL-MCP.git
ARG REF=main
RUN git clone --depth 1 --branch "$REF" "$REPO" app \
 && python3 -m venv venv \
 && venv/bin/pip install --no-cache-dir ./app

# Bake the database into the image. Admin access is over the local socket only; the
# server connects over TCP as the read-only role.
RUN mkdir -p "$PGSOCKETS" \
 && python3 -c "import secrets; print(secrets.token_urlsafe(24))" > .reader_pw \
 && initdb -U postgres --auth-local=trust --auth-host=scram-sha-256 -E UTF8 --locale=C >/dev/null \
 && pg_ctl -o "-c listen_addresses='' -k $PGSOCKETS" -w start >/dev/null \
 && cd app \
 && SAFESQL_ADMIN_URL="postgresql://postgres@/safesql?host=$PGSOCKETS" SAFESQL_READER_PASSWORD="$(cat ../.reader_pw)" \
    safesql setup-db --load evals/fixtures/spider_dev.sql.gz \
 && pg_ctl -m fast -w stop >/dev/null

EXPOSE 7860
ENTRYPOINT []
CMD ["sh", "/home/user/app/deploy/huggingface/start.sh"]
