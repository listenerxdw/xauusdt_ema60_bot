FROM python:3.12-slim
WORKDIR /app
COPY . .
# 建议先升级 pip，再安装依赖
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt
CMD ["python", "main.py"]