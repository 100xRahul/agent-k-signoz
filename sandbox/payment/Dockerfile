FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install uv && uv pip install --system -e .
COPY . .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8003"]
