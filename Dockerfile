# RandomGenerals AI - container image for any CPU-only host.
#
# Render uses render.yaml (its native Python runtime) rather than this
# file; this is for hosts that take a container - Railway, Fly, Spaces.
#
# WHAT IS AND ISN'T IN HERE
# No Ollama and no torch/diffusers. These hosts have a couple of shared
# vCPUs and no GPU: a 7B model would answer at about a word per second
# and the local image model would take minutes per picture. Replies come
# from Gemini instead (gemini.py, already the fallback for when the local
# machine is off) and image generation from the hosted API that
# imagegen.gemini_configured() checks for. Everything else - accounts,
# threads, memory, credits, the sandboxed code runner, web search, and
# the video bay - runs here unchanged.
#
# Skipping torch takes the image from ~2.5GB to ~300MB, which matters
# every time the host rebuilds it.
#
# TWO THINGS TO SET ON THE HOST
#   GEMINI_API_KEY  without it there is no model to answer with, and
#                   every prompt fails at the point of asking.
#   DB_PATH         point it at a mounted volume, e.g. /data/app.db.
#                   The container filesystem does not survive a deploy,
#                   so leaving this at its default means every account,
#                   thread and credit balance is wiped on each push. It
#                   is one env var and it is the difference between a
#                   real deployment and a demo.

FROM python:3.11-slim

# Hugging Face Spaces runs containers as UID 1000. Files written by root
# at build time would then be unwritable at runtime, so the user is
# created up front and everything is owned by it.
# ffmpeg is a runtime dependency, not a nicety: videoedit.py shells out
# to it for every render, and without it the video bay correctly reports
# itself unavailable - which looks like a broken feature rather than a
# missing package. ~80MB for the whole bay working.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces runs containers as UID 1000, and Railway and Fly
# are happier not running as root either. Files written by root at build
# time would be unwritable at runtime, so the user is created up front
# and everything below is owned by it.
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Dependencies first, so a code change doesn't invalidate the pip layer.
COPY --chown=appuser:appuser requirements-cloud.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements-cloud.txt

COPY --chown=appuser:appuser . .

# Writable locations the app needs. The container filesystem is
# otherwise effectively read-only for the app user.
RUN mkdir -p /app/static/uploads/video /app/static/generated \
    /app/static/video/renders /app/data \
    && chown -R appuser:appuser /app

USER appuser

# 7860 is the port Spaces expects; app.py already reads PORT, and
# Railway and Fly inject their own.
# TRUST_PROXY: every one of these hosts terminates TLS in front of the
# container, so the X-Forwarded-* headers come from the proxy and can
# be trusted. Google sign-in needs it to build the right redirect_uri.
#
# The comment lives here rather than inside the ENV block: a `#` line
# within a line continuation is not stripped by every builder, and
# where it is not, it becomes part of the instruction and fails.
ENV PORT=7860 \
    APP_DEBUG=0 \
    ALLOW_MOCK_UPGRADE=0 \
    FORCE_HTTPS_COOKIES=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/app \
    TRUST_PROXY=1

EXPOSE 7860

# Spaces restarts an unhealthy container, so /api/health earns its keep.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7860/api/health', timeout=4).status==200 else 1)"

# gunicorn, not app.py. Flask's built-in server is explicitly not for
# public traffic, and app.py's own __main__ block says as much - running
# it here would serve real users from the development server.
#
# The settings mirror render.yaml for the same reasons documented there:
# one worker because db.py holds a single module-level SQLite connection
# that is thread-safe within a process but would become one connection
# per process if workers scaled; threads because a streamed reply spends
# its life waiting on the model rather than on CPU; and a long timeout
# because gunicorn's 30s default would cut off any answer still being
# written.
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-7860} \
     --worker-class gthread --workers 1 --threads 8 --timeout 300 \
     --access-logfile -"]
