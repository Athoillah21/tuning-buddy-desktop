FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Normalise line endings (the repo may be checked out on Windows) and bake static files into the image
RUN sed -i 's/\r$//' entrypoint.sh \
    && chmod +x entrypoint.sh \
    && SECRET_KEY=collectstatic-only python manage.py collectstatic --noinput \
    && useradd --create-home --uid 1000 appuser \
    && chown -R appuser /app

USER appuser

EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]
# One worker keeps the AI-status cache consistent; threads handle concurrent (long) analyses
CMD ["gunicorn", "tuning_buddy.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "900"]
