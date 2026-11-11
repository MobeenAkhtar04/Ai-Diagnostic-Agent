FROM python:3.12-slim
WORKDIR /app
COPY infra/exporter/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY infra/exporter/stack_exporter.py .
CMD ["python", "stack_exporter.py"]
