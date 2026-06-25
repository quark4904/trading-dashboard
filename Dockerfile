FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1
ENV TRADING_DASHBOARD_DB_PATH=/data/trading_dashboard.db

WORKDIR /app

COPY app ./app
COPY static ./static

RUN mkdir -p /data

EXPOSE 8765

CMD ["python", "-m", "app.main", "--host", "0.0.0.0", "--port", "8765"]
