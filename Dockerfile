FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY . /app

EXPOSE 5000

# По умолчанию запускаем web, для бота команда переопределяется в docker-compose.
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "app:app"]
