FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir "mcp<2" "uvicorn[standard]" starlette httpx bcrypt croniter

COPY server.py ./

EXPOSE 8000

CMD ["python3", "server.py"]
