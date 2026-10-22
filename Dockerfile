FROM python:3.10-slim

# opencv-python needs libgl/libglib; paddleocr needs libgomp.
# PaddleOCR model files auto-download to ~/.paddleocr on first use.
# deb.debian.org is unreachable from mainland CN build hosts, while the
# Aliyun intranet mirror only resolves on Aliyun ECS. Try the mirror first
# and fall back to the stock sources so the same image builds on both.
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        cp /etc/apt/sources.list.d/debian.sources /tmp/debian.sources.bak; \
        sed -i "s|deb.debian.org|mirrors.cloud.aliyuncs.com|g; s|security.debian.org|mirrors.cloud.aliyuncs.com|g" \
            /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if ! apt-get update; then \
        if [ -f /tmp/debian.sources.bak ]; then \
            cp /tmp/debian.sources.bak /etc/apt/sources.list.d/debian.sources; \
        fi; \
        apt-get update; \
    fi; \
    apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1; \
    rm -rf /var/lib/apt/lists/*

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
