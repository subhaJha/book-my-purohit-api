FROM python:3.13-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BOOKING_DB=/data/bookings.sqlite \
    BOOKING_SECURE_COOKIES=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 purohit \
    && useradd --uid 10001 --gid 10001 --no-create-home purohit \
    && install -d -o 10001 -g 10001 /data

COPY app ./app
USER 10001:10001
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\" --no-access-log"]
