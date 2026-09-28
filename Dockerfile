FROM python:3.10-slim

# opencv-python needs libgl/libglib; paddleocr needs libgomp.
# PaddleOCR model files auto-download to ~/.paddleocr on first use.
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
