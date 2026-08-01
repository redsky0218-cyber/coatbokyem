# -*- coding: utf-8 -*-
"""
미국 주식 실시간 감시 -> 텔레그램 알림 (15분마다, 장중)
  1) 주봉 볼린저밴드 하단/중단 신호 (20주 SMA, 2σ)
  2) 급락 신호: 전일종가 대비 -N% 이상 하락 (기본 -10%)
- 데이터: yfinance (무료, 약 15분 지연), 프리/정규/애프터장 현재가 반영
- 중복방지(state.json):
    * 볼린저: 같은 신호구간 머무는 동안 1회만
    * 급락: 하루 1회만
- 거래시간(프리 04:00 ~ 애프터 20:00 ET, 월~금)에만 검사
"""

import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
SECRETS_PATH = os.path.join(BASE_DIR, "secrets.json")
NY_TZ = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_secrets(cfg):
    """텔레그램 토큰/chat_id 획득 우선순위:
    1) 환경변수(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)  <- 클라우드(GitHub Actions)
    2) secrets.json (로컬 전용, git에 올리지 않음)
    3) config.json 의 telegram 블록 (하위호환)
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return token, chat_id

    if os.path.exists(SECRETS_PATH):
        try:
            with open(SECRETS_PATH, "r", encoding="utf-8") as f:
                s = json.load(f)
            token = token or s.get("bot_token")
            chat_id = chat_id or s.get("chat_id")
            if token and chat_id:
                return token, chat_id
        except Exception:  # noqa: BLE001
            pass

    tg = cfg.get("telegram", {})
    return token or tg.get("bot_token"), chat_id or tg.get("chat_id")


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def is_market_open(now_ny=None):
    now_ny = now_ny or datetime.now(NY_TZ)
    if now_ny.weekday() >= 5:
        return False
    minutes = now_ny.hour * 60 + now_ny.minute
    return 4 * 60 <= minutes < 20 * 60


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_live_price(tk):
    try:
        intr = tk.history(period="1d", interval="1m", prepost=True)
        if intr is not None and not intr.empty:
            return float(intr["Close"].iloc[-1])
    except Exception:  # noqa: BLE001
        pass
    try:
        return float(tk.fast_info.last_price)
    except Exception:  # noqa: BLE001
        return None


def get_prev_close(tk):
    try:
        pc = float(tk.fast_info.previous_close)
        if pc > 0:
            return pc
    except Exception:  # noqa: BLE001
        pass
    try:
        d = tk.history(period="7d", interval="1d")
        if d is not None and len(d) >= 2:
            return float(d["Close"].iloc[-2])
    except Exception:  # noqa: BLE001
        pass
    return None


def evaluate_ticker(ticker, period, std_mult, prox_pct):
    """볼린저 신호 + 일간등락률 계산. 데이터 부족 시 볼린저만 None 처리."""
    tk = yf.Ticker(ticker)

    live = get_live_price(tk)
    prev_close = get_prev_close(tk)
    daily_change = None
    if live is not None and prev_close:
        daily_change = (live / prev_close - 1) * 100.0

    result = {
        "ticker": ticker,
        "close": live,
        "daily_change": daily_change,
        "signal": None,
        "severity": 0,   # 0=없음, 1=중단권, 2=하단권
        "middle": None,
        "lower": None,
        "bollinger_ok": False,
        "error": None,
    }

    weekly = tk.history(period="3y", interval="1wk", auto_adjust=True)
    if weekly is None or weekly.empty or len(weekly) < period + 1:
        # 볼린저 계산 불가(신규상장 등) -> 급락감지는 계속 유효
        if live is None:
            result["error"] = "시세 조회 실패"
        return result

    close = weekly["Close"].astype(float).copy()
    if live is not None:
        close.iloc[-1] = live
    last_close = float(close.iloc[-1])

    ma = close.rolling(period).mean()
    std = close.rolling(period).std()
    mid = float(ma.iloc[-1])
    lo = float((ma - std_mult * std).iloc[-1])

    prox = prox_pct / 100.0
    label = None
    severity = 0
    if last_close <= lo:
        label = ("🔴", "하단밴드 이탈 (과매도)")
        severity = 2
    elif last_close <= lo * (1 + prox):
        label = ("🟠", "하단밴드 근접")
        severity = 2
    elif mid * (1 - prox) <= last_close <= mid * (1 + prox):
        label = ("🟢", "중단밴드 근접")
        severity = 1
    elif last_close < mid:
        label = ("🟡", "중단밴드 하향 이탈")
        severity = 1

    result.update({
        "signal": label,
        "severity": severity,
        "middle": mid,
        "lower": lo,
        "bollinger_ok": True,
    })
    return result


def main():
    force = "--force" in sys.argv

    cfg = load_config()
    token, chat_id = get_secrets(cfg)
    tickers = cfg["tickers"]
    names = cfg.get("names", {})
    b = cfg["bollinger"]
    drop_pct = cfg.get("crash", {}).get("drop_pct", 10.0)

    if not token or not chat_id or "붙여넣기" in str(token):
        print("[오류] 텔레그램 토큰/chat_id 를 찾을 수 없습니다. "
              "환경변수 또는 secrets.json 를 확인하세요.")
        sys.exit(1)

    now_ny = datetime.now(NY_TZ)
    if not force and not is_market_open(now_ny):
        print(f"[{now_ny:%Y-%m-%d %H:%M} ET] 미국 거래시간 아님 -> 검사 건너뜀.")
        return

    today = datetime.now(KST).strftime("%Y-%m-%d")
    state = load_state()
    zone_state = state.setdefault("zone", {})
    crash_state = state.setdefault("crash", {})

    boll_hits = []
    crash_hits = []
    errors = []

    for t in tickers:
        try:
            r = evaluate_ticker(t, b["period"], b["std_mult"], b["proximity_pct"])
        except Exception as e:  # noqa: BLE001
            errors.append(f"{t}: {e}")
            continue
        if r.get("error"):
            errors.append(f"{t}: {r['error']}")
            continue

        nm = names.get(t, t)

        # --- 볼린저 신호: 밴드권에 '새로 진입'하거나 '더 깊은 권역'으로 갈 때만 1회 ---
        #   severity 0=없음, 1=중단권(근접/이탈), 2=하단권(근접/이탈)
        #   같은 권역에 머무는 동안(근접<->이탈 오가도) 재알림 안 함. 회복(하락권->상단)도 조용.
        cur_label = r["signal"][1] if r["signal"] else "none"
        sev = r["severity"]
        if r["bollinger_ok"]:
            prev_sev = zone_state.get(t, 0)
            if not isinstance(prev_sev, int):
                prev_sev = 0
            if sev > 0 and sev > prev_sev:
                boll_hits.append(r)
            zone_state[t] = sev

        # --- 급락 신호(하루 1회) ---
        dc = r["daily_change"]
        is_crash = dc is not None and dc <= -abs(drop_pct)
        if is_crash and crash_state.get(t) != today:
            crash_hits.append(r)
            crash_state[t] = today
        elif not is_crash and crash_state.get(t) == today:
            # 반등해서 회복되면 리셋 (다음날/재하락 대비)
            pass

        dc_str = f"{dc:+.1f}%" if dc is not None else "N/A"
        print(f"검사 {t}({nm}): 현재 {r['close']} 등락 {dc_str} / 볼린저 {cur_label}"
              f"{' [급락!]' if is_crash else ''}")
        time.sleep(0.3)

    save_state(state)

    # --- 알림 전송 ---
    now_kst = datetime.now(KST)
    blocks = []

    if crash_hits:
        lines = [f"⚠️ <b>급락 경보 (전일대비 -{abs(drop_pct):.0f}% 이상)</b>"]
        for r in crash_hits:
            nm = names.get(r["ticker"], r["ticker"])
            lines.append(f"💥 <b>{r['ticker']}</b> {nm} — {r['daily_change']:+.1f}% (${r['close']:.2f})")
        blocks.append("\n".join(lines))

    if boll_hits:
        lines = ["📊 <b>주봉 볼린저밴드 신호</b>"]
        for r in boll_hits:
            nm = names.get(r["ticker"], r["ticker"])
            emoji, label = r["signal"]
            mid_gap = (r["close"] - r["middle"]) / r["middle"] * 100
            low_gap = (r["close"] - r["lower"]) / r["lower"] * 100
            lines.append(
                f"{emoji} <b>{r['ticker']}</b> {nm} — {label}\n"
                f"   현재 ${r['close']:.2f} | 중단 ${r['middle']:.2f} ({mid_gap:+.1f}%) "
                f"| 하단 ${r['lower']:.2f} ({low_gap:+.1f}%)"
            )
        blocks.append("\n".join(lines))

    if blocks:
        msg = f"<i>{now_kst:%Y-%m-%d %H:%M} KST</i>\n\n" + "\n\n".join(blocks)
        try:
            send_telegram(token, chat_id, msg)
            print(f"\n텔레그램 전송 완료 (급락 {len(crash_hits)}, 볼린저 {len(boll_hits)}).")
        except Exception as e:  # noqa: BLE001
            print(f"\n[오류] 텔레그램 전송 실패: {e}")
    else:
        print("\n새 신호 없음. (알림 미전송)")

    if errors:
        print("\n[경고] 처리 실패:")
        for e in errors:
            print("  -", e)


if __name__ == "__main__":
    main()
