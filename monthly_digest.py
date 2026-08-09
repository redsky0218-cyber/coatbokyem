# -*- coding: utf-8 -*-
"""
월간 다이제스트: 지난 30일간 쌓인 내러티브 이력을 종합 정리.
  - narrative_history.jsonl 을 읽어 최근 N일치만 필터
  - 티커(기업)별 / 기술·테마별 언급 횟수 집계
  - 집계 결과를 Gemini 에게 던져 '이달의 종합 리포트' 작성
  - 텔레그램으로 전송

  narrative_scout.py 의 유틸(경로/시크릿/텔레그램)을 그대로 재사용.
"""

import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta

import requests

import narrative_scout as ns

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

LOOKBACK_DAYS = 30
TOP_TICKERS = 25
TOP_THEMES = 20


# ----------------------------- 이력 로딩/집계 ----------------------------- #
def load_history(days):
    cutoff = datetime.now(ns.KST) - timedelta(days=days)
    rows = []
    if not ns.os.path.exists(ns.HISTORY_PATH):
        return rows
    with open(ns.HISTORY_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = datetime.fromisoformat(rec["ts"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if ts >= cutoff:
                rows.append(rec)
    return rows


def tally(rows):
    ticker_counts = Counter()
    theme_counts = Counter()
    ticker_days = {}          # 티커 -> 등장한 날짜 집합 (지속성 측정)
    for rec in rows:
        day = rec["ts"][:10]
        for t in rec.get("tickers", []):
            t = (t or "").strip().upper()
            if not t:
                continue
            ticker_counts[t] += 1
            ticker_days.setdefault(t, set()).add(day)
        name = (rec.get("name") or "").strip()
        if name:
            theme_counts[name] += 1
    return ticker_counts, theme_counts, ticker_days


# ----------------------------- Gemini 종합 ----------------------------- #
DIGEST_SYSTEM = """\
You are an emerging-technology equity strategist writing a MONTHLY digest.
You are given aggregated statistics from a full month of automated narrative
detection across technology and investing communities: how many times each
US-listed ticker and each theme was surfaced, and on how many distinct days.

Write a concise Korean monthly report that helps an investor see the big picture:
- 이달의 핵심 테마 Top 5 (무엇이 왜 계속 언급됐는지, 한 줄 근거 포함)
- 가장 많이·꾸준히 언급된 종목 (티커) 과 그 이유 — 특히 여러 날에 걸쳐 반복 등장한
  종목을 '지속 신호'로 강조. 소형·중형주도 놓치지 말 것.
- 이번 달 새로 부상한(신규) 테마 vs 식어가는 테마가 있으면 짚어줄 것.
- 마지막에 '주목 리스트' 5~8개 티커를 한 줄 코멘트와 함께 정리.

규칙: 통계에 없는 종목/테마를 지어내지 말 것. 티커는 영문 그대로. 과장·펌핑 금지.
간결하게. 전체 900자 내외.
"""


def build_digest_prompt(rows, ticker_counts, theme_counts, ticker_days):
    lines = [f"[집계 기간] 최근 {LOOKBACK_DAYS}일, 총 {len(rows)}건의 내러티브 감지", ""]
    lines.append("[티커별 언급 횟수 / 등장 일수]")
    for t, c in ticker_counts.most_common(TOP_TICKERS):
        lines.append(f"- {t}: {c}회 / {len(ticker_days.get(t, set()))}일")
    lines.append("")
    lines.append("[테마(내러티브)별 언급 횟수]")
    for name, c in theme_counts.most_common(TOP_THEMES):
        lines.append(f"- ({c}회) {name}")
    return "\n".join(lines)


def summarize_with_gemini(cfg, api_key, prompt_text):
    lc = cfg.get("llm", {})
    model = lc.get("model", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "system_instruction": {"parts": [{"text": DIGEST_SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "maxOutputTokens": lc.get("max_tokens", 12000),
            "temperature": 0.5,
            "thinkingConfig": {"thinkingBudget": lc.get("thinking_budget", 2048)},
        },
    }
    resp = requests.post(url, params={"key": api_key}, json=body, timeout=120)
    if resp.status_code != 200:
        print(f"[오류] Gemini 호출 실패 {resp.status_code}: {resp.text[:400]}")
        return None
    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        print("[오류] Gemini 응답 파싱 실패:", str(data)[:400])
        return None
    um = data.get("usageMetadata", {})
    print(f"[Gemini] 입력 {um.get('promptTokenCount', '?')} / "
          f"출력 {um.get('candidatesTokenCount', '?')} 토큰")
    return text.strip()


# ----------------------------- 텔레그램 (HTML escape) ----------------------------- #
def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_stats_block(ticker_counts, ticker_days):
    """AI 요약과 별개로, 원자료(Top 티커 순위표)도 함께 보냄."""
    lines = ["", "📊 <b>티커(기업) 언급 순위 (Top 20)</b>"]
    for i, (t, c) in enumerate(ticker_counts.most_common(20), 1):
        d = len(ticker_days.get(t, set()))
        lines.append(f"{i}. <b>${esc(t)}</b> — {c}회 ({d}일)")
    return "\n".join(lines)


def build_themes_block(theme_counts):
    """기술·테마 언급 순위 원자료."""
    lines = ["", "🧬 <b>기술·테마 언급 순위 (Top 15)</b>"]
    for i, (name, c) in enumerate(theme_counts.most_common(15), 1):
        lines.append(f"{i}. ({c}회) {esc(name)}")
    return "\n".join(lines)


# ----------------------------- 메인 ----------------------------- #
def main():
    cfg = ns.load_json(ns.CONFIG_PATH)
    secrets = ns.load_json(ns.SECRETS_PATH, default={})

    gemini_key = ns.get_secret("GEMINI_API_KEY", "gemini_api_key", secrets)
    tg_token = ns.get_secret("TELEGRAM_BOT_TOKEN", "bot_token", secrets)
    tg_chat = ns.get_secret("TELEGRAM_CHAT_ID", "chat_id", secrets)
    if not all([gemini_key, tg_token, tg_chat]):
        print("[오류] GEMINI/TELEGRAM 키가 없습니다.")
        sys.exit(1)

    rows = load_history(LOOKBACK_DAYS)
    print(f"최근 {LOOKBACK_DAYS}일 이력 {len(rows)}건 로드")
    if not rows:
        print("이력이 없습니다. (아직 데이터가 쌓이지 않음) 종료.")
        return

    ticker_counts, theme_counts, ticker_days = tally(rows)
    print(f"티커 {len(ticker_counts)}종 / 테마 {len(theme_counts)}종 집계")

    prompt_text = build_digest_prompt(rows, ticker_counts, theme_counts, ticker_days)
    summary = summarize_with_gemini(cfg, gemini_key, prompt_text)
    if not summary:
        print("요약 생성 실패. 종료.")
        return

    now = datetime.now(ns.KST)
    header = f"🗓️ <b>월간 내러티브 다이제스트</b>  <i>{now:%Y-%m-%d} KST</i>"
    period = f"<i>최근 {LOOKBACK_DAYS}일 · 총 {len(rows)}건 감지</i>"
    stats = build_stats_block(ticker_counts, ticker_days)
    themes = build_themes_block(theme_counts)
    msg = f"{header}\n{period}\n\n{esc(summary)}\n{stats}\n{themes}"

    # 텔레그램 4096자 제한 대응 분할
    for chunk in _split(msg, 3800):
        ns.send_telegram(tg_token, tg_chat, chunk)
    print("텔레그램 전송 완료.")


def _split(text, limit):
    if len(text) <= limit:
        return [text]
    parts, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            parts.append(buf)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        parts.append(buf)
    return parts


if __name__ == "__main__":
    main()
