#!/bin/sh
# Container entrypoint: apply database migrations, then start the bot.
set -e
echo "Running database migrations..."
alembic upgrade head
echo "Starting bot..."
exec python -m app.main
