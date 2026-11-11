FROM python:3.12-slim
WORKDIR /app
COPY services/requirements.txt services/requirements.txt
RUN pip install --no-cache-dir -r services/requirements.txt
COPY services /app/services
COPY scripts /app/scripts
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
