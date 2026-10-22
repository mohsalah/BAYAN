FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY bayan ./bayan
RUN pip install --no-cache-dir .
ENV BAYAN_DATABASE_PATH=/data/app.sqlite \
    BAYAN_COOKIE_SECURE=true
EXPOSE 8000
CMD ["sh", "-c", "uvicorn bayan.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
