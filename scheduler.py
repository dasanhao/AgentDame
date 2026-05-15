"""
scheduler.py — 定时任务
─────────────────────────
每天 8:00 自动跑一次 Pipeline。

启动:
    python scheduler.py

或者用 nohup 让它后台跑:
    nohup python scheduler.py > scheduler.log 2>&1 &
"""
import subprocess
import sys
import datetime
import logging
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scheduler")


HERE = Path(__file__).parent
AGENT_SCRIPT = HERE / "agent.py"


def run_pipeline():
    """执行一次 Pipeline"""
    log.info("=" * 50)
    log.info("定时任务触发 — 开始运行 Pipeline")
    log.info("=" * 50)
    try:
        # 用同步调用,这样日志能完整看到
        result = subprocess.run(
            [sys.executable, str(AGENT_SCRIPT)],
            cwd=HERE,
            capture_output=False,  # 让 agent.py 的日志直接出现在终端
            timeout=600,            # 10 分钟超时,正常远远跑不到
        )
        log.info("Pipeline 退出码: %d", result.returncode)
    except subprocess.TimeoutExpired:
        log.error("Pipeline 超时(>10min)被强制终止")
    except Exception as e:
        log.error("Pipeline 启动失败: %s", e)


def main():
    scheduler = BlockingScheduler(timezone="Asia/Shanghai")

    # 每天 08:00 跑一次
    scheduler.add_job(
        run_pipeline,
        trigger="cron",
        hour=8, minute=0,
        id="daily_pipeline",
        misfire_grace_time=3600,  # 错过一小时内补跑
    )

    log.info("定时任务已注册:")
    for job in scheduler.get_jobs():
        log.info("  %s -> 下次运行: %s", job.id, job.next_run_time)

    log.info("按 Ctrl-C 退出。")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("收到退出信号,停止调度。")


if __name__ == "__main__":
    main()
