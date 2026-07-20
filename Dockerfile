FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render/сервер задаёт PORT для health-эндпоинта; локально не обязателен.
CMD ["python", "watcher.py"]
