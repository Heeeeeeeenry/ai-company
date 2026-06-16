"""Web tools — Tavily Search + urllib fetch + financial data APIs.

Tavily (https://tavily.com) handles all web search with structured results.
web_fetch still uses urllib for direct URL access.
Financial data (market_series) uses Yahoo/Macrotrends/Tencent APIs.
"""

import sys
import urllib.request
import urllib.parse
import urllib.error
import re
import json
from datetime import datetime, UTC

from src.config import config

# ─── Tavily Search ──────────────────────────────

_tavily_client = None


def _get_tavily():
    global _tavily_client
    if _tavily_client is None:
        try:
            from tavily import TavilyClient
            _tavily_client = TavilyClient(api_key=config.tavily_api_key)
        except ImportError:
            return None
        except Exception:
            return None
    return _tavily_client


def web_search(query: str, max_results: int = 5) -> str:
    """Search the web using Tavily API. Returns structured results."""
    client = _get_tavily()
    if not client:
        return "SEARCH UNAVAILABLE: Tavily not installed. Run: pip install tavily-python"

    try:
        response = client.search(
            query=query,
            max_results=max_results,
            search_depth="basic",
            include_answer=True,
        )

        lines = []

        # Tavily's AI-generated answer (best for quick facts)
        answer = response.get("answer", "")
        if answer:
            lines.append(f"Answer: {answer}\n")

        # Structured results with content snippets
        results = response.get("results", [])
        for i, r in enumerate(results[:max_results], 1):
            title = r.get("title", "?")[:120]
            url = r.get("url", "")
            content = r.get("content", "")[:300]
            score = r.get("score", 0)
            lines.append(
                f"{i}. {title}\n"
                f"   {content}\n"
                f"   URL: {url}  (relevance: {score:.2f})"
            )

        if not lines:
            return f"No results found for '{query}'."

        return "\n\n".join(lines)

    except Exception as e:
        return f"SEARCH FAILED (Tavily): {e}"


# ─── Web Fetch ──────────────────────────────────

def web_fetch(url: str, max_chars: int = 5000) -> str:
    """Fetch a URL and return text content."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()

        for encoding in ["utf-8", "latin-1", "cp1252"]:
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            return f"ERROR: Cannot decode response from {url}"

        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        return text[:max_chars]

    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.reason} for {url}"
    except Exception as e:
        return f"Network error: {e} for {url}"


# ─── Financial Market Data ──────────────────────
# (Yahoo chart, Macrotrends monthly, Tencent/Eastmoney K-line)
# Kept from the original implementation for structured market data queries.

_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]
_ua_index = 0


def _next_ua():
    global _ua_index
    ua = _USER_AGENTS[_ua_index % len(_USER_AGENTS)]
    _ua_index += 1
    return ua


def _extract_cn_stock_symbol(text: str):
    m = re.search(r"\b(sh|sz)\s*(\d{6})\b", text, re.IGNORECASE)
    if m:
        return f"{m.group(1).lower()}{m.group(2)}"
    m = re.search(r"\b(\d{6})\b", text)
    if m:
        code = m.group(1)
        return f"{'sh' if code.startswith('6') else 'sz'}{code}"
    return None


def _build_tencent_kline_url(symbol: str) -> str:
    return (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={urllib.parse.quote(symbol)},day,,,30,qfq"
    )


def _fetch_tencent_kline(url: str):
    req = urllib.request.Request(url, headers={
        "User-Agent": _next_ua(), "Accept": "application/json",
        "Referer": "https://gu.qq.com/",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read())
    data = payload.get("data") or {}
    symbol = next(iter(data.keys()), None)
    if not symbol:
        return None
    series = (data[symbol] or {}).get("qfqday") or (data[symbol] or {}).get("day") or []
    if not series:
        return None
    lines = [symbol, "Daily close (most recent last):"]
    parsed = []
    for row in series[-30:]:
        if not isinstance(row, list) or len(row) < 6:
            continue
        date_str, _, close_p, _, _, _ = row[:6]
        parsed.append((date_str, close_p))
        lines.append(f"{date_str}: {close_p}")
    if len(parsed) >= 2:
        fc, lc = float(parsed[0][1]), float(parsed[-1][1])
        pct = (lc - fc) / fc * 100 if fc else 0
        lines.append(f"Change: {parsed[0][0]}->{parsed[-1][0]} {fc}->{lc} ({pct:.2f}%)")
    lines.append(f"Source: {url}")
    return "\n".join(lines)


def _fetch_macrotrends_monthly(url: str):
    m = re.search(r"macrotrends\.net/(?:datasets/)?(\d+)/", url)
    if not m:
        return None
    endpoint = f"https://www.macrotrends.net/economic-data/{m.group(1)}/INDEXMONTHLY"
    req = urllib.request.Request(endpoint, headers={
        "User-Agent": _next_ua(), "Accept": "application/json", "Referer": url,
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read())
    points = payload.get("data", [])
    metadata = payload.get("metadata", {})
    if not points:
        return None
    recent = list(reversed(points[-6:]))
    lines = [metadata.get("name", "Data"), "Monthly (most recent first):"]
    for ts, val in recent:
        lines.append(f"{datetime.fromtimestamp(ts/1000, UTC).strftime('%Y-%m-%d')}: {val}")
    lines.append(f"Source: {url}")
    return "\n".join(lines)


def _fetch_yahoo_chart(symbol: str, range_: str = "1mo", interval: str = "1d"):
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
        f"?range={urllib.parse.quote(range_)}&interval={urllib.parse.quote(interval)}"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": _next_ua(), "Accept": "application/json",
        "Referer": "https://finance.yahoo.com/",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read())
    chart = (payload.get("chart") or {}).get("result") or []
    if not chart:
        return None
    item = chart[0] or {}
    ts = item.get("timestamp") or []
    closes = ((item.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    pairs = []
    for i, t in enumerate(ts):
        if i >= len(closes) or closes[i] is None:
            continue
        pairs.append((datetime.fromtimestamp(int(t), UTC).strftime("%Y-%m-%d"), closes[i]))
    if not pairs:
        return None
    lines = [f"{symbol}", "Daily close (most recent last):"]
    for d, c in pairs[-30:]:
        lines.append(f"{d}: {c}")
    if len(pairs) >= 2:
        fc, lc = float(pairs[0][1]), float(pairs[-1][1])
        pct = (lc - fc) / fc * 100 if fc else 0
        lines.append(f"Change: {pairs[0][0]}->{pairs[-1][0]} {fc}->{lc} ({pct:.2f}%)")
    lines.append(f"Source: https://finance.yahoo.com/quote/{symbol}")
    return "\n".join(lines)


def market_series(query: str, max_points: int = 30) -> str:
    """Get structured time-series for market data (stocks, gold, etc.)."""
    q = query.strip().lower()

    if "gold" in q or "金价" in q:
        url = "https://www.macrotrends.net/1333/historical-gold-prices-100-year-chart"
        text = _fetch_macrotrends_monthly(url)
        return text or "Network error: Unable to fetch gold price series."

    # CN stocks
    symbol = _extract_cn_stock_symbol(query)
    if not symbol:
        m = re.search(r"[\u4e00-\u9fff]{2,}", query)
        if m:
            try:
                url = f"https://searchapi.eastmoney.com/api/suggest/get?input={urllib.parse.quote(m.group(0))}&type=14&count=5"
                req = urllib.request.Request(url, headers={"User-Agent": _next_ua(), "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                table = data.get("QuotationCodeTable") or {}
                candidates = table.get("Data") or []
                if candidates:
                    symbol = _extract_cn_stock_symbol(str(candidates[0].get("Code", "")))
            except Exception:
                pass
    if symbol:
        text = _fetch_tencent_kline(_build_tencent_kline_url(symbol))
        if text:
            return text
        return "Network error: Unable to fetch CN stock series."

    # US/international tickers
    m = re.search(r"\b[A-Z]{1,5}\b", query)
    if m:
        try:
            text = _fetch_yahoo_chart(m.group(0), range_="1mo")
            if text:
                return text
        except Exception:
            pass

    return "Unsupported query. Provide a ticker like AAPL, 600519, or 'gold'."


# ─── CLI entry point ────────────────────────────

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "search":
        query = sys.argv[2] if len(sys.argv) > 2 else ""
        max_r = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        print(web_search(query, max_r))
    elif cmd == "fetch":
        url = sys.argv[2] if len(sys.argv) > 2 else ""
        max_c = int(sys.argv[3]) if len(sys.argv) > 3 else 5000
        print(web_fetch(url, max_c))
    else:
        print(f"Usage: {sys.argv[0]} search|fetch <query|url> [limit]")
