# 选择稳定的 Python 运行环境
FROM python:3.11-slim

# 容器内工作目录
WORKDIR /app

# 先拷贝依赖清单并安装（利用缓存加速构建）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷贝项目代码
COPY . .

# 以“worker”方式运行你的机器人
CMD ["python", "checkin_bot.py"]
