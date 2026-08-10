# -*- coding: utf-8 -*-
"""
유망 종목 '내러티브' 발굴 -> 텔레그램 알림
  - 목적: 샌디스크 HBF 처럼, 새로운 기술/제품/표준이 커뮤니티에서
          '막 논의되기 시작하는' 초기 신호를 잡아 수혜주와 함께 알림.
  - 흐름:
      1) Reddit(기술/투자 서브레딧)에서 최근 글 수집
      2) Google Gemini(무료 API)에게 통째로 던져 '부상하는 내러티브 + 미국 수혜주' 추출
      3) 이미 알린 내러티브는 제외(narrative_state.json)하고 새 것만 텔레그램 전송

  - Reddit 는 공개 .json 엔드포인트를 사용 -> API 키/앱 등록 불필요.
    (개인·소량 읽기 전용. User-Agent 헤더만 있으면 됨.)

  - 키 우선순위 (bollinger 봇과 동일 패턴):
      * 환경변수:  GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
      * secrets.json: gemini_api_key, bot_token, chat_id
"""

import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "narrative_config.json")
STATE_PATH = os.path.join(BASE_DIR, "narrative_state.json")
HISTORY_PATH = os.path.join(BASE_DIR, "narrative_history.jsonl")
SECRETS_PATH = os.path.join(BASE_DIR, "secrets.json")
KST = ZoneInfo("Asia/Seoul")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
TAG_RE = re.compile(r"<[^>]+>")


# ----------------------------- 설정/시크릿 ----------------------------- #
def load_json(path, default=None):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return default if default is not None else {}
    return default if default is not None else {}


def get_secret(env_key, secret_key, secrets):
    return os.environ.get(env_key) or secrets.get(secret_key)


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def append_history(narratives):
    """이번 실행에서 추출된 모든 내러티브를 이력(JSONL)에 한 줄씩 추가.
    월간 다이제스트에서 '언급 횟수'(재등장 빈도)를 집계하는 데 사용."""
    if not narratives:
        return
    ts = datetime.now(KST).isoformat()
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        for n in narratives:
            rec = {
                "ts": ts,
                "name": n.get("name", ""),
                "tickers": n.get("tickers", []),
                "stage": n.get("stage", ""),
                "confidence": n.get("confidence", ""),
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ----------------------- Reddit 수집 (공개 RSS, 키 불필요) ----------------------- #
#   Reddit 은 익명 .json 을 차단(403)하지만 .rss(Atom) 피드는 열려 있음.
#   데이터센터 IP(GitHub Actions)에서도 동작. User-Agent 는 브라우저형으로.
def _strip_html(raw):
    if not raw:
        return ""
    txt = TAG_RE.sub(" ", raw)
    txt = html.unescape(txt)
    return re.sub(r"\s+", " ", txt).strip()


def _fetch_rss(sub, listing, params, retries=3):
    url = f"https://www.reddit.com/r/{sub}/{listing}.rss"
    resp = None
    for attempt in range(retries):
        resp = requests.get(url, params=params,
                            headers={"User-Agent": USER_AGENT}, timeout=15)
        if resp.status_code == 429:
            time.sleep(5 * (attempt + 1))   # 백오프 후 재시도
            continue
        resp.raise_for_status()
        return ET.fromstring(resp.content)
    resp.raise_for_status()   # 재시도 소진 -> 마지막 오류 전달
    return ET.fromstring(resp.content)


def collect_reddit_posts(cfg):
    """Reddit RSS(Atom) 피드로 서브레딧 글 수집 (API 키/앱 등록 불필요)."""
    rc = cfg["reddit"]
    lookback = rc.get("lookback_hours", 48) * 3600
    now = time.time()
    max_chars = rc.get("max_selftext_chars", 1200)
    per_sub = rc.get("posts_per_subreddit", 12)

    posts = []
    for sub in rc["subreddits"]:
        entries = []
        # 하루 top + new 를 섞어 초기 신호 포착
        for listing, extra in (("top", {"t": "day"}), ("new", {})):
            try:
                root = _fetch_rss(sub, listing, extra)
                entries += root.findall("a:entry", ATOM_NS)
            except Exception as e:  # noqa: BLE001
                print(f"[경고] r/{sub}/{listing} 수집 실패: {e}")
            time.sleep(1.5)   # Reddit RSS 레이트리밋 회피

        seen = set()
        count = 0
        for e in entries:
            link_el = e.find("a:link", ATOM_NS)
            link = link_el.get("href") if link_el is not None else None
            if not link or link in seen:
                continue
            seen.add(link)

            # 발행시각 lookback 필터
            pub_el = e.find("a:published", ATOM_NS)
            if pub_el is not None and pub_el.text:
                try:
                    pub_ts = datetime.fromisoformat(pub_el.text).timestamp()
                    if (now - pub_ts) > lookback:
                        continue
                except Exception:  # noqa: BLE001
                    pass

            title_el = e.find("a:title", ATOM_NS)
            title = (title_el.text or "").strip() if title_el is not None else ""
            cont_el = e.find("a:content", ATOM_NS)
            body = _strip_html(cont_el.text if cont_el is not None else "")
            if len(body) > max_chars:
                body = body[:max_chars] + "…"

            posts.append({
                "source": f"r/{sub}",
                "title": title,
                "body": body,
                "url": link,
            })
            count += 1
            if count >= per_sub:
                break

    return posts


# ----------------------- Hacker News 수집 (Algolia API, 키 불필요) ----------------------- #
def collect_hn_posts(cfg):
    """Hacker News 최근 인기 스토리 수집 (무료 Algolia API, 키/UA 불필요).
    기술 발표가 레딧보다 먼저 뜨는 경우가 많아 초기 신호원으로 유용."""
    hc = cfg.get("hackernews", {})
    if not hc.get("enabled", True):
        return []

    lookback = hc.get("lookback_hours", 48) * 3600
    now = time.time()
    cutoff = int(now - lookback)
    min_points = hc.get("min_points", 30)
    max_stories = hc.get("max_stories", 30)
    max_chars = cfg.get("reddit", {}).get("max_selftext_chars", 1200)

    posts = []
    try:
        resp = requests.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={
                "tags": "story",
                "numericFilters": f"created_at_i>{cutoff},points>{min_points}",
                "hitsPerPage": max_stories,
            },
            timeout=15,
        )
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
    except Exception as e:  # noqa: BLE001
        print(f"[경고] Hacker News 수집 실패: {e}")
        return []

    for h in hits:
        title = (h.get("title") or "").strip()
        if not title:
            continue
        body = _strip_html(h.get("story_text") or "")
        if len(body) > max_chars:
            body = body[:max_chars] + "…"
        oid = h.get("objectID")
        url = h.get("url") or f"https://news.ycombinator.com/item?id={oid}"
        posts.append({
            "source": f"HN({h.get('points', 0)}p)",
            "title": title,
            "body": body,
            "url": url,
        })

    return posts


def build_corpus(posts):
    """수집한 글을 LLM 입력용 텍스트로 직렬화."""
    lines = []
    for i, p in enumerate(posts, 1):
        lines.append(f"### [{i}] {p['source']}")
        lines.append(f"TITLE: {p['title']}")
        if p.get("body"):
            lines.append(f"BODY: {p['body']}")
        lines.append(f"URL: {p['url']}")
        lines.append("")
    return "\n".join(lines)


# ----------------------------- Claude 분석 ----------------------------- #
SYSTEM_PROMPT = """\
You are an emerging-technology equity analyst. You read raw discussion from
technology and investing communities and surface NEW or EARLY-STAGE narratives:
a specific new technology, product, standard, or supply-chain shift that is just
starting to be talked about and that could benefit particular US-listed stocks.

A good example is SanDisk's "HBF" (High-Bandwidth Flash) — a new memory concept
positioned against HBM — spotted early in niche hardware discussion before it was
widely known. That is the KIND of signal to find.

Rules:
- Only report narratives that are genuinely emerging / not yet mainstream. Skip
  well-worn, already-priced-in stories (e.g. "NVIDIA sells AI GPUs", "AI is big").
- Each narrative MUST map to one or more concrete US-listed tickers that plausibly
  benefit. Use real ticker symbols. If you cannot name a plausible beneficiary,
  do not include it.
- Do NOT default to only the largest, most obvious mega-caps. For each narrative,
  actively include SMALL- and MID-CAP beneficiaries (the more asymmetric, higher-
  upside plays) alongside any large-cap names. A niche small-cap pure-play on the
  emerging technology is often the most interesting signal.
- If a specific ticker is mentioned DIRECTLY in the source discussion (e.g. someone
  names "AAOI", "CRDO", etc.), prioritize surfacing that exact ticker rather than
  substituting a bigger, safer name for the same theme.
- Prefer specificity: a named technology/product/standard over a vague theme.
- Be skeptical of pure hype / pump-and-dump. Note it in the rationale if relevant.
- If nothing qualifies, return an empty list. Do not force weak items.
- Respond in Korean for the summary/rationale fields (ticker symbols stay English).
"""

# Gemini 구조화 출력 스키마 (OpenAPI 서브셋: type 대문자, additionalProperties 미지원)
GEMINI_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "narratives": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "summary": {"type": "STRING"},
                    "tickers": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "rationale": {"type": "STRING"},
                    "stage": {"type": "STRING",
                              "enum": ["very_early", "emerging", "gaining_traction"]},
                    "confidence": {"type": "STRING",
                                   "enum": ["low", "medium", "high"]},
                },
                "required": ["name", "summary", "tickers", "rationale",
                             "stage", "confidence"],
            },
        }
    },
    "required": ["narratives"],
}


def analyze_with_gemini(cfg, api_key, corpus):
    """Google Gemini(무료 티어) REST API 로 내러티브 추출. 구조화 JSON 강제."""
    lc = cfg["llm"]
    model = lc.get("model", "gemini-2.0-flash")

    user_msg = (
        f"감시 테마: {cfg['focus']}\n\n"
        f"최대 {lc.get('max_narratives', 6)}개까지, 가장 유망한 초기 내러티브만 골라라.\n\n"
        f"--- 커뮤니티 원문 시작 ---\n{corpus}\n--- 커뮤니티 원문 끝 ---"
    )

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "response_schema": GEMINI_SCHEMA,
            "maxOutputTokens": lc.get("max_tokens", 12000),
            "temperature": 0.6,
            # 2.5-flash 는 thinking 토큰이 출력 예산을 잠식 -> 상한을 둬서 JSON 잘림 방지
            "thinkingConfig": {"thinkingBudget": lc.get("thinking_budget", 2048)},
        },
    }

    resp = requests.post(
        url,
        params={"key": api_key},
        json=body,
        timeout=120,
    )
    if resp.status_code != 200:
        print(f"[오류] Gemini 호출 실패 {resp.status_code}: {resp.text[:400]}")
        return []

    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        print("[오류] Gemini 응답 파싱 실패:", str(data)[:400])
        return []

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        print("[오류] Gemini JSON 파싱 실패:\n", text[:500])
        return []

    um = data.get("usageMetadata", {})
    print(f"[Gemini] 입력 {um.get('promptTokenCount', '?')} / "
          f"출력 {um.get('candidatesTokenCount', '?')} 토큰")
    return parsed.get("narratives", [])


# -------------------- 종목 enrich: 가격·모멘텀·신규성·점수 -------------------- #
def price_snapshot(tickers):
    """yfinance 로 티커별 현재가·1M/3M 등락률·52주 고점대비(%) 조회.
    실패해도 알림은 계속 나가도록 예외를 삼킨다. (키 불필요·무료)"""
    uniq = [t for t in dict.fromkeys((t or "").strip().upper() for t in tickers) if t]
    out = {}
    if not uniq:
        return out
    try:
        import yfinance as yf
        data = yf.download(uniq, period="1y", interval="1d",
                           progress=False, group_by="ticker", threads=True)
    except Exception as e:  # noqa: BLE001
        print(f"[경고] 가격 조회 실패(전체): {e}")
        return out

    for t in uniq:
        try:
            if len(uniq) == 1:
                close = data["Close"].dropna()
            else:
                close = data[t]["Close"].dropna()
            if close is None or close.empty:
                continue
            price = float(close.iloc[-1])

            def pct(days):
                if len(close) > days:
                    past = float(close.iloc[-1 - days])
                    return (price / past - 1) * 100 if past else None
                return None

            hi = float(close.max())
            out[t] = {
                "price": price,
                "chg_1m": pct(21),
                "chg_3m": pct(63),
                "off_high": (price / hi - 1) * 100 if hi else None,
            }
        except Exception:  # noqa: BLE001
            continue
    return out


def ticker_history_days():
    """이력에서 티커별 '등장한 날짜 집합' 로드 (신규/지속 판정용)."""
    m = {}
    if not os.path.exists(HISTORY_PATH):
        return m
    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
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
                t = (t or "").strip().upper()
                if t and day:
                    m.setdefault(t, set()).add(day)
    return m


def novelty_tag(ticker, today, hist_days):
    """🆕 최초포착 / 🔁 지속(며칠) 태그. 오늘 이전 등장 이력으로 판정."""
    days = hist_days.get((ticker or "").strip().upper(), set())
    prior = days - {today}
    if not prior:
        return "🆕", True, 0
    recent7 = 0
    try:
        cutoff = (datetime.now(KST) - timedelta(days=7)).strftime("%Y-%m-%d")
        recent7 = len([d for d in days if d >= cutoff])
    except Exception:  # noqa: BLE001
        recent7 = len(days)
    return f"🔁{len(prior)}일", False, recent7


def opportunity_score(n, tickers_meta):
    """조기·저평가 기회 점수 0~100.
    단계(초기일수록↑) + 신뢰도 + 신규성 + '아직 안 오른 정도' 를 합산."""
    stage_pts = {"very_early": 40, "emerging": 28,
                 "gaining_traction": 15}.get(n.get("stage"), 20)
    conf_pts = {"high": 25, "medium": 15, "low": 8}.get(n.get("confidence"), 12)

    is_new = any(m.get("is_new") for m in tickers_meta)
    novelty_pts = 20 if is_new else 8

    # 대표 종목 = 3개월 등락률이 가장 낮은(=가장 덜 오른) 종목의 미상승 보너스
    chgs = [m["price"]["chg_3m"] for m in tickers_meta
            if m.get("price") and m["price"].get("chg_3m") is not None]
    price_pts = 0
    if chgs:
        least = min(chgs)
        if least <= 10:
            price_pts = 15          # 아직 안 움직임 = 조기
        elif least <= 40:
            price_pts = 8
        elif least >= 120:
            price_pts = -12         # 이미 폭등 = 늦음
    total = stage_pts + conf_pts + novelty_pts + price_pts
    return max(0, min(100, total))


def enrich_narratives(narratives, today):
    """각 내러티브에 티커별 가격·신규태그 메타를 붙이고 점수 계산 후 정렬."""
    all_tickers = [t for n in narratives for t in n.get("tickers", [])]
    prices = price_snapshot(all_tickers)
    hist_days = ticker_history_days()
    for n in narratives:
        metas = []
        for t in n.get("tickers", []):
            tag, is_new, recent7 = novelty_tag(t, today, hist_days)
            metas.append({"ticker": (t or "").strip().upper(),
                          "price": prices.get((t or "").strip().upper()),
                          "tag": tag, "is_new": is_new, "recent7": recent7})
        n["_meta"] = metas
        n["_score"] = opportunity_score(n, metas)
    narratives.sort(key=lambda x: x.get("_score", 0), reverse=True)
    return narratives


def fmt_ticker_line(m):
    """'• $AAOI 🆕  $12.30 (1M +3% · 3M -8% · 고점 -22%)' 형태."""
    parts = [f"• <b>${m['ticker']}</b> {m['tag']}"]
    p = m.get("price")
    if p:
        seg = [f"${p['price']:.2f}"]
        mom = []
        for label, key in (("1M", "chg_1m"), ("3M", "chg_3m"), ("고점", "off_high")):
            v = p.get(key)
            if v is not None:
                mom.append(f"{label} {v:+.0f}%")
        if mom:
            seg.append("(" + " · ".join(mom) + ")")
        parts.append("  " + " ".join(seg))
    return "".join(parts)


# ----------------------------- 텔레그램 ----------------------------- #
def split_message(text, limit=3800):
    """텔레그램 4096자 제한 대응 - 줄 단위로 분할."""
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


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        data={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


STAGE_KO = {
    "very_early": "🌱 극초기",
    "emerging": "🌿 부상 중",
    "gaining_traction": "🔥 확산 중",
}
CONF_KO = {"low": "낮음", "medium": "보통", "high": "높음"}


def norm_key(name):
    """내러티브 중복 판정용 정규화 키 (영문 소문자 + 숫자만)."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


# ----------------------------- 메인 ----------------------------- #
def main():
    cfg = load_json(CONFIG_PATH)
    secrets = load_json(SECRETS_PATH, default={})

    gemini_key = get_secret("GEMINI_API_KEY", "gemini_api_key", secrets)
    tg_token = get_secret("TELEGRAM_BOT_TOKEN", "bot_token", secrets)
    tg_chat = get_secret("TELEGRAM_CHAT_ID", "chat_id", secrets)

    missing = [n for n, v in [
        ("GEMINI_API_KEY", gemini_key),
        ("TELEGRAM_BOT_TOKEN", tg_token),
        ("TELEGRAM_CHAT_ID", tg_chat),
    ] if not v]
    if missing:
        print("[오류] 다음 키가 없습니다:", ", ".join(missing))
        print("       secrets.json 또는 환경변수(GitHub Secrets)에 넣어주세요.")
        sys.exit(1)

    print("1) 소스 수집 중… (Reddit + Hacker News)")
    posts = collect_reddit_posts(cfg)
    print(f"   -> Reddit {len(posts)}개")
    hn = collect_hn_posts(cfg)
    print(f"   -> Hacker News {len(hn)}개")
    posts += hn
    print(f"   -> 합계 {len(posts)}개 글 수집")
    if not posts:
        print("수집된 글 없음. 종료.")
        return

    print("2) Gemini 분석 중…")
    corpus = build_corpus(posts)
    narratives = analyze_with_gemini(cfg, gemini_key, corpus)
    print(f"   -> {len(narratives)}개 내러티브 추출")

    # 이력 기록 (재등장 빈도 집계용 - 중복 제거 전 전체 기록)
    append_history(narratives)

    # 3) 중복 제거
    state = load_json(STATE_PATH, default={"seen": {}})
    seen = state.setdefault("seen", {})
    today = datetime.now(KST).strftime("%Y-%m-%d")

    fresh = []
    for n in narratives:
        key = norm_key(n.get("name", ""))
        if not key or key in seen:
            continue
        seen[key] = {"name": n.get("name"), "first_seen": today,
                     "tickers": n.get("tickers", [])}
        fresh.append(n)

    save_state(state)

    if not fresh:
        print("새 내러티브 없음. (알림 미전송)")
        return

    # 4) 종목 가격·모멘텀·신규성 enrich + 조기기회 점수순 정렬
    print("3) 종목 가격·모멘텀 조회 중…")
    fresh = enrich_narratives(fresh, today)

    # 5) 텔레그램 전송 (점수 높은 순)
    now_kst = datetime.now(KST)
    lines = [f"🔍 <b>새 유망 내러티브 감지</b>  <i>{now_kst:%Y-%m-%d %H:%M} KST</i>",
             "<i>조기·저평가 기회 점수순 · 🆕최초 🔁지속</i>", ""]
    for n in fresh:
        stage = STAGE_KO.get(n.get("stage"), n.get("stage", ""))
        conf = CONF_KO.get(n.get("confidence"), n.get("confidence", ""))
        score = n.get("_score", 0)
        star = "🌟 " if score >= 70 else ""
        lines.append(f"{star}<b>[{score}점] {n.get('name')}</b>")
        lines.append(f"{stage} · 신뢰도 {conf}")
        lines.append(f"{n.get('summary','')}")
        lines.append(f"<i>{n.get('rationale','')}</i>")
        for m in n.get("_meta", []):
            lines.append(fmt_ticker_line(m))
        lines.append("")

    msg = "\n".join(lines).strip()
    try:
        for chunk in split_message(msg, 3800):
            send_telegram(tg_token, tg_chat, chunk)
        print(f"텔레그램 전송 완료 (새 내러티브 {len(fresh)}개).")
    except Exception as e:  # noqa: BLE001
        print(f"[오류] 텔레그램 전송 실패: {e}")


if __name__ == "__main__":
    main()
