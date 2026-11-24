# C 実行サンドボックス（仕様 §1, §6, §14）。
# ASan/UBSan は glibc 前提なので Debian 系を使う（Alpine/musl では動かない）。
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev python3 \
 && rm -rf /var/lib/apt/lists/*
COPY runner.py /opt/runner.py
# 実行は nobody。コンテナは --read-only / --network none / --cap-drop ALL で起動する（server/sandbox.py）。
USER nobody
WORKDIR /tmp
ENTRYPOINT ["python3", "/opt/runner.py"]
