FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && useradd --create-home --uid 10001 appuser

COPY app ./app
USER appuser

EXPOSE 8000
# 監聽平台給的 $PORT(Render / Cloud Run 都會注入);用 exec 讓 uvicorn 成為 PID 1 收到 SIGTERM
CMD ["sh", "-c", "exec uvicorn --factory app.main:create_app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
