#!/bin/sh
# Container entrypoint: apply database migrations, then start the bot.
set -e
echo "Running database migrations..."
alembic upgrade head 2>&1  # alembic logs to stderr: keep its INFO lines out of "error"
echo "Starting bot..."
exec python -m app.main
