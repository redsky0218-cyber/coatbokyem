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
                "tickers": clean_tickers(n.get("tickers", [])),
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


# ----------------------- arXiv 논문 수집 (공식 API, 키 불필요) ----------------------- #
def collect_arxiv_posts(cfg):
    """arXiv 최신 논문 수집. 기술의 '진짜 최초' 신호는 커뮤니티보다
    논문에서 먼저 나오는 경우가 많다 (HBF 도 학회 발표가 먼저)."""
    ac = cfg.get("arxiv", {})
    if not ac.get("enabled", True):
        return []

    cats = ac.get("categories", ["cs.AR", "cs.ET", "quant-ph", "physics.optics"])
    max_papers = ac.get("max_papers", 25)
    lookback = ac.get("lookback_hours", 72) * 3600
    max_chars = ac.get("max_abstract_chars", 700)

    query = "+OR+".join(f"cat:{c}" for c in cats)
    url = ("http://export.arxiv.org/api/query?search_query=" + query +
           f"&sortBy=submittedDate&sortOrder=descending&max_results={max_papers}")
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=25)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:  # noqa: BLE001
        print(f"[경고] arXiv 수집 실패: {e}")
        return []

    cutoff = time.time() - lookback
    posts = []
    for entry in root.findall("a:entry", ATOM_NS):
        pub = entry.findtext("a:published", "", ATOM_NS)
        try:
            ts = datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if ts < cutoff:
            continue
        title = " ".join((entry.findtext("a:title", "", ATOM_NS) or "").split())
        abstract = " ".join((entry.findtext("a:summary", "", ATOM_NS) or "").split())
        if len(abstract) > max_chars:
            abstract = abstract[:max_chars] + "…"
        link = entry.findtext("a:id", "", ATOM_NS) or ""
        cat_el = entry.find("a:category", ATOM_NS)
        cat = cat_el.get("term") if cat_el is not None else "arXiv"
        posts.append({"source": f"arXiv({cat})", "title": title,
                      "body": abstract, "url": link})
    return posts


# ----------------- Lobsters 수집 (하드코어 개발자 커뮤니티, 키 불필요) ----------------- #
def collect_lobsters_posts(cfg):
    """lobste.rs 인기글. HN 보다 큐레이션이 엄격해 신호 밀도가 높음."""
    lc = cfg.get("lobsters", {})
    if not lc.get("enabled", True):
        return []
    try:
        resp = requests.get("https://lobste.rs/hottest.json",
                            headers={"User-Agent": USER_AGENT}, timeout=15)
        resp.raise_for_status()
        items = resp.json()
    except Exception as e:  # noqa: BLE001
        print(f"[경고] Lobsters 수집 실패: {e}")
        return []
    cutoff = time.time() - lc.get("lookback_hours", 48) * 3600
    min_score = lc.get("min_score", 10)
    posts = []
    for it in items[:lc.get("max_stories", 15) * 2]:
        try:
            ts = datetime.fromisoformat(it.get("created_at", "")).timestamp()
        except ValueError:
            continue
        if ts < cutoff or (it.get("score") or 0) < min_score:
            continue
        posts.append({
            "source": f"Lobsters({it.get('score', 0)}p)",
            "title": (it.get("title") or "").strip(),
            "body": "",
            "url": it.get("url") or it.get("short_id_url") or "",
        })
        if len(posts) >= lc.get("max_stories", 15):
            break
    return posts


# ----------------- StockTwits 트렌딩 (개미 관심 티커, 키 불필요) ----------------- #
def collect_stocktwits_posts(cfg):
    """미국 리테일 투자자들의 실시간 트렌딩 티커 목록.
    소형주에 자금·관심이 몰리는 순간의 스냅샷."""
    sc = cfg.get("stocktwits", {})
    if not sc.get("enabled", True):
        return []
    try:
        resp = requests.get("https://api.stocktwits.com/api/2/trending/symbols.json",
                            headers={"User-Agent": USER_AGENT}, timeout=15)
        resp.raise_for_status()
        syms = resp.json().get("symbols", [])
    except Exception as e:  # noqa: BLE001
        print(f"[경고] StockTwits 수집 실패: {e}")
        return []
    if not syms:
        return []
    body = ", ".join(f"${s.get('symbol')}({s.get('title', '')})"
                     for s in syms[:30] if s.get("symbol"))
    return [{
        "source": "StockTwits트렌딩",
        "title": "지금 미국 개인투자자들이 가장 많이 보는 티커 (실시간 트렌딩)",
        "body": body,
        "url": "https://stocktwits.com/rankings/trending",
    }]


# ----------------- GitHub 신규 인기 저장소 (새 기술의 코드 신호, 키 불필요) ----------------- #
def collect_github_posts(cfg):
    """최근 며칠 내 생성돼 스타가 폭증한 저장소 = 새 기술이 코드로 먼저 뜨는 신호."""
    gc = cfg.get("github", {})
    if not gc.get("enabled", True):
        return []
    since = (datetime.now(KST) - timedelta(days=gc.get("created_within_days", 7)))
    try:
        resp = requests.get(
            "https://api.github.com/search/repositories",
            params={"q": f"created:>{since:%Y-%m-%d} stars:>{gc.get('min_stars', 100)}",
                    "sort": "stars", "order": "desc",
                    "per_page": gc.get("max_repos", 10)},
            headers={"User-Agent": USER_AGENT,
                     "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except Exception as e:  # noqa: BLE001
        print(f"[경고] GitHub 수집 실패: {e}")
        return []
    posts = []
    for it in items:
        posts.append({
            "source": f"GitHub(⭐{it.get('stargazers_count', 0)})",
            "title": f"{it.get('full_name', '')}: {(it.get('description') or '').strip()}"[:200],
            "body": f"language: {it.get('language') or '?'} · 신규 저장소 스타 급증",
            "url": it.get("html_url") or "",
        })
    return posts


# ----------------- SEC EDGAR 전문검색 (공시 교차확인, 키 불필요) ----------------- #
def sec_fulltext_hits(term, lookback_days=90, max_hits=2):
    """감지된 신기술 용어가 실제 '기업 공시'(8-K/10-K/S-1 등)에 등장하는지 확인.
    커뮤니티 소문 -> 공식 문서로 확인되는 순간을 잡는 교차검증."""
    end = datetime.now(KST).date()
    start = end - timedelta(days=lookback_days)
    try:
        resp = requests.get(
            "https://efts.sec.gov/LATEST/search-index",
            params={"q": f'"{term}"',
                    "startdt": start.isoformat(), "enddt": end.isoformat()},
            headers={"User-Agent": "narrative-scout/1.0 (redsky0218@gmail.com)"},
            timeout=20,
        )
        resp.raise_for_status()
        hits = resp.json().get("hits", {}).get("hits", [])
    except Exception as e:  # noqa: BLE001
        print(f"[경고] SEC 검색 실패({term}): {e}")
        return []
    out = []
    for h in hits[:max_hits]:
        s = h.get("_source", {})
        names = s.get("display_names") or []
        name = re.sub(r"\s*\(CIK[^)]*\)", "", names[0]).strip() if names else "?"
        out.append({"name": name,
                    "form": s.get("file_type") or "",
                    "date": s.get("file_date") or ""})
    return out


def sec_query_term(term):
    """'HBF (High-Bandwidth Flash)' -> 더 구체적인 쪽(긴 쪽)을 검색어로."""
    m = re.match(r"(.+?)\s*\((.+)\)\s*$", (term or "").strip())
    if m:
        outer, inner = m.group(1).strip(), m.group(2).strip()
        return inner if len(inner) >= len(outer) else outer
    return (term or "").strip()


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

SEPARATELY, also fill "emerging_tech": a list of NEW technology terms, products,
standards, materials, or acronyms that are just starting to be discussed — the
EARLIEST possible signal (like "HBF" before anyone knew which stock benefits).
- Include an item here EVEN IF there is no clear public-stock beneficiary yet.
  This list is specifically for catching the technology itself as early as possible.
- For each: term(정확한 용어/약어), what(무엇인지 한 줄), why_notable(왜 주목할 가치가
  있는지), maybe_tickers(관련 상장사가 떠오르면 넣고, 없으면 빈 배열),
  market_size(시장 규모), cagr(연평균 예상 성장률).
- market_size/cagr 작성 요령: 그 기술 자체의 시장이 아직 없으면(대부분 그렇다),
  그 기술이 노리는/대체하려는 인접 시장의 수치를 써라.
  예: HBF -> market_size "HBM 시장 약 $250억 (2025)", cagr "연 ~30% (2030년까지)".
  '(대상 시장명) + 규모' 형태. 알고 있는 시장조사 수치 기반의 대략치만 쓰고,
  정말 모르면 "미상" 이라고 써라. 지어내지 마라.
- Prefer genuinely novel/niche terms over well-known ones. Skip generic buzzwords.
- term 은 원문 그대로(영문 약어 등), 설명은 한국어로.

For EVERY narrative and emerging_tech item, fill "source_ids": the [N] numbers of
the community posts (from the corpus headers like "### [12] r/hardware") that the
item is actually based on. Only cite posts you really used. 1-4 ids each.
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
                    "source_ids": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                },
                "required": ["name", "summary", "tickers", "rationale",
                             "stage", "confidence", "source_ids"],
            },
        },
        "emerging_tech": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "term": {"type": "STRING"},
                    "what": {"type": "STRING"},
                    "why_notable": {"type": "STRING"},
                    "maybe_tickers": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "market_size": {"type": "STRING"},
                    "cagr": {"type": "STRING"},
                    "source_ids": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                },
                "required": ["term", "what", "why_notable", "maybe_tickers",
                             "market_size", "cagr", "source_ids"],
            },
        },
    },
    "required": ["narratives", "emerging_tech"],
}


def analyze_with_gemini(cfg, api_key, corpus):
    """Google Gemini(무료 티어) REST API 로 내러티브 추출. 구조화 JSON 강제."""
    lc = cfg["llm"]
    model = lc.get("model", "gemini-2.0-flash")

    user_msg = (
        f"감시 테마: {cfg['focus']}\n\n"
        f"narratives 는 최대 {lc.get('max_narratives', 6)}개까지, 가장 유망한 초기 "
        f"내러티브(수혜주 포함)만 골라라.\n"
        f"emerging_tech 는 최대 {lc.get('max_tech', 8)}개까지, 수혜주가 아직 없어도 "
        f"'막 논의되기 시작한 새 기술/용어' 를 최대한 이르게 포착해서 담아라.\n\n"
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
        return [], []

    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        print("[오류] Gemini 응답 파싱 실패:", str(data)[:400])
        return [], []

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        print("[오류] Gemini JSON 파싱 실패:\n", text[:500])
        return [], []

    um = data.get("usageMetadata", {})
    print(f"[Gemini] 입력 {um.get('promptTokenCount', '?')} / "
          f"출력 {um.get('candidatesTokenCount', '?')} 토큰")
    return parsed.get("narratives", []), parsed.get("emerging_tech", [])


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
            try:                     # 멀티인덱스(티커별) 형태 우선
                close = data[t]["Close"].dropna()
                vol = data[t]["Volume"].dropna()
            except (KeyError, IndexError):
                close = data["Close"].dropna()
                vol = data["Volume"].dropna()
            if close is None or close.empty:
                continue
            price = float(close.iloc[-1])

            def pct(days):
                if len(close) > days:
                    past = float(close.iloc[-1 - days])
                    return (price / past - 1) * 100 if past else None
                return None

            # 거래량 이상: 최근 거래량 / 30일 평균 (2.5x 이상 = 자금 유입 흔적)
            vol_ratio = None
            try:
                if vol is not None and len(vol) > 10:
                    base = float(vol.tail(30).mean())
                    if base > 0:
                        vol_ratio = float(vol.iloc[-1]) / base
            except Exception:  # noqa: BLE001
                pass

            hi = float(close.max())
            out[t] = {
                "price": price,
                "chg_1m": pct(21),
                "chg_3m": pct(63),
                "off_high": (price / hi - 1) * 100 if hi else None,
                "vol_ratio": vol_ratio,
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
    """🆕 최초포착 / 🔁 지속(며칠) / ⚡ 언급 가속(최근 7일 급증) 판정."""
    days = hist_days.get((ticker or "").strip().upper(), set())
    prior = days - {today}
    if not prior:
        return "🆕", True, 0, False
    now = datetime.now(KST)
    cut7 = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    cut14 = (now - timedelta(days=14)).strftime("%Y-%m-%d")
    recent7 = len([d for d in days if d >= cut7])
    prior7 = len([d for d in days if cut14 <= d < cut7])
    # 직전 주 대비 등장일수 2배 이상 & 3일 이상 = 언급 가속
    accel = recent7 >= 3 and recent7 >= 2 * max(prior7, 1)
    return f"🔁{len(prior)}일", False, recent7, accel


def opportunity_score(n, tickers_meta):
    """조기·저평가 기회 점수 0~100.
    단계(초기일수록↑) + 신뢰도 + 신규성 + '아직 안 오른 정도' 를 합산."""
    stage_pts = {"very_early": 40, "emerging": 28,
                 "gaining_traction": 15}.get(n.get("stage"), 20)
    conf_pts = {"high": 25, "medium": 15, "low": 8}.get(n.get("confidence"), 12)

    is_new = any(m.get("is_new") for m in tickers_meta)
    novelty_pts = 20 if is_new else 8
    if any(m.get("accel") for m in tickers_meta):
        novelty_pts += 5            # ⚡ 언급 가속 보너스

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

    # 🔥 거래량 이상(2.5x+) = 자금이 먼저 움직이는 흔적
    if any(((m.get("price") or {}).get("vol_ratio") or 0) >= 2.5
           for m in tickers_meta):
        price_pts += 6

    # 🔗 교차출처(Reddit·HN·arXiv 중 2종 이상) = 독립 확인된 신호
    cross_pts = 5 if n.get("_src_kinds", 0) >= 2 else 0

    total = stage_pts + conf_pts + novelty_pts + price_pts + cross_pts
    return max(0, min(100, total))


def enrich_narratives(narratives, today, posts=None):
    """각 내러티브에 티커별 가격·신규태그 메타를 붙이고 점수 계산 후 정렬."""
    for n in narratives:
        n["tickers"] = clean_tickers(n.get("tickers", []))
        # 교차출처: 근거 글이 몇 종류의 소스(reddit/HN/arXiv)에서 왔는지
        kinds = set()
        for sid in n.get("source_ids") or []:
            try:
                src = (posts or [])[int(sid) - 1].get("source", "")
            except (ValueError, TypeError, IndexError):
                continue
            if src.startswith("arXiv"):
                kinds.add("arxiv")
            elif src.startswith("HN"):
                kinds.add("hn")
            elif src.startswith("Lobsters"):
                kinds.add("lobsters")
            elif src.startswith("StockTwits"):
                kinds.add("stocktwits")
            elif src.startswith("GitHub"):
                kinds.add("github")
            else:
                kinds.add("reddit")
        n["_src_kinds"] = len(kinds)
    all_tickers = [t for n in narratives for t in n["tickers"]]
    prices = price_snapshot(all_tickers)
    hist_days = ticker_history_days()
    for n in narratives:
        metas = []
        for t in n["tickers"]:
            tag, is_new, recent7, accel = novelty_tag(t, today, hist_days)
            metas.append({"ticker": t,
                          "price": prices.get(t),
                          "tag": tag, "is_new": is_new, "recent7": recent7,
                          "accel": accel})
        n["_meta"] = metas
        n["_score"] = opportunity_score(n, metas)
    narratives.sort(key=lambda x: x.get("_score", 0), reverse=True)
    return narratives


def fmt_sources(item, posts):
    """source_ids -> '📎 출처: r/hardware, HN(85p)' + 링크. 잘못된 id 는 무시."""
    ids = item.get("source_ids") or []
    links = []
    for sid in ids[:4]:
        try:
            p = posts[int(sid) - 1]          # corpus 는 [1]부터 번호
        except (ValueError, TypeError, IndexError):
            continue
        title = html.escape((p.get("title") or "")[:60])
        url = html.escape(p.get("url") or "", quote=True)
        src = html.escape(p.get("source") or "")
        if url:
            links.append(f'<a href="{url}">{src}</a>')
        else:
            links.append(src or title)
    if not links:
        return None
    return "📎 출처: " + " · ".join(links)


def fmt_ticker_line(m):
    """'• $AAOI 🆕⚡  $12.30 (1M +3% · 3M -8% · 고점 -22%)' 형태."""
    tag = m["tag"] + ("⚡" if m.get("accel") else "")
    parts = [f"• <b>${m['ticker']}</b> {tag}"]
    p = m.get("price")
    if p:
        seg = [f"${p['price']:.2f}"]
        mom = []
        for label, key in (("1M", "chg_1m"), ("3M", "chg_3m"), ("고점", "off_high")):
            v = p.get(key)
            if v is not None:
                mom.append(f"{label} {v:+.0f}%")
        vr = p.get("vol_ratio")
        if vr is not None and vr >= 2.0:
            mom.append(f"거래량 {vr:.1f}x🔥")
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


def clean_ticker(raw):
    """LLM 이 'XYLEM (XYL)' 처럼 회사명을 섞어 줄 때 순수 티커만 추출.
    유효하지 않으면 None."""
    s = (raw or "").strip().upper()
    m = re.search(r"\(([A-Z]{1,6}(?:\.[A-Z])?)\)", s)   # 괄호 안 티커 우선
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Z]{1,6}(?:\.[A-Z])?", s):
        return s
    return None


def clean_tickers(lst):
    out = []
    for t in lst or []:
        c = clean_ticker(t)
        if c and c not in out:
            out.append(c)
    return out


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

    print("1) 소스 수집 중… (Reddit·HN·arXiv·Lobsters·StockTwits·GitHub)")
    posts = collect_reddit_posts(cfg)
    print(f"   -> Reddit {len(posts)}개")
    hn = collect_hn_posts(cfg)
    print(f"   -> Hacker News {len(hn)}개")
    ax = collect_arxiv_posts(cfg)
    print(f"   -> arXiv {len(ax)}개")
    lb = collect_lobsters_posts(cfg)
    print(f"   -> Lobsters {len(lb)}개")
    st = collect_stocktwits_posts(cfg)
    print(f"   -> StockTwits {len(st)}개")
    gh = collect_github_posts(cfg)
    print(f"   -> GitHub {len(gh)}개")
    posts += hn + ax + lb + st + gh
    print(f"   -> 합계 {len(posts)}개 글 수집")
    if not posts:
        print("수집된 글 없음. 종료.")
        return

    print("2) Gemini 분석 중…")
    corpus = build_corpus(posts)
    narratives, tech = analyze_with_gemini(cfg, gemini_key, corpus)
    print(f"   -> 내러티브 {len(narratives)}개 / 기술신호 {len(tech)}개 추출")

    # 이력 기록 (재등장 빈도 집계용 - 중복 제거 전 전체 기록)
    append_history(narratives)

    # 3) 중복 제거
    state = load_json(STATE_PATH, default={"seen": {}})
    seen = state.setdefault("seen", {})
    seen_tech = state.setdefault("seen_tech", {})
    today = datetime.now(KST).strftime("%Y-%m-%d")

    fresh = []
    for n in narratives:
        key = norm_key(n.get("name", ""))
        if not key or key in seen:
            continue
        seen[key] = {"name": n.get("name"), "first_seen": today,
                     "tickers": n.get("tickers", [])}
        fresh.append(n)

    fresh_tech = []
    for tt in tech:
        key = norm_key(tt.get("term", ""))
        if not key or key in seen_tech:
            continue
        seen_tech[key] = {"term": tt.get("term"), "first_seen": today}
        fresh_tech.append(tt)

    save_state(state)

    if not fresh and not fresh_tech:
        print("새 내러티브·기술신호 없음. (알림 미전송)")
        return

    now_kst = datetime.now(KST)
    SEP = "──────────────────"
    lines = [f"🔍 <b>새 유망 신호 감지</b>",
             f"<i>{now_kst:%m-%d %H:%M} KST · 🆕최초 🔁지속 ⚡가속 🔥거래량 🔗교차</i>"]

    # 4) 내러티브: 가격·모멘텀·신규성 enrich + 조기기회 점수순
    if fresh:
        print("3) 종목 가격·모멘텀 조회 중…")
        fresh = enrich_narratives(fresh, today, posts)
        lines.append("")
        lines.append("💡 <b>유망 내러티브</b> (조기·저평가 점수순)")
        for n in fresh:
            stage = STAGE_KO.get(n.get("stage"), n.get("stage", ""))
            conf = CONF_KO.get(n.get("confidence"), n.get("confidence", ""))
            score = n.get("_score", 0)
            star = "🌟 " if score >= 70 else ""
            cross = " · 🔗교차" if n.get("_src_kinds", 0) >= 2 else ""
            lines.append("")
            lines.append(SEP)
            lines.append(f"{star}<b>[{score}점] {n.get('name')}</b>")
            lines.append(f"{stage} · 신뢰도 {conf}{cross}")
            lines.append("")
            lines.append(f"{n.get('summary','')}")
            lines.append("")
            lines.append(f"💬 <i>{n.get('rationale','')}</i>")
            metas = n.get("_meta", [])
            if metas:
                lines.append("")
                for m in metas:
                    lines.append(fmt_ticker_line(m))
            src_line = fmt_sources(n, posts)
            if src_line:
                lines.append(src_line)

    # 5) 기술 신호: 수혜주가 아직 없어도 '기술 자체'를 가장 이르게 포착
    if fresh_tech:
        # SEC 공시 교차확인: 이 용어가 실제 기업 공시에 등장했는가?
        sec_cfg = cfg.get("sec", {})
        if sec_cfg.get("enabled", True):
            print("4) SEC 공시 교차확인 중…")
            for tt in fresh_tech[:sec_cfg.get("max_terms", 6)]:
                q = sec_query_term(tt.get("term", ""))
                if len(q) >= 4:                  # 너무 짧은 약어는 오탐 방지
                    tt["_sec"] = sec_fulltext_hits(
                        q, sec_cfg.get("lookback_days", 90))
                    time.sleep(0.4)

        lines.append("")
        lines.append("")
        lines.append("🔬 <b>새로 포착된 기술/용어</b>")
        lines.append("<i>수혜주가 없어도 기술 자체를 먼저 잡습니다</i>")
        for tt in fresh_tech:
            term = tt.get("term", "")
            mt = [f"${t}" for t in clean_tickers(tt.get("maybe_tickers", []))]
            lines.append("")
            lines.append(SEP)
            lines.append(f"🔬 <b>{term}</b>")
            lines.append(f"{tt.get('what','')}")
            lines.append("")
            lines.append(f"💬 <i>{tt.get('why_notable','')}</i>")
            lines.append("")
            ms = (tt.get("market_size") or "").strip()
            cg = (tt.get("cagr") or "").strip()
            if ms or cg:
                seg = []
                if ms and ms != "미상":
                    seg.append(f"시장 {ms}")
                if cg and cg != "미상":
                    seg.append(f"성장률 {cg}")
                if seg:
                    lines.append("📊 " + " · ".join(seg) + " <i>(AI 추정)</i>")
            if mt:
                lines.append("📈 관련주: " + " ".join(mt))
            else:
                lines.append("📈 관련주: <i>아직 없음 (극초기 신호)</i>")
            for h in tt.get("_sec", []):
                lines.append(f"🏛️ SEC 공시: <b>{html.escape(h['name'])}</b>"
                             f" · {h['form']} {h['date']}")
            src_line = fmt_sources(tt, posts)
            if src_line:
                lines.append(src_line)

    msg = "\n".join(lines).strip()
    try:
        for chunk in split_message(msg, 3800):
            send_telegram(tg_token, tg_chat, chunk)
        print(f"텔레그램 전송 완료 (내러티브 {len(fresh)} · 기술신호 {len(fresh_tech)}).")
    except Exception as e:  # noqa: BLE001
        print(f"[오류] 텔레그램 전송 실패: {e}")


if __name__ == "__main__":
    main()
