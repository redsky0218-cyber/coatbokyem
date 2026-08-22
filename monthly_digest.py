# -*- coding: utf-8 -*-
"""
월간 다이제스트: 지난 30일간 쌓인 내러티브 이력을 종합 정리.
  - narrative_history.jsonl 을 읽어 최근 N일치만 필터
  - 티커(기업)별 / 기술·테마별 언급 횟수 집계
  - 집계 결과를 Gemini 에게 던져 '이달의 종합 리포트' 작성
  - 텔레그램으로 전송 + (설정 시) Gmail 로 메일 발송 (본문 표 + CSV 첨부)

  narrative_scout.py 의 유틸(경로/시크릿/텔레그램)을 그대로 재사용.

  이메일 키 (있을 때만 메일 발송):
    * 환경변수:  GMAIL_ADDRESS, GMAIL_APP_PASSWORD, EMAIL_TO(선택)
    * secrets.json: gmail_address, gmail_app_password, email_to
"""

import csv
import io
import json
import smtplib
import ssl
import sys
from collections import Counter
from datetime import datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

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
            t = ns.clean_ticker(t)
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
detection: theme mention counts, ticker mention counts, and newly-detected
technology terms (with what they are).

Write a Korean monthly report ORGANIZED BY THEME CATEGORY. Use only categories
that actually appear in the data, chosen from:
반도체·AI 하드웨어 / AI·소프트웨어 / 원자력·에너지 / 우주·방산 / 바이오·헬스 /
사이버보안 / 양자컴퓨팅 / 로보틱스·자동화 / 기타

Format (텔레그램용, 카테고리마다):
■ [카테고리명]
- 이달 핵심 흐름 1~2문장 (무엇이 왜 계속 언급됐는지)
- 관련 기술: 감지된 신기술 용어가 있으면 '용어 = 쉬운 한 줄 설명' 형태로.
  전문지식 없는 사람도 이해하게 아주 쉽게 풀어써라.
- 대표 종목: $티커 들 (반복 등장한 것 위주, 소형주 놓치지 말 것)

마지막에:
■ 이달의 주목 리스트 — 5~8개 티커, 각 한 줄 코멘트.
■ 흐름 변화 — 새로 부상한 테마 vs 식어가는 테마 한두 줄.

규칙: 통계에 없는 종목/테마/기술을 지어내지 말 것. 티커는 영문 그대로.
과장·펌핑 금지. 전체 1300자 내외.
마크다운(**, ##, - 목록기호 남발) 쓰지 말 것 — 일반 텍스트다.
별도의 제목/연월/헤더를 만들지 말고 곧바로 첫 ■ 카테고리부터 시작하라.
"""


def tally_tech(tech_rows):
    """기술 용어별 언급 횟수 + 설명/시장정보."""
    counts = Counter()
    info = {}
    for rec in tech_rows:
        term = (rec.get("term") or "").strip()
        if not term:
            continue
        counts[term] += 1
        info[term] = {"what": rec.get("what", ""),
                      "market_size": rec.get("market_size", ""),
                      "cagr": rec.get("cagr", "")}
    return counts, info


def build_digest_prompt(rows, ticker_counts, theme_counts, ticker_days,
                        tech_counts, tech_info):
    lines = [f"[집계 기간] 최근 {LOOKBACK_DAYS}일, 총 {len(rows)}건의 내러티브 감지", ""]
    lines.append("[티커별 언급 횟수 / 등장 일수]")
    for t, c in ticker_counts.most_common(TOP_TICKERS):
        lines.append(f"- {t}: {c}회 / {len(ticker_days.get(t, set()))}일")
    lines.append("")
    lines.append("[테마(내러티브)별 언급 횟수]")
    for name, c in theme_counts.most_common(TOP_THEMES):
        lines.append(f"- ({c}회) {name}")
    if tech_counts:
        lines.append("")
        lines.append("[감지된 신기술 용어]")
        for term, c in tech_counts.most_common(15):
            inf = tech_info.get(term, {})
            lines.append(f"- ({c}회) {term}: {inf.get('what', '')}")
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


def build_tech_block(tech_counts, tech_info):
    """이달 감지된 신기술 정리 (쉬운 설명 + 시장 정보)."""
    if not tech_counts:
        return ""
    lines = ["", "🔬 <b>이달의 신기술 정리</b>"]
    for term, c in tech_counts.most_common(12):
        inf = tech_info.get(term, {})
        lines.append(f"• <b>{esc(term)}</b> ({c}회)")
        if inf.get("what"):
            lines.append(f"   {esc(inf['what'])}")
        ms, cg = inf.get("market_size", ""), inf.get("cagr", "")
        seg = [s for s in (ms, cg) if s and s != "미상"]
        if seg:
            lines.append(f"   📊 {esc(' · '.join(seg))}")
    return "\n".join(lines)


def build_guru_block():
    """거장 추적 상태 요약 (guru_state.json 이 있으면)."""
    gpath = ns.os.path.join(ns.BASE_DIR, "guru_state.json")
    state = ns.load_json(gpath, default={})
    gurus = state.get("gurus", {})
    if not gurus:
        return ""
    tag = {"new": "🆕", "add": "➕", "top": "📌"}
    lines = ["", "🐋 <b>거장 동향 (최신 13F 기준)</b>"]
    for gs in gurus.values():
        tracked = gs.get("tracked", [])
        if not tracked:
            continue
        toks = [f"{tag.get(e.get('type'), '')}${e.get('ticker')}"
                for e in tracked[:8]]
        lines.append(f"• <b>{esc(gs.get('name', ''))}</b> "
                     f"<i>({gs.get('period', '')})</i>: {' '.join(toks)}")
    return "\n".join(lines) if len(lines) > 2 else ""


# ----------------------------- 이메일 ----------------------------- #
def _csv_bytes(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    # 엑셀/구글시트에서 한글 안 깨지도록 UTF-8 BOM
    return ("﻿" + buf.getvalue()).encode("utf-8")


def build_email_html(summary, ticker_counts, ticker_days, theme_counts, rows, now):
    def tbl(title, headers, body_rows):
        th = "".join(f"<th style='padding:6px 10px;border:1px solid #ddd;"
                     f"background:#f4f4f4;text-align:left'>{esc(h)}</th>" for h in headers)
        trs = []
        for r in body_rows:
            tds = "".join(f"<td style='padding:6px 10px;border:1px solid #ddd'>{esc(str(c))}</td>"
                          for c in r)
            trs.append(f"<tr>{tds}</tr>")
        return (f"<h3 style='margin:18px 0 6px'>{esc(title)}</h3>"
                f"<table style='border-collapse:collapse;font-size:14px'>"
                f"<tr>{th}</tr>{''.join(trs)}</table>")

    ticker_rows = [[i, t, f"{c}회", f"{len(ticker_days.get(t, set()))}일"]
                   for i, (t, c) in enumerate(ticker_counts.most_common(TOP_TICKERS), 1)]
    theme_rows = [[i, name, f"{c}회"]
                  for i, (name, c) in enumerate(theme_counts.most_common(TOP_THEMES), 1)]

    summary_html = esc(summary).replace("\n", "<br>")
    return f"""\
<div style="font-family:Segoe UI,Apple SD Gothic Neo,sans-serif;max-width:720px;color:#222">
  <h2>🗓️ 월간 내러티브 다이제스트</h2>
  <p style="color:#666">{now:%Y-%m-%d} KST · 최근 {LOOKBACK_DAYS}일 · 총 {len(rows)}건 감지</p>
  <div style="line-height:1.7">{summary_html}</div>
  {tbl("📊 티커(기업) 언급 순위", ["#", "티커", "언급", "등장일수"], ticker_rows)}
  {tbl("🧬 기술·테마 언급 순위", ["#", "테마", "언급"], theme_rows)}
  <p style="color:#999;font-size:12px;margin-top:20px">
    첨부된 CSV 파일은 엑셀/구글시트에서 바로 열 수 있습니다.</p>
</div>"""


def send_email(sender, app_pw, recipient, subject, html_body, attachments):
    """Gmail SMTP(SSL)로 발송. attachments: [(filename, bytes)]"""
    msg = MIMEMultipart("mixed")
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)
    for fname, data in attachments:
        part = MIMEApplication(data, Name=fname)
        part["Content-Disposition"] = f'attachment; filename="{fname}"'
        msg.attach(part)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(sender, app_pw)
        s.sendmail(sender, [recipient], msg.as_string())


# ----------------------------- 구글 시트 (Apps Script 웹훅) ----------------------------- #
def push_to_gsheet(webhook_url, token, month, ticker_counts, ticker_days,
                   theme_counts, total):
    """Apps Script 웹앱(doPost)에 월간 집계를 POST -> 시트에 누적.
    payload:
      { token, month, total,
        tickers: [[티커, 언급횟수, 등장일수], ...],
        themes:  [[테마, 언급횟수], ...] }
    """
    payload = {
        "token": token or "",
        "month": month,
        "total": total,
        "tickers": [[t, c, len(ticker_days.get(t, set()))]
                    for t, c in ticker_counts.most_common(TOP_TICKERS)],
        "themes": [[name, c]
                   for name, c in theme_counts.most_common(TOP_THEMES)],
    }
    resp = requests.post(webhook_url, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.text[:200]


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

    all_rows = load_history(LOOKBACK_DAYS)
    rows = [r for r in all_rows if r.get("kind") != "tech"]
    tech_rows = [r for r in all_rows if r.get("kind") == "tech"]
    print(f"최근 {LOOKBACK_DAYS}일 이력: 내러티브 {len(rows)}건 / 기술 {len(tech_rows)}건")
    if not rows and not tech_rows:
        print("이력이 없습니다. (아직 데이터가 쌓이지 않음) 종료.")
        return

    ticker_counts, theme_counts, ticker_days = tally(rows)
    tech_counts, tech_info = tally_tech(tech_rows)
    print(f"티커 {len(ticker_counts)}종 / 테마 {len(theme_counts)}종 / "
          f"기술 {len(tech_counts)}종 집계")

    prompt_text = build_digest_prompt(rows, ticker_counts, theme_counts,
                                      ticker_days, tech_counts, tech_info)
    summary = summarize_with_gemini(cfg, gemini_key, prompt_text)
    if not summary:
        print("요약 생성 실패. 종료.")
        return

    now = datetime.now(ns.KST)
    banner = "🟡🟡🟡 <b>월간 정리 봇</b> 🟡🟡🟡"
    header = f"🗓️ <b>월간 내러티브 다이제스트</b>  <i>{now:%Y-%m-%d} KST</i>"
    period = f"<i>최근 {LOOKBACK_DAYS}일 · 내러티브 {len(rows)}건 · 기술 {len(tech_rows)}건</i>"
    guru = build_guru_block()
    stats = build_stats_block(ticker_counts, ticker_days)
    themes = build_themes_block(theme_counts)
    techs = build_tech_block(tech_counts, tech_info)
    msg = (f"{banner}\n{header}\n{period}\n\n{esc(summary)}"
           f"\n{guru}\n{stats}\n{themes}\n{techs}")

    # 텔레그램 4096자 제한 대응 분할
    try:
        for chunk in _split(msg, 3800):
            ns.send_telegram(tg_token, tg_chat, chunk)
        print("텔레그램 전송 완료.")
    except Exception as e:  # noqa: BLE001
        print(f"[경고] 텔레그램 전송 실패: {e}")

    # 이메일 전송 (Gmail 앱 비밀번호가 있을 때만)
    gmail_addr = ns.get_secret("GMAIL_ADDRESS", "gmail_address", secrets)
    gmail_pw = ns.get_secret("GMAIL_APP_PASSWORD", "gmail_app_password", secrets)
    if gmail_addr and gmail_pw:
        recipient = ns.get_secret("EMAIL_TO", "email_to", secrets) or gmail_addr
        ticker_csv = _csv_bytes(
            ["순위", "티커", "언급횟수", "등장일수"],
            [[i, t, c, len(ticker_days.get(t, set()))]
             for i, (t, c) in enumerate(ticker_counts.most_common(TOP_TICKERS), 1)])
        theme_csv = _csv_bytes(
            ["순위", "테마", "언급횟수"],
            [[i, name, c]
             for i, (name, c) in enumerate(theme_counts.most_common(TOP_THEMES), 1)])
        html_body = build_email_html(summary, ticker_counts, ticker_days,
                                     theme_counts, rows, now)
        ym = now.strftime("%Y%m")
        try:
            send_email(
                gmail_addr, gmail_pw, recipient,
                f"[월간 다이제스트] {now:%Y-%m-%d} 내러티브 리포트",
                html_body,
                [(f"ticker_ranking_{ym}.csv", ticker_csv),
                 (f"theme_ranking_{ym}.csv", theme_csv)])
            print(f"이메일 전송 완료 -> {recipient}")
        except Exception as e:  # noqa: BLE001
            print(f"[경고] 이메일 전송 실패: {e}")
    else:
        print("이메일 미설정 (GMAIL_ADDRESS/GMAIL_APP_PASSWORD 없음) - 메일 생략")

    # 구글 시트 누적 (Apps Script 웹훅 URL 이 있을 때만)
    gsheet_url = ns.get_secret("GSHEET_WEBHOOK_URL", "gsheet_webhook_url", secrets)
    gsheet_token = ns.get_secret("GSHEET_TOKEN", "gsheet_token", secrets)
    if gsheet_url:
        try:
            r = push_to_gsheet(gsheet_url, gsheet_token, now.strftime("%Y-%m"),
                               ticker_counts, ticker_days, theme_counts, len(rows))
            print(f"구글 시트 기록 완료: {r}")
        except Exception as e:  # noqa: BLE001
            print(f"[경고] 구글 시트 기록 실패: {e}")
    else:
        print("구글 시트 미설정 (GSHEET_WEBHOOK_URL 없음) - 시트 기록 생략")


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
