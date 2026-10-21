FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && groupadd -g 10001 iacp && useradd -u 10001 -g iacp -M iacp
COPY iacp ./iacp
COPY alembic.ini .
COPY alembic ./alembic
# Normalizar el checkout sin conceder escritura al usuario de ejecución.
RUN chown -R root:root /app \
    && find /app -type d -exec chmod 0755 {} + \
    && find /app -type f -exec chmod 0644 {} +
USER iacp
EXPOSE 8000
CMD ["uvicorn", "iacp.main:app", "--host", "0.0.0.0", "--port", "8000"]
