FROM python:3.12-slim

# Без .pyc, логи сразу в stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

WORKDIR /app

# Сначала зависимости — слой кэшируется между сборками
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Затем код проекта
COPY . .

# Веб-интерфейс бота
ENV WEB_HOST=0.0.0.0 \
    WEB_PORT=3000
EXPOSE 3000

# Health-check для хостинга
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3000/health',timeout=4).status==200 else 1)"

# Главный файл: поднимает и HTTP-сервер, и long polling
CMD ["python", "bot.py"]
