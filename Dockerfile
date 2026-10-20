FROM python:3.10-slim

# opencv-python needs libgl/libglib; paddleocr needs libgomp.
# PaddleOCR model files auto-download to ~/.paddleocr on first use.
# Build on Aliyun ECS: deb.debian.org is unreachable from CN, use the
# intranet mirror (free traffic, falls back gracefully off-Aliyun via the
# public mirrors.aliyuncs.com name only resolvable on ECS -- keep both
# attempts so local builds still work).
RUN sed -i "s|deb.debian.org|mirrors.cloud.aliyuncs.com|g; s|security.debian.org|mirrors.cloud.aliyuncs.com|g" \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null \
    || sed -i "s|deb.debian.org|mirrors.cloud.aliyuncs.com|g" /etc/apt/sources.list \
    || true
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple || \
    pip install --no-cache-dir -r requirements.txt

COPY . .

# Healthcheck: the daemon prints to stdout every scan cycle (5 min). A
# process that died would stop the container itself, so a simple liveness
# probe on the python process is enough.
CMD ["python", "-u", "start.py"]
