# One worker, not three. UvicornWorker is async: a single worker serves
# many requests at once, because while one waits on Supabase it handles
# the others. The familiar (2 x cores) + 1 rule is for SYNC workers, which
# take one request each — it does not apply here, and three copies of the
# app measured 270MB against 90MB for one, held every minute of every day
# to serve a handful of people.
web: gunicorn web.app:app --workers 1 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT --timeout 120
worker: python scripts/worker.py
