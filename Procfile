# One worker, not three. UvicornWorker is async: a single worker serves
# many requests at once, because while one waits on Supabase it handles
# the others. The familiar (2 x cores) + 1 rule is for SYNC workers, which
# take one request each — it does not apply here, and three copies of the
# app measured 270MB against 90MB for one, held every minute of every day
# to serve a handful of people.
web: gunicorn web.app:app --workers 1 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT --timeout 120
worker: python scripts/worker.py

# The MCP server, for asking questions from claude.ai or a phone. Its own
# process rather than mounted into the web app: a connector lookup can sit for
# a minute or two on a Gmail fetch, and the website runs on a single async
# worker, so sharing it would make the site feel slow for everyone while one
# question was being answered.
#
# Needs MCP_PUBLIC_URL set to this service's own https address, and the same
# JWT_SECRET as the web app - connector tokens are signed with it. Requires
# mcp from requirements-mcp.txt, which is NOT in requirements.txt, so this
# service needs that installed too.
mcp: python scripts/mcp_server.py --http
