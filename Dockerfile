# 1. 使用项目要求的 Python 3.12
FROM docker.m.daocloud.io/library/python:3.12-slim

# 2. 后续命令默认在容器的 /app 目录执行
WORKDIR /app

# 3. 安装项目使用的依赖管理工具 uv
RUN pip install --no-cache-dir uv

# 4. 把依赖配置和源码复制到容器
COPY pyproject.toml uv.lock ./
COPY src ./src

# 5. 根据 uv.lock 安装运行时依赖和当前项目
RUN uv sync --frozen --no-dev

# 6. 让系统能够找到 uv 创建的 kama-core 命令
ENV PATH="/app/.venv/bin:$PATH"
ENV KAMA_HOST=0.0.0.0
ENV KAMA_PORT=7437
ENV PYTHONUNBUFFERED=1

# 7. 声明 Core 使用的端口
EXPOSE 7437

# 8. 容器启动时运行 kama-core
CMD ["kama-core"]
