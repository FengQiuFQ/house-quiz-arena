FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts
COPY alembic.ini .

# 生产：uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1（§17.1）
# 数据库迁移由独立部署步骤执行，不在应用启动时自动迁移。
# 注意：**必须 --workers 1**。实时连接 Hub、限流计数、在途提交计数与比赛控制器都在进程内，
# 多 worker 会导致进度广播丢失与状态不一致；app 启动时也会主动拒绝 workers>1。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
