FROM python:3.12-alpine

# Extractors: unzip, GNU tar (+ xz/bzip2 support) and 7-Zip (handles .7z and .rar).
# (Alpine no longer ships a separate unrar package.)
RUN apk add --no-cache unzip tar xz bzip2 tzdata 7zip \
 && (command -v 7zz || command -v 7z) && unzip -v | head -1 && tar --version | head -1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

ENV DATA_DIR=/data \
    PORT=8080 \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD wget -qO- "http://127.0.0.1:${PORT}/health" >/dev/null || exit 1

CMD ["sh", "-c", "exec waitress-serve --listen=0.0.0.0:${PORT} --threads=8 main:app"]
