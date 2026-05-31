# Competitor Analysis

## 启动前准备

1. 安装依赖：
   - 后端目录：`backend`
   - 前端目录：`frontend`
2. 确保已配置环境变量文件：
   - `Competitor_Analysis/.env`
   - `Competitor_Analysis/backend/.env`

## 后端启动

在 `backend` 目录执行：

```bash
uv run uvicorn app.main:app --reload --port 8010
```

后端默认地址：`http://127.0.0.1:8010`

## 前端启动

在 `frontend` 目录执行：

```bash
npm run dev
```

前端默认地址：`http://127.0.0.1:3000`

## 推荐启动顺序

1. 先启动后端（8010）
2. 再启动前端（3000）
3. 浏览器访问前端地址并开始联调
