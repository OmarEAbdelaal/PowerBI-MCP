FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
EXPOSE 8000
# Pass POWERBI_* settings with -e / --env-file. Protect the endpoint (HTTPS + auth) before exposing it.
CMD ["powerbi-mcp", "--http", "--host", "0.0.0.0", "--port", "8000"]
