# -*- coding: utf-8 -*-
"""
신호 성적표(주간 백테스트): 봇이 과거에 포착한 티커들이 실제로 올랐는지 자동 검증.
  - narrative_history.jsonl 에서 티커별 '최초 포착일' 을 찾고
  - 포착일 종가 -> 현재 종가 수익률 + 같은 기간 SPY 대비 초과수익(알파) 계산
  - 연차별(1주/2주/1개월) 버킷 통계 + 베스트/워스트 -> 텔레그램 리포트

  이 피드백 루프가 있어야 "봇 신호가 진짜 유효한가" 를 스스로 증명하고
  점수 공식을 개선할 근거가 생긴다. (전문 퀀트 파이프라인의 필수 요소)
"""

import json
import sys
from datetime import datetime, timedelta

import narrative_scout as ns

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

MIN_AGE_DAYS = 5      # 포착 후 최소 며칠 지나야 평가 대상
MAX_AGE_DAYS = 45     # 너무 오래된 신호는 제외
BENCH = "SPY"


def first_seen_map():
    """이력에서 티커별 최초 포착일(YYYY-MM-DD)."""
    m = {}
    if not ns.os.path.exists(ns.HISTORY_PATH):
        return m
    with open(ns.HISTORY_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            day = (rec.get("ts") or "")[:10]
            for t in rec.get("tickers", []):
                t = ns.clean_ticker(t)          # 과거 이력의 오염된 티커 정제
                if t and day and (t not in m or day < m[t]):
                    m[t] = day
    return m


def get_closes(tickers):
    """티커 -> 종가 시계열(Series). 실패 티커는 제외."""
    out = {}
    try:
        import yfinance as yf
        data = yf.download(tickers, period="6mo", interval="1d",
                           progress=False, group_by="ticker", threads=True)
    except Exception as e:  # noqa: BLE001
        print(f"[경고] 가격 조회 실패: {e}")
        return out
    for t in tickers:
        try:
            close = data["Close"].dropna() if len(tickers) == 1 else data[t]["Close"].dropna()
            if close is not None and not close.empty:
                out[t] = close
        except Exception:  # noqa: BLE001
            continue
    return out


def price_on(close, day):
    """해당 날짜(휴장이면 직전 거래일) 종가."""
    try:
        sub = close[close.index.strftime("%Y-%m-%d") <= day]
        return float(sub.iloc[-1]) if len(sub) else None
    except Exception:  # noqa: BLE001
        return None


def bucket_of(age):
    if age >= 30:
        return "1개월+"
    if age >= 14:
        return "2주+"
    return "1주+"


def main():
    secrets = ns.load_json(ns.SECRETS_PATH, default={})
    tg_token = ns.get_secret("TELEGRAM_BOT_TOKEN", "bot_token", secrets)
    tg_chat = ns.get_secret("TELEGRAM_CHAT_ID", "chat_id", secrets)
    if not all([tg_token, tg_chat]):
        print("[오류] 텔레그램 키가 없습니다.")
        sys.exit(1)

    today = datetime.now(ns.KST)
    fs = first_seen_map()
    targets = {}
    for t, day in fs.items():
        try:
            age = (today - datetime.fromisoformat(day).replace(tzinfo=ns.KST)).days
        except ValueError:
            continue
        if MIN_AGE_DAYS <= age <= MAX_AGE_DAYS:
            targets[t] = (day, age)

    print(f"평가 대상 티커: {len(targets)}개 (포착 {MIN_AGE_DAYS}~{MAX_AGE_DAYS}일 경과)")
    if len(targets) < 3:
        print("데이터가 아직 부족합니다. (누적되면 자동으로 리포트 시작)")
        return

    closes = get_closes(list(targets) + [BENCH])
    spy = closes.get(BENCH)

    results = []
    for t, (day, age) in targets.items():
        close = closes.get(t)
        if close is None:
            continue
        p0, p1 = price_on(close, day), float(close.iloc[-1])
        if not p0 or not p1:
            continue
        ret = (p1 / p0 - 1) * 100
        alpha = None
        if spy is not None:
            s0, s1 = price_on(spy, day), float(spy.iloc[-1])
            if s0 and s1:
                alpha = ret - (s1 / s0 - 1) * 100
        results.append({"ticker": t, "day": day, "age": age,
                        "ret": ret, "alpha": alpha,
                        "bucket": bucket_of(age)})

    if not results:
        print("평가 가능한 가격 데이터 없음.")
        return

    results.sort(key=lambda r: r["ret"], reverse=True)
    n = len(results)
    avg = sum(r["ret"] for r in results) / n
    wins = len([r for r in results if r["ret"] > 0])
    alphas = [r["alpha"] for r in results if r["alpha"] is not None]
    avg_alpha = sum(alphas) / len(alphas) if alphas else None

    lines = ["🟢🟢🟢 <b>성적표 봇</b> 🟢🟢🟢",
             f"📋 <b>신호 성적표</b> (자동 백테스트)  <i>{today:%Y-%m-%d} KST</i>",
             f"<i>최근 {MAX_AGE_DAYS}일 내 첫 포착 · {MIN_AGE_DAYS}일 이상 경과 {n}개 티커</i>",
             "",
             f"전체: 평균 <b>{avg:+.1f}%</b> · 승률 <b>{wins}/{n} ({wins / n * 100:.0f}%)</b>"
             + (f" · SPY대비 <b>{avg_alpha:+.1f}%p</b>" if avg_alpha is not None else "")]

    for bname in ("1개월+", "2주+", "1주+"):
        bs = [r for r in results if r["bucket"] == bname]
        if not bs:
            continue
        bavg = sum(r["ret"] for r in bs) / len(bs)
        bwin = len([r for r in bs if r["ret"] > 0])
        lines.append(f"· {bname}: 평균 {bavg:+.1f}% · 승률 {bwin}/{len(bs)}")

    lines.append("")
    lines.append("🏆 <b>베스트</b>")
    for r in results[:5]:
        lines.append(f"• <b>${r['ticker']}</b> {r['ret']:+.1f}%  "
                     f"<i>({r['day']} 포착 · {r['age']}일)</i>")
    worst = [r for r in results[-3:] if r["ret"] < 0]
    if worst:
        lines.append("")
        lines.append("💀 <b>워스트</b>")
        for r in reversed(worst):
            lines.append(f"• <b>${r['ticker']}</b> {r['ret']:+.1f}%  "
                         f"<i>({r['day']} 포착 · {r['age']}일)</i>")

    msg = "\n".join(lines)
    for chunk in ns.split_message(msg, 3800):
        ns.send_telegram(tg_token, tg_chat, chunk)
    print(f"성적표 전송 완료 ({n}개 티커, 평균 {avg:+.1f}%).")


if __name__ == "__main__":
    main()
