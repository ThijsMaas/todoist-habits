FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/data/habits.db

WORKDIR /app
COPY pyproject.toml ./
COPY habits ./habits
RUN pip install --no-cache-dir .

VOLUME ["/data"]
CMD ["todoist-habits", "run"]
