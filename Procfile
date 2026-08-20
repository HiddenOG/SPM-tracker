web: gunicorn web.app:app --workers 3 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT --timeout 120
worker: python scripts/worker.py
