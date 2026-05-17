# 股票分析程序Dockerfile
FROM python:3.8-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ssh \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 复制项目文件
COPY . /app

# 安装Python依赖
RUN pip install --no-cache-dir -r requirements.txt

# 配置SSH免密登录（用于拉取代码）
RUN mkdir -p ~/.ssh && chmod 700 ~/.ssh

# 设置环境变量
ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=INFO
ENV STREAMLIT_PORT=8501
ENV STREAMLIT_HOST=0.0.0.0

# 暴露Streamlit端口
EXPOSE 8501

# 启动web服务和股票分析程序
CMD ["bash", "-c", "python main.py & streamlit run src/app/dashboard.py --server.port $STREAMLIT_PORT --server.address $STREAMLIT_HOST"]
