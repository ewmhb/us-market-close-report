"""Cloud Run Job entry point for the US market close report."""

from datetime import datetime, timedelta
import os
import subprocess
import sys
from zoneinfo import ZoneInfo

from google.cloud import storage
import pandas_market_calendars as mcal


NEW_YORK = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
REPORT_KIND = "us"


def latest_closed_market_date(now: datetime):
    """Return the latest NYSE session whose regular close has already passed."""
    first_date = (now - timedelta(days=7)).date()
    last_date = now.date()
    schedule = mcal.get_calendar("NYSE").schedule(
        start_date=first_date,
        end_date=last_date,
    )
    closed = schedule[schedule["market_close"] <= datetime.now(UTC)]
    if closed.empty:
        return None
    return closed.index[-1].date()


def main() -> None:
    report_bucket_name = os.environ["REPORT_BUCKET"]
    state_bucket_name = os.environ["STATE_BUCKET"]
    now = datetime.now(NEW_YORK)
    test_send = os.getenv("TEST_SEND", "false").lower() == "true"
    closed_date = latest_closed_market_date(now)
    if closed_date is None:
        print("최근 7일 안에 마감된 NYSE 거래일이 없어 종료합니다.")
        return
    market_date = closed_date.strftime("%Y%m%d")
    client = storage.Client()
    report_bucket = client.bucket(report_bucket_name)
    state_bucket = client.bucket(state_bucket_name)
    marker = state_bucket.blob(f"state/{REPORT_KIND}/{market_date}.sent")
    news_cache_blob = state_bucket.blob(f"state/{REPORT_KIND}/news-cache.json")

    if marker.exists(client) and not test_send:
        print(f"{market_date} 리포트는 이미 발송되었습니다.")
        return
    if test_send:
        print("본인 전용 테스트 실행: 기존 발송 마커를 무시하고 새 마커는 기록하지 않습니다.")
    os.makedirs("work", exist_ok=True)
    if news_cache_blob.exists(client):
        news_cache_blob.download_to_filename("work/news-cache.json")
    try:
        result = subprocess.run(
            [sys.executable, "scripts/generate_report.py"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            print(exc.stdout, end="", flush=True)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr, end="", flush=True)
        raise
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("리포트 생성 요약문이 없습니다.")
    for line in lines:
        print(line)
    summary = lines[-1]
    if test_send:
        summary = "[본인 전용 테스트] " + summary

    report_blob = report_bucket.blob(f"{REPORT_KIND}/index.html")
    report_blob.cache_control = "no-cache, max-age=0"
    report_blob.upload_from_filename("site/index.html", content_type="text/html; charset=utf-8")
    report_blob.make_public()
    if os.path.exists("work/news-cache.json"):
        news_cache_blob.upload_from_filename("work/news-cache.json", content_type="application/json")
    report_url = f"https://storage.googleapis.com/{report_bucket_name}/{REPORT_KIND}/index.html"

    env = os.environ.copy()
    env["REPORT_URL"] = report_url
    env["REPORT_SUMMARY"] = summary
    subprocess.run([sys.executable, "scripts/send_kakao.py"], check=True, env=env)
    if not test_send:
        marker.upload_from_string(now.isoformat(), content_type="text/plain")
    print(f"완료: {report_url}")


if __name__ == "__main__":
    main()
