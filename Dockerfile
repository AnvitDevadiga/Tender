FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY dashboard ./dashboard
COPY assets/favicon.svg ./assets/favicon.svg
RUN pip install --no-cache-dir . && rm -rf /app/src /app/dashboard /app/assets
EXPOSE 8000
CMD ["uvicorn", "tender.api:app", "--host", "0.0.0.0", "--port", "8000"]
