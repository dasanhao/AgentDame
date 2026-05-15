"""
api.py — FastAPI 服务
─────────────────────
启动:
    uvicorn api:app --host 0.0.0.0 --port 8000 --reload

访问:
    http://localhost:8000/docs   ← Swagger UI 自动生成,可直接试接口
    http://localhost:8000/api/articles
"""
import datetime
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from db import init_schema, ArticleStore


DB_PATH = Path("news_agent.db")


# ============================================================
# 启动
# ============================================================

app = FastAPI(
    title="News Agent API",
    description="供 Android App 读取每日 AI 生成的新闻评论",
    version="1.0.0",
)

# 允许 Android 模拟器 / 真机访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_schema(DB_PATH)


# ============================================================
# DTO (Android 收到的 JSON 结构)
# ============================================================

class ArticleDTO(BaseModel):
    id: str
    date: str
    source: str
    title: str
    link: str
    summary: str
    key_points: List[str]
    opinion: str
    score: int
    status: str
    published_to: List[str]
    created_at: str
    updated_at: str


class EditPayload(BaseModel):
    """编辑文章时 PATCH 的请求体,每个字段都可选"""
    summary: Optional[str] = None
    key_points: Optional[List[str]] = None
    opinion: Optional[str] = None


class PublishPayload(BaseModel):
    """发布请求:指定要发到哪些平台"""
    platforms: List[str] = Field(..., examples=[["weibo", "x"]])


class RunResult(BaseModel):
    started: bool
    message: str


# ============================================================
# 路由
# ============================================================

def _store():
    """每次请求拿一个新 store,简单可靠;高并发时再换连接池"""
    return ArticleStore(DB_PATH)


@app.get("/api/health", tags=["meta"])
def health():
    return {"ok": True, "now": datetime.datetime.now().isoformat()}


@app.get("/api/articles", response_model=List[ArticleDTO], tags=["articles"])
def list_articles(
    date: Optional[str] = None,
    limit: int = 50,
):
    """
    列文章。
    - 不传 date:返回最近 limit 条(按日期+分数 desc)
    - 传 date=YYYY-MM-DD:返回指定日期的所有文章
    """
    store = _store()
    try:
        if date:
            items = store.list_by_date(date)
        else:
            items = store.list_recent(limit=limit)
        return [ArticleDTO(**a.__dict__) for a in items]
    finally:
        store.close()


@app.get("/api/dates", response_model=List[str], tags=["articles"])
def list_dates(limit: int = 30):
    """有内容的日期清单,给 App 做日历/分组用"""
    store = _store()
    try:
        return store.list_dates(limit=limit)
    finally:
        store.close()


@app.get("/api/articles/{article_id}", response_model=ArticleDTO, tags=["articles"])
def get_article(article_id: str):
    store = _store()
    try:
        a = store.get_by_id(article_id)
        if not a:
            raise HTTPException(404, f"article {article_id} not found")
        return ArticleDTO(**a.__dict__)
    finally:
        store.close()


@app.patch("/api/articles/{article_id}", response_model=ArticleDTO, tags=["articles"])
def edit_article(article_id: str, payload: EditPayload):
    """编辑文章内容(摘要/要点/观点),用于 App 端审核后修改"""
    store = _store()
    try:
        if not store.get_by_id(article_id):
            raise HTTPException(404, f"article {article_id} not found")
        store.update_content(
            article_id,
            summary=payload.summary,
            key_points=payload.key_points,
            opinion=payload.opinion,
        )
        a = store.get_by_id(article_id)
        return ArticleDTO(**a.__dict__)
    finally:
        store.close()


@app.post("/api/articles/{article_id}/publish", response_model=ArticleDTO, tags=["articles"])
def publish_article(article_id: str, payload: PublishPayload):
    """
    标记为已发布。
    注意:这一步只是改数据库状态。
    真正调用平台 API 是 Android 端做(MVP 阶段走"复制到剪贴板+跳转 App")。
    """
    store = _store()
    try:
        ok = store.mark_published(article_id, payload.platforms)
        if not ok:
            raise HTTPException(404, f"article {article_id} not found")
        a = store.get_by_id(article_id)
        return ArticleDTO(**a.__dict__)
    finally:
        store.close()


@app.post("/api/run", response_model=RunResult, tags=["meta"])
def trigger_run(background: BackgroundTasks):
    """
    手动触发一次 Pipeline。
    用 subprocess 在后台跑 agent.py,不阻塞 API 响应。
    """
    def _run():
        try:
            subprocess.Popen(
                [sys.executable, "agent.py"],
                cwd=Path(__file__).parent,
            )
        except Exception as e:
            print(f"[run] 启动失败: {e}")

    background.add_task(_run)
    return RunResult(
        started=True,
        message="Pipeline 已在后台启动,请稍后刷新文章列表查看",
    )
