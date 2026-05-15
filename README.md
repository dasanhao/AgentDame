# News Agent 后端 (v3 - 加入 API 与定时任务)

## 文件结构

```
backend/
├── agent.py           你的 Pipeline (v2.4 基础上,新增写 articles 表)
├── db.py              数据库层 (SeenStore + ArticleStore)
├── api.py             FastAPI 服务,暴露 REST 接口
├── scheduler.py       定时任务 (每天 8:00 跑 Pipeline)
├── news_agent.db      SQLite 数据库 (自动创建)
├── output/            md/json 输出 (沿用)
└── requirements.txt
```

## 安装

```bash
pip install -r requirements.txt
```

## 三个常用启动姿势

### 1. 手动跑一次 Pipeline (调试期最常用)

```bash
# 设置 DeepSeek API Key
export DEEPSEEK_API_KEY=sk-xxx          # Linux/Mac
$env:DEEPSEEK_API_KEY="sk-xxx"          # Windows PowerShell

python agent.py                # 正常跑
python agent.py --reset        # 清空去重库后再跑
python agent.py --dry-run      # 只看正文准备,不调 LLM
```

跑完后:
- `output/news_YYYY-MM-DD.md` 给人看
- `output/news_YYYY-MM-DD.json` 备份
- `news_agent.db` 的 `articles` 表写入新条目,**这是 Android App 要读的**

### 2. 启动 API 服务 (给 Android App 用)

```bash
uvicorn api:app --host 0.0.0.0 --port 8000 --reload
```

启动后:
- **http://localhost:8000/docs** ← Swagger UI,可直接试所有接口
- 局域网内手机访问: `http://你电脑的局域网IP:8000`
- Android 模拟器访问宿主机: `http://10.0.2.2:8000`

### 3. 启动定时任务 (生产环境)

```bash
python scheduler.py                                # 前台跑
nohup python scheduler.py > scheduler.log 2>&1 &  # 后台跑
```

每天早 8 点自动调用 `agent.py`。

## API 接口清单

启动 API 后访问 `/docs` 看完整文档。核心接口:

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/articles` | 列文章,可加 `?date=YYYY-MM-DD` |
| GET | `/api/articles/{id}` | 单篇详情 |
| PATCH | `/api/articles/{id}` | 编辑(传 summary/key_points/opinion 任一) |
| POST | `/api/articles/{id}/publish` | 标记已发布 (`{"platforms": ["weibo"]}`) |
| GET | `/api/dates` | 有内容的日期列表 |
| POST | `/api/run` | 手动触发 Pipeline 跑一次 |

## 完整联调流程

```bash
# 终端 A: 启动 API
export DEEPSEEK_API_KEY=sk-xxx
uvicorn api:app --host 0.0.0.0 --port 8000 --reload

# 终端 B: 跑一次 Pipeline 产生数据
python agent.py --reset    # 第一次跑加 --reset

# 浏览器: 验证有数据
打开 http://localhost:8000/api/articles
应该能看到 JSON 数组,包含今天生成的文章

# Android App 配置 Base URL:
真机 + 同 WiFi:   http://192.168.x.x:8000  (你电脑的 IP)
模拟器:           http://10.0.2.2:8000
```

## 常见问题

**Q: API 启动后,Android 模拟器 ECONNREFUSED**
A: 模拟器里 `localhost` 指模拟器自己,要用 `10.0.2.2` 才能访问宿主机。

**Q: 真机访问不通**
A:
1. 手机和电脑在同一 WiFi
2. uvicorn 启动加了 `--host 0.0.0.0`(不是 127.0.0.1)
3. 电脑防火墙放行 8000 端口

**Q: 改了 agent.py 后,API 读到的还是旧数据**
A: API 是从 `news_agent.db` 读的,不是从 md 文件读。确认 agent.py 调用了 `save_to_articles_db()`。

**Q: 想清掉所有数据重新开始**
A: 删 `news_agent.db` 即可,下次启动会自动重建表。
