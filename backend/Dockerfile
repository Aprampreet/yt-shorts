FROM node:22-bookworm-slim AS node-runtime
FROM debian:bookworm-slim AS whisper-runtime
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates cmake curl g++ git make && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch v1.9.5 --recurse-submodules https://github.com/ggml-org/whisper.cpp.git /src/whisper.cpp \
    && cmake -S /src/whisper.cpp -B /src/whisper.cpp/build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF \
    && cmake --build /src/whisper.cpp/build --target whisper-cli -j 2
RUN mkdir -p /models && curl -L --fail --retry 2 -o /models/ggml-base-q5_1.bin \
    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base-q5_1.bin \
    && echo 'a3733eda680ef76256db5fc5dd9de8629e62c5e7  /models/ggml-base-q5_1.bin' | sha1sum -c -
FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LOCAL_WHISPER_MODEL=/opt/models/ggml-base-q5_1.bin
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=whisper-runtime /src/whisper.cpp/build/bin/whisper-cli /usr/local/bin/whisper-cli
COPY --from=whisper-runtime /models/ggml-base-q5_1.bin /opt/models/ggml-base-q5_1.bin
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fonts-noto-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
RUN useradd --create-home --uid 10001 studio && mkdir -p /data/media && chown -R studio:studio /app /data
COPY --chown=studio:studio . .
USER studio
ENV MEDIA_ROOT=/data/media DATABASE_URL=sqlite:////data/db.sqlite3
CMD ["gunicorn", "backend.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120", "--access-logfile", "-"]
