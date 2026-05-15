"""
News Agent v2.4 (DeepSeek 版) — 多源 fallback
─────────────────────────────────────────
v2.4 改动 (RSS 源健壮性):
- 每个 source 支持多 URL 备选,自动选最优
- 从 entry 中提取最长内容字段 (content:encoded 优先于 summary)
- 备选源按内容质量打分,达到 RSS_SUMMARY_MIN_FULL 就采用,否则在所有可用源里选最好的

v2.3: 内容质量护栏 (短文本回退/跳过)
v2.2: 换虎嗅源 / RSS 健康诊断 / 标题黑名单 / 每源配额
v2.1: 修模型名 / link 指纹 / dict 去重 / logging

运行:
    pip install feedparser openai httpx trafilatura
    export DEEPSEEK_API_KEY=sk-xxx
    python agent.py [--reset] [--dry-run]
"""
import os
import json
import hashlib
import sqlite3
import logging
import argparse
import datetime
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import List

import httpx
import feedparser
import trafilatura
from openai import OpenAI


# ============================================================
# 配置区
# ============================================================

RSS_FEEDS = [
    {
        "name": "36氪",
        "urls": ["https://36kr.com/feed"],
    },
    {
        "name": "虎嗅",
        # 多个备选源,按顺序尝试,第一个能给够正文的获胜
        # FeedX 镜像 summary 偏短 (~500字),RSSHub 路由理论上能给完整正文
        "urls": [
            "https://rsshub.app/huxiu/article",
            "https://feedx.net/rss/huxiu.xml",
            "https://rss.huxiu.com/",
        ],
    },
    {
        "name": "少数派",
        "urls": ["https://sspai.com/feed"],
    },
]

MAX_PER_FEED      = 8     # 每个源多抓一些,因为后面要 LLM 筛选
TOP_N             = 5     # 最终深度加工的数量
PER_SOURCE_CAP    = 2     # 每个源在 Top N 中最多入选几条,保多样性
MODEL             = "deepseek-chat"  # DeepSeek-V3,官方在售模型
FETCH_TIMEOUT     = 10    # 抓正文超时(秒)
FETCH_MAX_LEN     = 4000  # 正文最长保留字符数(防止 token 爆炸)

# 内容长度护栏:
# RSS_SUMMARY_MIN_FULL: 如果 RSS summary 本身就够长(如 FeedX 全文 RSS),
#                      直接当作正文用,不必再去抓页面
# CONTENT_MIN_FOR_LLM: 给 LLM 加工的内容下限,低于此值视为正文获取失败,
#                      跳过 LLM 调用而不是让它瞎编
RSS_SUMMARY_MIN_FULL = 800
CONTENT_MIN_FOR_LLM  = 800

# 标题黑名单:聚合稿、早晚报、盘点类不适合做"单条深度评论"
TITLE_BLACKLIST = ["8点1氪", "早报", "晚报", "周报", "盘点", "每周精选", "本周推荐"]

DB_PATH        = Path("news_agent.db")
OUTPUT_DIR     = Path("output")


# --- Prompt 区 ---

# 用于 LLM 打分选 Top N
RANKER_PROMPT = """
你是一名资深新闻编辑,需要从一批新闻标题中挑出最值得深度评论的几条。
评分维度:
- 重要性(影响范围、长期意义)
- 信息密度(是否包含具体事实)
- 评论价值(是否值得展开观点)

请给每条标题打 1-10 分,严格输出 JSON 数组,不要任何额外文字:
{"scores": [{"index": 0, "score": 8}, {"index": 1, "score": 5}, ...]}

注意:index 必须与输入的编号严格对应,从 0 开始,且每个 index 只出现一次。
"""

# 用于内容加工
PERSONA_PROMPT = """
你是一名关注科技与全球时事的资深评论员。
风格:理性、克制、略带批判性思维。
立场:重视事实、警惕情绪化叙事、关注长期影响。

请基于给定的新闻原文,完成以下任务:
1. 用一句话(50字内)概括核心事实
2. 提炼 3 个关键要点(每条 30 字内)
3. 给出 150-200 字的个人观点评论

要求:
- 评论必须基于原文,严禁编造未提及的事实
- 避免绝对化表述、煽动性词汇
- 严格输出 JSON,不要任何额外文字

{"summary": "...", "key_points": ["...", "...", "..."], "opinion": "..."}
"""


# ============================================================
# 日志
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("news_agent")


# ============================================================
# 数据结构
# ============================================================

@dataclass
class NewsItem:
    source: str
    title: str
    link: str
    published: str = ""
    summary: str = ""           # RSS 原文摘要(后备)
    full_text: str = ""         # 抓取的全文(优先用)

    @property
    def fingerprint(self) -> str:
        # 用 link 做指纹,标题被编辑修改后也不会误判为新文章
        # link 为空时回退到 title,保证 always 有值
        key = self.link or self.title
        return hashlib.md5(key.encode("utf-8")).hexdigest()[:12]

    @property
    def content_for_llm(self) -> str:
        """优先用全文,没抓到则回退到 RSS 摘要"""
        return self.full_text if self.full_text else self.summary


@dataclass
class ProcessedItem:
    source: str
    title: str
    link: str
    summary: str
    key_points: List[str]
    opinion: str
    score: int = 0


# ============================================================
# 模块 1: SQLite 去重 (B3)
# ============================================================

class SeenStore:
    """记录已处理过的新闻指纹,避免重复加工"""

    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS seen (
                fingerprint TEXT PRIMARY KEY,
                title       TEXT,
                source      TEXT,
                link        TEXT,
                processed_at TEXT
            )
        """)
        self.conn.commit()

    def is_seen(self, fp: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM seen WHERE fingerprint = ?", (fp,))
        return cur.fetchone() is not None

    def mark_seen(self, item: NewsItem):
        self.conn.execute(
            "INSERT OR IGNORE INTO seen VALUES (?, ?, ?, ?, ?)",
            (item.fingerprint, item.title, item.source, item.link,
             datetime.datetime.now().isoformat())
        )
        self.conn.commit()

    def mark_many(self, items: List[NewsItem]):
        rows = [
            (i.fingerprint, i.title, i.source, i.link,
             datetime.datetime.now().isoformat())
            for i in items
        ]
        self.conn.executemany("INSERT OR IGNORE INTO seen VALUES (?, ?, ?, ?, ?)", rows)
        self.conn.commit()

    def count(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) FROM seen")
        return cur.fetchone()[0]

    def reset(self):
        """清空去重表(--reset 用),用于测试或换源后强制重抓"""
        self.conn.execute("DELETE FROM seen")
        self.conn.commit()

    def close(self):
        self.conn.close()


# ============================================================
# 模块 2: RSS 采集
# ============================================================

def extract_entry_content(entry) -> str:
    """
    从一个 RSS entry 中取出最长的正文内容。
    优先 content:encoded(全文 RSS 习惯字段),其次 summary/description。
    """
    candidates = []
    # feedparser 把 <content:encoded> 解析到 entry.content 列表里
    if entry.get("content"):
        for c in entry["content"]:
            v = c.get("value", "")
            if v:
                candidates.append(v)
    # summary / description
    s = entry.get("summary", "")
    if s:
        candidates.append(s)
    if not candidates:
        return ""
    # 返回最长的那个
    return max(candidates, key=len)


def try_parse_feed(name: str, urls: List[str]):
    """
    依次尝试 urls 列表,返回第一个成功(有 entries)的 parsed feed。
    用最长 entry 的内容长度作为"质量分",所有源都失败时返回 None。
    """
    best = None
    best_score = 0
    for url in urls:
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            log.warning("  %s: 解析 %s 失败 -> %s", name, url, e)
            continue

        status = parsed.get("status")
        if status and status >= 400:
            log.warning("  %s: %s HTTP %s", name, url, status)
            continue
        if not parsed.entries:
            log.warning("  %s: %s 无条目", name, url)
            continue

        # 评估质量:取前 3 条 entry 的最长内容长度的平均值
        sample = parsed.entries[:3]
        avg_len = sum(len(extract_entry_content(e)) for e in sample) // max(len(sample), 1)
        log.info("  %s: %s 可用 (entries=%d, avg_content=%d字)",
                 name, url, len(parsed.entries), avg_len)

        if avg_len > best_score:
            best, best_score = parsed, avg_len

        # 已经够好,不用再试备选
        if avg_len >= RSS_SUMMARY_MIN_FULL:
            return parsed

    return best


def collect_news(seen_store: SeenStore) -> List[NewsItem]:
    """阶段 1: 采集,并跳过已处理过的"""
    log.info("[1/5] 采集 RSS 源...")
    items, in_batch_seen = [], set()
    skipped_db = 0
    skipped_blacklist = 0

    for feed in RSS_FEEDS:
        try:
            parsed = try_parse_feed(feed["name"], feed["urls"])
            if parsed is None:
                log.error("  %s: 所有备选源都不可用", feed["name"])
                continue

            new_count = 0
            for entry in parsed.entries[:MAX_PER_FEED]:
                title = entry.get("title", "").strip()
                if not title or title in in_batch_seen:
                    continue
                in_batch_seen.add(title)

                # 标题黑名单:聚合稿/早晚报不适合深度评论
                if any(kw in title for kw in TITLE_BLACKLIST):
                    skipped_blacklist += 1
                    continue

                # 取最长正文字段(content:encoded > summary)
                content = extract_entry_content(entry)[:FETCH_MAX_LEN]

                item = NewsItem(
                    source=feed["name"],
                    title=title,
                    link=entry.get("link", ""),
                    published=entry.get("published", ""),
                    summary=content,
                )

                # B3: 数据库去重
                if seen_store.is_seen(item.fingerprint):
                    skipped_db += 1
                    continue

                items.append(item)
                new_count += 1
            log.info("  %s: 新增 %d 条", feed["name"], new_count)
        except Exception as e:
            log.error("  %s: %s", feed["name"], e)

    log.info("合计新增 %d 条 (历史去重 %d,黑名单过滤 %d)",
             len(items), skipped_db, skipped_blacklist)
    return items


# ============================================================
# 模块 3: LLM 打分选 Top N (B1)
# ============================================================

def rank_and_select(client: OpenAI, items: List[NewsItem], n: int,
                    per_source_cap: int = PER_SOURCE_CAP) -> List[NewsItem]:
    """让 LLM 给所有标题打分,按分数排序后用每源配额选出 Top N"""
    log.info("[2/5] LLM 从 %d 条中筛选 Top %d (每源最多 %d 条)...",
             len(items), n, per_source_cap)

    if len(items) <= n:
        log.info("候选数 ≤ %d,无需筛选,全部进入下一阶段", n)
        return items

    # 拼成编号列表给 LLM
    titles_text = "\n".join(
        f"{i}. [{item.source}] {item.title}"
        for i, item in enumerate(items)
    )
    user_msg = f"以下是今日候选新闻,请为每条打分:\n\n{titles_text}"

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": RANKER_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            max_tokens=512,        # 仅返回打分 JSON,不需要太大
            temperature=0.3,       # 打分要稳定,温度低
        )
        data = json.loads(resp.choices[0].message.content)
        scores = data.get("scores", [])

        # 用 dict 去重 + 兜底 0 分
        score_map = {}
        for s in scores:
            idx = s.get("index", -1)
            if 0 <= idx < len(items):
                score_map[idx] = s.get("score", 0)

        scored_items = [(items[i], score_map.get(i, 0)) for i in range(len(items))]
        scored_items.sort(key=lambda x: x[1], reverse=True)

        # 每源配额选择:同源不超过 per_source_cap 条
        selected, source_count, overflow = [], {}, []
        for item, score in scored_items:
            if len(selected) >= n:
                break
            cnt = source_count.get(item.source, 0)
            if cnt >= per_source_cap:
                overflow.append((item, score))
                continue
            selected.append((item, score))
            source_count[item.source] = cnt + 1

        # 如果配额太严导致选不满 n 条,从 overflow 里按分数补足
        if len(selected) < n:
            need = n - len(selected)
            log.info("配额限制下只选出 %d 条,从同源候补里补 %d 条",
                     len(selected), need)
            selected.extend(overflow[:need])

        log.info("打分 Top %d:", n)
        for item, score in selected:
            log.info("  ⭐ %d/10  [%s] %s", score, item.source, item.title[:50])
        return [item for item, _ in selected]

    except Exception as e:
        log.warning("打分失败 (%s),回退到取前 %d 条", e, n)
        return items[:n]


# ============================================================
# 模块 4: 抓取全文 (B2)
# ============================================================

def fetch_full_text(client: httpx.Client, url: str) -> str:
    """用共享 httpx client 拉网页,用 trafilatura 抽正文"""
    if not url:
        return ""
    try:
        r = client.get(url)
        r.raise_for_status()
        text = trafilatura.extract(r.text, include_comments=False, include_tables=False)
        if not text:
            return ""
        return text[:FETCH_MAX_LEN]
    except Exception as e:
        log.warning("    抓全文失败: %s", e)
        return ""


def enrich_with_full_text(items: List[NewsItem]) -> List[NewsItem]:
    """
    阶段 3: 给每条新闻补全文,按优先级:
    1. RSS summary 已经够长 (≥ RSS_SUMMARY_MIN_FULL) → 直接用,跳过抓取
    2. 抓取页面正文 → 如果长度合理就用
    3. 抓取太短 (可能是登录墙/反爬页) → 回退到 RSS summary
    最终 content_for_llm 仍小于 CONTENT_MIN_FOR_LLM 的,会在加工阶段被跳过
    """
    log.info("[3/5] 准备 %d 条新闻的正文...", len(items))

    # 先看哪些 RSS 已经给了全文,根本不用抓
    need_fetch = []
    for item in items:
        if len(item.summary) >= RSS_SUMMARY_MIN_FULL:
            item.full_text = item.summary
            log.info("  [RSS全文] %s (%d 字)", item.title[:40], len(item.full_text))
        else:
            need_fetch.append(item)

    if not need_fetch:
        return items

    log.info("  其余 %d 条需抓取页面...", len(need_fetch))
    with httpx.Client(
        timeout=FETCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (NewsAgent)"},
    ) as http:
        for i, item in enumerate(need_fetch, 1):
            log.info("  (%d/%d) %s...", i, len(need_fetch), item.link[:60])
            fetched = fetch_full_text(http, item.link)
            # 如果抓回来比 RSS summary 还短,大概率是登录墙/反爬页
            if len(fetched) >= max(CONTENT_MIN_FOR_LLM, len(item.summary)):
                item.full_text = fetched
                log.info("    抓到 %d 字 ✓", len(fetched))
            elif item.summary:
                item.full_text = item.summary
                log.info("    抓取仅 %d 字 (疑似登录墙),回退 RSS summary (%d 字)",
                         len(fetched), len(item.summary))
            else:
                item.full_text = fetched  # 两个都短,只能用这个
                log.warning("    正文不足 (抓取 %d 字 / RSS %d 字),加工阶段会跳过",
                            len(fetched), len(item.summary))
    return items


# ============================================================
# 模块 5: LLM 加工
# ============================================================

def make_client() -> OpenAI:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("请设置环境变量 DEEPSEEK_API_KEY")
    return OpenAI(api_key=api_key, base_url="https://api.deepseek.com")


def process_with_llm(client: OpenAI, item: NewsItem) -> ProcessedItem:
    user_msg = (
        f"新闻来源: {item.source}\n"
        f"标题: {item.title}\n"
        f"原文内容:\n{item.content_for_llm}"
    )
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": PERSONA_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        response_format={"type": "json_object"},
        max_tokens=1024,
        temperature=0.5,  # 评论需要克制理性,稍低一点
    )
    raw = resp.choices[0].message.content.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"summary": item.title, "key_points": [], "opinion": raw}

    return ProcessedItem(
        source=item.source, title=item.title, link=item.link,
        summary=data.get("summary", ""),
        key_points=data.get("key_points", []),
        opinion=data.get("opinion", ""),
    )


def process_all(client: OpenAI, items: List[NewsItem]) -> List[ProcessedItem]:
    log.info("[4/5] DeepSeek 加工 %d 条...", len(items))
    results = []
    for i, item in enumerate(items, 1):
        content = item.content_for_llm
        # 关键护栏:内容不足直接跳过,不让 LLM 基于标题瞎编观点
        if len(content) < CONTENT_MIN_FOR_LLM:
            log.warning("  (%d/%d) 跳过 [正文仅 %d 字 < %d]: %s",
                        i, len(items), len(content),
                        CONTENT_MIN_FOR_LLM, item.title[:45])
            continue

        used = "全文" if item.full_text else "摘要"
        log.info("  (%d/%d) [%s %d字] %s...",
                 i, len(items), used, len(content), item.title[:40])
        try:
            results.append(process_with_llm(client, item))
        except Exception as e:
            log.warning("  失败: %s", e)
    return results


# ============================================================
# 模块 6: 渲染输出
# ============================================================

def render_markdown(items: List[ProcessedItem]) -> str:
    today = datetime.date.today().isoformat()
    md = [
        f"# 每日观察 · {today}\n",
        f"> 共 {len(items)} 条 | News Agent v2.4 (DeepSeek) 自动生成,人工审核后发布\n",
        "---\n",
    ]
    for i, item in enumerate(items, 1):
        md.append(f"## {i}. {item.title}\n")
        md.append(f"**来源**: {item.source} · [原文链接]({item.link})\n")
        md.append(f"### 📌 核心事实\n{item.summary}\n")
        if item.key_points:
            md.append("### 🔑 关键要点")
            for kp in item.key_points:
                md.append(f"- {kp}")
            md.append("")
        md.append(f"### 💭 观点\n{item.opinion}\n")
        md.append("---\n")
    return "\n".join(md)


# ============================================================
# 主流程
# ============================================================

def parse_args():
    p = argparse.ArgumentParser(description="News Agent v2.4")
    p.add_argument("--reset", action="store_true",
                   help="清空去重库后再运行 (用于测试或换源后强制重抓)")
    p.add_argument("--dry-run", action="store_true",
                   help="只跑采集+打分+正文准备,不调用 LLM 加工,不写入去重库")
    return p.parse_args()


def main():
    args = parse_args()
    OUTPUT_DIR.mkdir(exist_ok=True)

    log.info("=" * 50)
    log.info("News Agent v2.4 启动 (多源 fallback)")
    if args.dry_run:
        log.info("** DRY-RUN 模式:不调用 LLM 加工,不写去重库 **")
    log.info("=" * 50)

    seen_store = SeenStore(DB_PATH)

    if args.reset:
        n = seen_store.count()
        seen_store.reset()
        log.info("已清空去重库 (原 %d 条)", n)

    client = make_client()

    try:
        # B3: 采集时跳过已处理
        raw_items = collect_news(seen_store)
        if not raw_items:
            log.info("今日没有新内容,退出 (提示: --reset 可清库后重试)")
            return

        # B1: LLM 打分选 Top N
        top_items = rank_and_select(client, raw_items, TOP_N)

        # 未被选中的低分候选,直接标记已读,避免明天再来打分
        # dry-run 模式下不写入,避免影响下一次正式运行
        if not args.dry_run:
            top_fps = {it.fingerprint for it in top_items}
            unselected = [it for it in raw_items if it.fingerprint not in top_fps]
            if unselected:
                seen_store.mark_many(unselected)
                log.info("已将 %d 条未入选候选标记为已读", len(unselected))

        # B2: 抓取/准备全文
        top_items = enrich_with_full_text(top_items)

        # dry-run 到此为止:看 RSS 全文是否被正确识别即可
        if args.dry_run:
            log.info("=" * 50)
            log.info("DRY-RUN 结束。正文长度一览:")
            for it in top_items:
                content = it.content_for_llm
                tag = "✓" if len(content) >= CONTENT_MIN_FOR_LLM else "✗ 不足"
                log.info("  [%s %s] %d 字  %s",
                         tag, it.source, len(content), it.title[:40])
            return

        # 加工
        processed = process_all(client, top_items)

        # 输出
        log.info("[5/5] 生成输出文件...")
        today = datetime.date.today().isoformat()
        md_path = OUTPUT_DIR / f"news_{today}.md"
        json_path = OUTPUT_DIR / f"news_{today}.json"
        md_path.write_text(render_markdown(processed), encoding="utf-8")
        json_path.write_text(
            json.dumps([asdict(p) for p in processed], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 标记成功加工的(失败的下次还能重试)
        # 用 link 反查 fingerprint,比 title 更稳
        link_to_item = {it.link: it for it in top_items}
        success_items = [link_to_item[p.link] for p in processed if p.link in link_to_item]
        seen_store.mark_many(success_items)

        log.info("  -> %s", md_path)
        log.info("  -> %s", json_path)
        log.info("完成!共加工 %d 条,已记入去重库", len(processed))
    finally:
        seen_store.close()


if __name__ == "__main__":
    main()