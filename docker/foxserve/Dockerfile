FROM python:3.12-slim

# Camoufox headful 需要 Xvfb 与一组图形/字体库；--shm-size 由运行参数给足
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb libgtk-3-0 libdbus-glib-1-2 libxt6 libasound2 fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "camoufox[geoip]" fastapi uvicorn

# 预下载 Camoufox 浏览器，避免每次启动时拉取
RUN python -m camoufox fetch

COPY foxserve.py /app/foxserve.py
WORKDIR /app

EXPOSE 8100
CMD ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1440x900x24", \
     "python", "-m", "uvicorn", "foxserve:app", "--host", "0.0.0.0", "--port", "8100"]
