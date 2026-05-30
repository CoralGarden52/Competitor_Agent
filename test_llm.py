from openai import OpenAI
from dotenv import load_dotenv  # 关键：加载.env文件
import os

# 自动加载 .env 里的所有环境变量
load_dotenv()

# 初始化客户端（自动读取配置）
client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url=os.getenv("OPENAI_BASE_URL")
)

# 测试调用
response = client.chat.completions.create(
    model=os.getenv("OPENAI_MODEL"),
    messages=[{"role": "user", "content": "你好"}]
)

# 打印结果
print("模型返回：", response.choices[0].message.content)