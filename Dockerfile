FROM python:3.12-slim

# psql is used once, by `safesql setup-db --load`, to restore the Spider fixture.
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY evals ./evals

RUN useradd --create-home --uid 10001 app
USER app

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"
CMD ["safesql", "serve", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
