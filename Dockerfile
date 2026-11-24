# アプリ本体（サーバ）。サンドボックスは別イメージ（sandbox/）で、ホストの Docker から実行ごとに起動する。
FROM python:3.12-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends docker.io sqlite3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY engine engine
COPY server server
COPY web web
COPY tasks tasks
COPY materials materials
COPY prompts prompts
COPY instruments instruments
COPY server_config.json .
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=4)"
CMD ["python", "-m", "server", "--config", "server_config.json", "--data", "/app/data", "--host", "0.0.0.0", "--port", "8000"]
