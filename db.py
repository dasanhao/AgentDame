"""
db.py — 数据库层
─────────────────
表结构:
- seen     : 去重(沿用 v2.4)
- articles : 生成的文章(新增,供 API 读)
- audit    : 编辑/发布历史(新增,做基础追溯)

字段说明:
articles.status 枚举: draft(生成完待审) / approved(已审核) / published(已发布)
articles.published_to: JSON 字符串,记录发布到哪些平台 ["weibo", "x"]
"""
import sqlite3
import json
import datetime
import hashlib
from pathlib import Path
from typing import Optional, List
from dataclasses import dataclass, asdict


# ============================================================
# 数据模型 (对应 articles 表)
# ============================================================

@dataclass
class Article:
    id: str                      # 主键 = link 的 md5[:12](和 fingerprint 同步)
    date: str                    # 生成日期 YYYY-MM-DD
    source: str
    title: str
    link: str
    summary: str
    key_points: List[str]
    opinion: str
    score: int = 0
    status: str = "draft"        # draft / approved / published
    published_to: List[str] = None
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        if self.published_to is None:
            self.published_to = []


# ============================================================
# 通用连接
# ============================================================

def _connect(db_path: Path) -> sqlite3.Connection:
    """让 sqlite3 返回 dict-like 行"""
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(db_path: Path):
    """初始化所有表(幂等)"""
    conn = _connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS seen (
            fingerprint  TEXT PRIMARY KEY,
            title        TEXT,
            source       TEXT,
            link         TEXT,
            processed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS articles (
            id            TEXT PRIMARY KEY,
            date          TEXT NOT NULL,
            source        TEXT,
            title         TEXT,
            link          TEXT,
            summary       TEXT,
            key_points    TEXT,    -- JSON 数组字符串
            opinion       TEXT,
            score         INTEGER DEFAULT 0,
            status        TEXT DEFAULT 'draft',
            published_to  TEXT DEFAULT '[]',
            created_at    TEXT,
            updated_at    TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_articles_date   ON articles(date);
        CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);

        CREATE TABLE IF NOT EXISTS audit (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id TEXT,
            action     TEXT,     -- create/edit/approve/publish
            payload    TEXT,     -- JSON
            at         TEXT
        );
    """)
    conn.commit()
    conn.close()


# ============================================================
# SeenStore (沿用 v2.4 行为)
# ============================================================

class SeenStore:
    def __init__(self, db_path: Path):
        self.conn = _connect(db_path)

    def is_seen(self, fp: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM seen WHERE fingerprint = ?", (fp,))
        return cur.fetchone() is not None

    def mark_seen_one(self, fp: str, title: str, source: str, link: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO seen VALUES (?, ?, ?, ?, ?)",
            (fp, title, source, link, datetime.datetime.now().isoformat())
        )
        self.conn.commit()

    def mark_many(self, rows):
        """rows: List[(fp, title, source, link)]"""
        now = datetime.datetime.now().isoformat()
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen VALUES (?, ?, ?, ?, ?)",
            [(fp, t, s, l, now) for fp, t, s, l in rows],
        )
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]

    def reset(self):
        self.conn.execute("DELETE FROM seen")
        self.conn.commit()

    def close(self):
        self.conn.close()


# ============================================================
# ArticleStore — 新增,供 API 读写
# ============================================================

class ArticleStore:
    def __init__(self, db_path: Path):
        self.conn = _connect(db_path)

    @staticmethod
    def _row_to_article(row: sqlite3.Row) -> Article:
        return Article(
            id=row["id"],
            date=row["date"],
            source=row["source"] or "",
            title=row["title"] or "",
            link=row["link"] or "",
            summary=row["summary"] or "",
            key_points=json.loads(row["key_points"] or "[]"),
            opinion=row["opinion"] or "",
            score=row["score"] or 0,
            status=row["status"] or "draft",
            published_to=json.loads(row["published_to"] or "[]"),
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )

    def insert(self, a: Article):
        now = datetime.datetime.now().isoformat()
        self.conn.execute(
            """INSERT OR REPLACE INTO articles
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                a.id, a.date, a.source, a.title, a.link,
                a.summary, json.dumps(a.key_points, ensure_ascii=False),
                a.opinion, a.score, a.status,
                json.dumps(a.published_to, ensure_ascii=False),
                a.created_at or now, now,
            ),
        )
        self._audit(a.id, "create", {"source": a.source, "title": a.title})
        self.conn.commit()

    def update_content(self, id: str, *, summary: Optional[str] = None,
                       key_points: Optional[List[str]] = None,
                       opinion: Optional[str] = None) -> bool:
        sets, vals = [], []
        if summary is not None:
            sets.append("summary = ?"); vals.append(summary)
        if key_points is not None:
            sets.append("key_points = ?"); vals.append(json.dumps(key_points, ensure_ascii=False))
        if opinion is not None:
            sets.append("opinion = ?"); vals.append(opinion)
        if not sets:
            return False
        sets.append("updated_at = ?"); vals.append(datetime.datetime.now().isoformat())
        vals.append(id)
        cur = self.conn.execute(
            f"UPDATE articles SET {', '.join(sets)} WHERE id = ?", vals
        )
        self._audit(id, "edit", {
            "summary": summary, "key_points": key_points, "opinion": opinion
        })
        self.conn.commit()
        return cur.rowcount > 0

    def mark_published(self, id: str, platforms: List[str]) -> bool:
        cur = self.conn.execute("SELECT published_to FROM articles WHERE id = ?", (id,))
        row = cur.fetchone()
        if not row:
            return False
        current = set(json.loads(row["published_to"] or "[]"))
        current.update(platforms)
        now = datetime.datetime.now().isoformat()
        self.conn.execute(
            """UPDATE articles
               SET published_to = ?, status = 'published', updated_at = ?
               WHERE id = ?""",
            (json.dumps(sorted(current), ensure_ascii=False), now, id),
        )
        self._audit(id, "publish", {"platforms": platforms})
        self.conn.commit()
        return True

    def get_by_id(self, id: str) -> Optional[Article]:
        row = self.conn.execute("SELECT * FROM articles WHERE id = ?", (id,)).fetchone()
        return self._row_to_article(row) if row else None

    def list_by_date(self, date: str) -> List[Article]:
        rows = self.conn.execute(
            "SELECT * FROM articles WHERE date = ? ORDER BY score DESC, id ASC",
            (date,),
        ).fetchall()
        return [self._row_to_article(r) for r in rows]

    def list_dates(self, limit: int = 30) -> List[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT date FROM articles ORDER BY date DESC LIMIT ?", (limit,),
        ).fetchall()
        return [r["date"] for r in rows]

    def list_recent(self, limit: int = 50) -> List[Article]:
        rows = self.conn.execute(
            "SELECT * FROM articles ORDER BY date DESC, score DESC LIMIT ?", (limit,),
        ).fetchall()
        return [self._row_to_article(r) for r in rows]

    def _audit(self, article_id: str, action: str, payload: dict):
        self.conn.execute(
            "INSERT INTO audit (article_id, action, payload, at) VALUES (?, ?, ?, ?)",
            (article_id, action, json.dumps(payload, ensure_ascii=False),
             datetime.datetime.now().isoformat()),
        )

    def close(self):
        self.conn.close()


# ============================================================
# 工具:从 link 生成 id (与 fingerprint 同算法)
# ============================================================

def make_article_id(link: str, title: str = "") -> str:
    key = link or title
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:12]
