FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS dependencies
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src \
    V360_RUNTIME_ROOT=/app/runtime \
    V360_QEMU=/usr/bin/qemu-aarch64 V360_WORKER_PATH=/app/runtime/native-worker
ARG APT_MIRROR=https://deb.debian.org
RUN sed -i "s|http://deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 update && apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 install -y --no-install-recommends ffmpeg qemu-user ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin v360
WORKDIR /app
COPY deploy/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.txt
COPY deploy/collect_runtime.py /tmp/collect_runtime.py
RUN python /tmp/collect_runtime.py

FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src \
    V360_RUNTIME_ROOT=/app/runtime V360_QEMU=/usr/bin/qemu-aarch64 \
    V360_WORKER_PATH=/app/runtime/native-worker
WORKDIR /app
COPY --from=dependencies /opt/runtime-root/ /
COPY --from=dependencies /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin v360
COPY src /app/src
COPY tools/run_native_worker.py /app/tools/run_native_worker.py
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="v360-web" \
      org.opencontainers.image.source="https://github.com/yungzzi/v360-web" \
      org.opencontainers.image.revision=$VCS_REF
ENV V360_REVISION=$VCS_REF
USER 1000:1000
EXPOSE 8080
ENTRYPOINT ["python", "-m", "v360_web.production"]
