#!/usr/bin/env bash
set -e; cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate; pip install -q -r requirements.txt
[ -f .env ] || python manage.py init
exec python -m uvicorn main:app --host 0.0.0.0 --port 8000
