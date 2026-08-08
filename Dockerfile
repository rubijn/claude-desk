FROM python:3.13-slim

# ccdeck is standard-library only, so there is nothing to install and no
# dependency layer to cache. The image is the interpreter plus one file.
WORKDIR /app
# --chmod because COPY otherwise preserves the host file's mode, and ccdeck.py is
# 0600 in this checkout — which USER nobody below cannot read.
COPY --chmod=0644 ccdeck.py .

# 127.0.0.1 inside a container is only reachable from inside the container;
# the published port on the host is what hooks actually talk to.
ENV CCDECK_HOST=0.0.0.0 \
    CCDECK_PORT=8787 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8787
USER nobody

# /api/state exercises the lock and the snapshot, so a wedged board fails here.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('CCDECK_PORT','8787')+'/api/state',timeout=3).read()"

CMD ["python3", "ccdeck.py"]
