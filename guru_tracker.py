# -*- coding: utf-8 -*-
"""
거장(슈퍼투자자) 13F 포트폴리오 추적 -> 텔레그램
  - SEC EDGAR 에서 각 거장의 최신 13F 보유내역을 읽고 (무료·키 불필요)
  - 새 공시가 뜨면: 신규매수/추가/청산 을 즉시 알림
  - 매일: '추정 매수가(매수 분기 평균 주가) vs 현재가' 비교
      -> 현재가가 거장 추정매수가보다 낮으면 🟢 (거장보다 싸게 살 기회)

  주의: 13F 는 분기말 보유량만 공개하므로 정확한 매수가는 알 수 없다.
        '매수한 분기의 평균 종가' 를 추정 매수가로 사용한다 (업계 관행).

  CUSIP -> 티커 변환: OpenFIGI 무료 API (키 불필요, 분당 25요청 제한 -> 캐시 사용)
"""

import json
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import requests

import narrative_scout as ns

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

GURU_CONFIG_PATH = ns.os.path.join(ns.BASE_DIR, "guru_config.json")
GURU_STATE_PATH = ns.os.path.join(ns.BASE_DIR, "guru_state.json")
SEC_HEADERS = {"User-Agent": "guru-tracker/1.0 (redsky0218@gmail.com)"}


# ----------------------------- EDGAR ----------------------------- #
def list_13f(cik, limit=13):
    """해당 CIK 의 13F-HR 목록 (최신순). [{entity, acc, filed, period}]"""
    url = f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"
    d = requests.get(url, headers=SEC_HEADERS, timeout=30).json()
    rec = d.get("filings", {}).get("recent", {})
    out = []
    for form, acc, fdate, rdate in zip(rec.get("form", []),
                                       rec.get("accessionNumber", []),
                                       rec.get("filingDate", []),
                                       rec.get("reportDate", [])):
        if form == "13F-HR":
            out.append({"entity": d.get("name", ""), "acc": acc,
                        "filed": fdate, "period": rdate})
            if len(out) >= limit:
                break
    return out


def latest_13f(cik):
    """해당 CIK 의 최신 13F-HR 접수번호/공시일/분기말."""
    lst = list_13f(cik, limit=1)
    return lst[0] if lst else None


# ------------------- 매수시점 추정 (과거 13F 최초 등장 분기) ------------------- #
def quarter_label(period):
    """'2026-06-30' -> '26.2Q'"""
    try:
        d = datetime.fromisoformat(period)
        return f"{d.year % 100}.{(d.month - 1) // 3 + 1}Q"
    except (ValueError, TypeError):
        return period or ""


def quarter_months(period):
    """'2026-06-30' -> '4~6월'"""
    try:
        d = datetime.fromisoformat(period)
        return {1: "1~3월", 2: "4~6월", 3: "7~9월", 4: "10~12월"}[(d.month - 1) // 3 + 1]
    except (ValueError, TypeError):
        return ""


def backfill_first_seen(cik, gs, quarters=12):
    """과거 13F 를 거슬러 각 종목(cusip)이 처음 등장한 분기 기록 (1회 실행 후 캐시).
    보유 시작 분기 ≈ 대략적 매수 시점."""
    if gs.get("first_seen_done"):
        return
    filings = list_13f(cik, limit=quarters)
    if not filings:
        return
    print(f"  매수시점 백필: {gs.get('name')} — 과거 {len(filings)}개 분기 조회 중…")
    fs = {}
    for f in reversed(filings):              # 과거 -> 현재 순으로
        try:
            holds = fetch_holdings(cik, f["acc"])
        except Exception as e:  # noqa: BLE001
            print(f"    [경고] {f['period']} 조회 실패: {e}")
            continue
        for cusip in holds:
            fs.setdefault(cusip, f["period"])
        time.sleep(0.4)
    gs["first_seen"] = fs
    gs["first_seen_oldest"] = filings[-1]["period"]
    gs["first_seen_done"] = True
    print(f"    -> {len(fs)}개 종목 매수시점 기록")


def since_label(e, gs):
    """추적 항목의 보유 시작(매수시점) 라벨."""
    if e.get("type") == "new":
        return f"{quarter_months(e.get('period'))} 매수"
    s = e.get("since")
    if not s:
        return ""
    if s == gs.get("first_seen_oldest"):
        return "3년+ 보유"
    return f"{quarter_label(s)}~ 보유"


def attach_since(gs, cache):
    """tracked 항목에 매수시점(since) 붙이기 (기존 상태 복구 겸용)."""
    fs = gs.get("first_seen") or {}
    if not fs:
        return
    rev = {}
    for c, t in cache.items():
        if t:
            rev.setdefault(t, c)
    for e in gs.get("tracked", []):
        if "since" not in e:
            c = e.get("cusip") or rev.get(e.get("ticker"))
            if c and c in fs:
                e["since"] = fs[c]


def fetch_holdings(cik, acc):
    """13F infotable XML 을 찾아 보유내역 파싱. {cusip: {issuer, shares, value}}"""
    base = (f"https://www.sec.gov/Archives/edgar/data/"
            f"{int(cik)}/{acc.replace('-', '')}")
    idx = requests.get(base + "/index.json", headers=SEC_HEADERS, timeout=30).json()
    names = [it.get("name", "") for it in idx.get("directory", {}).get("item", [])]
    xmls = [n for n in names if n.lower().endswith(".xml")]
    for fn in xmls:
        time.sleep(0.3)
        try:
            content = requests.get(f"{base}/{fn}", headers=SEC_HEADERS,
                                   timeout=30).content
        except Exception:  # noqa: BLE001
            continue
        if b"informationTable" not in content and b"infoTable" not in content:
            continue
        rows = parse_infotable(content)
        if rows:
            return rows
    return {}


def parse_infotable(content):
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return {}

    def sub(el, tag):
        for c in el.iter():
            if c.tag.endswith(tag):
                return (c.text or "").strip()
        return ""

    agg = {}
    for it in root.iter():
        if not it.tag.endswith("infoTable"):
            continue
        if sub(it, "putCall"):          # 옵션(풋/콜)은 제외
            continue
        cusip = sub(it, "cusip").upper()
        if not cusip:
            continue
        try:
            value = float(sub(it, "value") or 0)
            shares = float(sub(it, "sshPrnamt") or 0)
        except ValueError:
            continue
        a = agg.setdefault(cusip, {"issuer": sub(it, "nameOfIssuer"),
                                   "shares": 0.0, "value": 0.0})
        a["shares"] += shares
        a["value"] += value
    return agg


# ----------------------------- CUSIP -> 티커 ----------------------------- #
def map_tickers(cusips, cache):
    """OpenFIGI 무료 API. 캐시에 없는 것만 10개씩 배치 변환."""
    todo = [c for c in dict.fromkeys(cusips) if c not in cache]
    for i in range(0, len(todo), 10):
        batch = todo[i:i + 10]
        try:
            resp = requests.post(
                "https://api.openfigi.com/v3/mapping",
                json=[{"idType": "ID_CUSIP", "idValue": c} for c in batch],
                timeout=20)
            if resp.status_code == 429:
                time.sleep(15)
                resp = requests.post(
                    "https://api.openfigi.com/v3/mapping",
                    json=[{"idType": "ID_CUSIP", "idValue": c} for c in batch],
                    timeout=20)
            resp.raise_for_status()
            for c, item in zip(batch, resp.json()):
                data = item.get("data") or []
                t = (data[0].get("ticker") or "").replace("/", "-") if data else None
                cache[c] = t
        except Exception as e:  # noqa: BLE001
            print(f"[경고] OpenFIGI 변환 실패: {e}")
            break
        time.sleep(3)
    return cache


# ----------------------------- 가격 ----------------------------- #
def quarter_avg_prices(tickers, period_end):
    """매수 분기(분기말 period_end 기준 3개월)의 평균 종가 = 추정 매수가."""
    if not tickers:
        return {}
    end = datetime.fromisoformat(period_end)
    start = end - timedelta(days=92)
    out = {}
    try:
        import yfinance as yf
        data = yf.download(tickers, start=start.strftime("%Y-%m-%d"),
                           end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
                           interval="1d", progress=False,
                           group_by="ticker", threads=True)
    except Exception as e:  # noqa: BLE001
        print(f"[경고] 분기 평균가 조회 실패: {e}")
        return out
    for t in tickers:
        try:
            close = data[t]["Close"].dropna() if len(tickers) > 1 else data["Close"].dropna()
            if close is not None and len(close):
                out[t] = float(close.mean())
        except Exception:  # noqa: BLE001
            continue
    return out


def current_prices(tickers):
    if not tickers:
        return {}
    out = {}
    try:
        import yfinance as yf
        data = yf.download(tickers, period="5d", interval="1d",
                           progress=False, group_by="ticker", threads=True)
    except Exception as e:  # noqa: BLE001
        print(f"[경고] 현재가 조회 실패: {e}")
        return out
    for t in tickers:
        try:
            close = data[t]["Close"].dropna() if len(tickers) > 1 else data["Close"].dropna()
            if close is not None and len(close):
                out[t] = float(close.iloc[-1])
        except Exception:  # noqa: BLE001
            continue
    return out


# ----------------------------- 메인 ----------------------------- #
def process_guru(g, state, cfg, alerts):
    """한 거장 처리: 새 공시면 diff 분석 + tracked 갱신."""
    cik = str(g["cik"])
    info = latest_13f(cik)
    time.sleep(0.3)
    if not info:
        print(f"[경고] {g['name']}: 13F 없음")
        return
    gs = state["gurus"].setdefault(cik, {})
    gs["name"] = g["name"]

    if gs.get("acc") == info["acc"]:
        return                                   # 새 공시 없음 -> 그대로 추적

    print(f"  새 13F: {g['name']} ({info['filed']} 공시, {info['period']} 분기)")
    holdings = fetch_holdings(cik, info["acc"])
    if not holdings:
        print(f"[경고] {g['name']}: 보유내역 파싱 실패")
        return

    total = sum(h["value"] for h in holdings.values()) or 1.0
    prev = gs.get("holdings", {})
    first_run = not prev                # 첫 수집이면 이전 분기가 없어 diff 불가

    # diff 분류
    news, adds, exits = [], [], []
    if not first_run:
        for cusip, h in holdings.items():
            if cusip not in prev:
                news.append((cusip, h))
            elif h["shares"] > prev[cusip]["shares"] * 1.05:
                adds.append((cusip, h,
                             h["shares"] / max(prev[cusip]["shares"], 1) - 1))
        for cusip, h in prev.items():
            if cusip not in holdings:
                exits.append(h.get("issuer", cusip))

    news.sort(key=lambda x: x[1]["value"], reverse=True)
    adds.sort(key=lambda x: x[1]["value"], reverse=True)

    # 티커 변환 대상: 신규/추가 + 상위 보유
    top_n = cfg.get("top_holdings", 8)
    top = sorted(holdings.items(), key=lambda x: x[1]["value"], reverse=True)[:top_n]
    want = [c for c, _ in news[:10]] + [c for c, _, _ in adds[:10]] + [c for c, _ in top]
    cache = state.setdefault("cusip_ticker", {})
    map_tickers(want, cache)

    # 추정 매수가(분기 평균)
    tickers = [cache.get(c) for c in want]
    tickers = [t for t in dict.fromkeys(tickers) if t]
    qavg = quarter_avg_prices(tickers, info["period"])

    def entry(cusip, h, typ):
        t = cache.get(cusip)
        if not t:
            return None
        return {"ticker": t, "type": typ, "cusip": cusip,
                "ref": round(qavg.get(t) or 0, 2) or None,
                "weight": round(h["value"] / total * 100, 1),
                "issuer": h.get("issuer", ""), "period": info["period"]}

    tracked = []
    for c, h in news[:10]:
        e = entry(c, h, "new")
        if e:
            tracked.append(e)
    for c, h, inc in adds[:10]:
        e = entry(c, h, "add")
        if e:
            e["inc"] = round(inc * 100)
            tracked.append(e)
    if len(tracked) < 5:                        # 신규/추가가 적으면 상위보유로 채움
        have = {e["ticker"] for e in tracked}
        for c, h in top:
            e = entry(c, h, "top")
            if e and e["ticker"] not in have:
                tracked.append(e)
            if len(tracked) >= cfg.get("max_tracked", 10):
                break
    tracked = tracked[:cfg.get("max_tracked", 10)]

    # 공시 알림 구성
    title = "추적 시작 (현재 포트폴리오)" if first_run else "새 13F 공시"
    al = ["🔵🔵🔵 <b>거장 추적 봇</b> 🔵🔵🔵",
          f"🚨 <b>{g['name']} {title}</b> "
          f"<i>({info['filed']} 공시 · {info['period']} 분기말)</i>"]
    tag = {"new": "🆕 신규", "add": "➕ 추가", "top": "📌 상위보유"}
    for e in tracked:
        ref = f" 추정매수가 ~${e['ref']}" if e.get("ref") else ""
        inc = f" (+{e['inc']}%)" if e.get("inc") else ""
        when = (f" · 🕐{quarter_months(info['period'])} 매수"
                if e["type"] in ("new", "add") else "")
        al.append(f"{tag[e['type']]}: <b>${e['ticker']}</b> "
                  f"비중 {e['weight']}%{inc}{ref}{when}")
    if exits:
        al.append("➖ 청산: " + ", ".join(exits[:8]))
    alerts.append("\n".join(al))

    # 상태 갱신 (보유내역은 다음 분기 diff 용)
    gs.update({"acc": info["acc"], "filed": info["filed"],
               "period": info["period"], "tracked": tracked,
               "holdings": {c: {"issuer": h["issuer"], "shares": h["shares"],
                                "value": h["value"]} for c, h in holdings.items()}})
    # 이번 분기에 처음 등장한 종목의 매수시점 기록
    fs = gs.setdefault("first_seen", {})
    for c in holdings:
        fs.setdefault(c, info["period"])


def build_daily_message(state, now):
    """매일: 추적 중인 종목의 추정매수가 vs 현재가."""
    all_tickers = []
    for gs in state["gurus"].values():
        for e in gs.get("tracked", []):
            all_tickers.append(e["ticker"])
    cur = current_prices(list(dict.fromkeys(all_tickers)))

    lines = ["🔵🔵🔵 <b>거장 추적 봇</b> 🔵🔵🔵",
             "🐋 <b>거장 포트폴리오 추적</b>",
             f"<i>{now:%m-%d %H:%M} KST · 추정매수가(매수분기 평균) vs 현재가</i>",
             "<i>🟢 = 현재가가 거장 추정매수가보다 낮음</i>"]
    tag = {"new": "🆕", "add": "➕", "top": "📌"}
    for gs in state["gurus"].values():
        tracked = gs.get("tracked", [])
        if not tracked:
            continue
        lines.append("")
        lines.append(f"<b>【{gs.get('name')}】</b> <i>{gs.get('period', '')} 분기</i>")
        for e in tracked:
            t, ref = e["ticker"], e.get("ref")
            p = cur.get(t)
            lab = since_label(e, gs)
            when = f" · 🕐{lab}" if lab else ""
            if ref and p:
                diff = (p / ref - 1) * 100
                mark = "🟢" if diff < 0 else ""
                lines.append(f"• {tag.get(e['type'], '')} <b>${t}</b> {e['weight']}% "
                             f"~${ref:g} → ${p:.2f} {mark}{diff:+.1f}%{when}")
            elif p:
                lines.append(f"• {tag.get(e['type'], '')} <b>${t}</b> {e['weight']}% "
                             f"현재 ${p:.2f}{when}")
    return "\n".join(lines)


def main():
    secrets = ns.load_json(ns.SECRETS_PATH, default={})
    tg_token = ns.get_secret("TELEGRAM_BOT_TOKEN", "bot_token", secrets)
    tg_chat = ns.get_secret("TELEGRAM_CHAT_ID", "chat_id", secrets)
    if not all([tg_token, tg_chat]):
        print("[오류] 텔레그램 키가 없습니다.")
        sys.exit(1)

    cfg = ns.load_json(GURU_CONFIG_PATH)
    state = ns.load_json(GURU_STATE_PATH, default={})
    state.setdefault("gurus", {})
    state.setdefault("cusip_ticker", {})

    print("1) 거장 13F 확인 중…")
    alerts = []
    for g in cfg.get("gurus", []):
        try:
            process_guru(g, state, cfg, alerts)
            gs = state["gurus"].get(str(g["cik"]))
            if gs:
                backfill_first_seen(str(g["cik"]), gs)
                attach_since(gs, state["cusip_ticker"])
        except Exception as e:  # noqa: BLE001
            print(f"[경고] {g.get('name')}: {e}")

    with open(GURU_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)

    now = datetime.now(ns.KST)
    print("2) 일일 비교 메시지 구성 중…")
    daily = build_daily_message(state, now)

    msgs = alerts + [daily]
    sent = 0
    for m in msgs:
        if not m.strip():
            continue
        for chunk in ns.split_message(m, 3800):
            ns.send_telegram(tg_token, tg_chat, chunk)
            sent += 1
    print(f"텔레그램 전송 완료 (새 공시 {len(alerts)}건 + 일일비교, 메시지 {sent}개).")


if __name__ == "__main__":
    main()
