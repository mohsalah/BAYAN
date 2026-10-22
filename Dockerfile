FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY brilliant_research_agent ./brilliant_research_agent
RUN pip install --no-cache-dir .
ENV BRA_DATABASE_PATH=/data/app.sqlite
EXPOSE 8000
CMD ["sh", "-c", "uvicorn brilliant_research_agent.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
