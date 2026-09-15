"""Web tools — Tavily Search + urllib fetch + financial data APIs.

Tavily (https://tavily.com) handles all web search with structured results.
web_fetch still uses urllib for direct URL access.
Financial data (market_series) uses Yahoo/Macrotrends/Tencent APIs.
"""

import sys
import os
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
    import re as _vre
    
    # ── Curated: known reliable URLs for common queries ──
    query_lower = query.lower()
    curated = None

    # 天气：直连数据接口。网页是 JS 壳，Tavily 也给不了实时气温，
    # 所以这里必须短路到 weather()，不能落到下面的 Tavily 分支。
    if _vre.search(r"天气|气温|降雨|下雨|台风|空气质量|weather", query_lower):
        return weather(_extract_city(query))

    if _vre.search(r"gold|金价|黄金|gold price", query_lower):
        curated = (
            "📊 Gold Price Data Sources (reliable, no API needed):\n"
            "1. Kitco 30-day chart\n"
            "   https://www.kitco.com/charts/livegold.html\n"
            "2. Macrotrends historical gold prices\n"
            "   https://www.macrotrends.net/1333/historical-gold-prices-100-year-chart\n"
            "3. APMEX gold spot price\n"
            "   https://www.apmex.com/gold-price\n\n"
            "Use web_fetch on macrotrends.net for historical monthly data (table format).\n"
            "Use web_fetch on apmex.com for current spot price."
        )
    elif _vre.search(r"silver|银价|白银", query_lower):
        curated = (
            "📊 Silver Price Sources:\n"
            "1. https://www.macrotrends.net/1470/historical-silver-prices-100-year-chart\n"
            "2. https://www.apmex.com/silver-price\n"
        )
    elif _vre.search(r"bitcoin|btc|比特币", query_lower):
        curated = (
            "📊 Bitcoin Price Sources:\n"
            "1. https://www.coindesk.com/price/bitcoin\n"
            "2. https://coinmarketcap.com/currencies/bitcoin/historical-data/\n"
        )
    
    if curated:
        return curated
    
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


# ─── Weather ────────────────────────────────────
# 天气**不能**走 web_search / web_fetch：中国天气网的网页是 JS 壳
# （抓 cityinfo/101090801.html 只会得到"正在努力为您加载"），
# 数据在 d1.weather.com.cn 的 JSON 接口里，只多要一个 Referer 头。
# 主源：中国天气网（权威、无需 key）；兜底：open-meteo（无需 key）。

DEFAULT_WEATHER_CITY = os.environ.get("AI_COMPANY_DEFAULT_CITY", "衡水")

_WEATHER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "http://www.weather.com.cn/",
}

# WMO weather_code → 中文（open-meteo 兜底源用）
_WMO_CN = {
    0: "晴", 1: "少云", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 56: "冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨", 85: "小阵雪", 86: "大阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}

# 问句里不是城市名的词，避免"今天天气"把"今天"当城市
_WEATHER_NON_CITY = {
    "今天", "明天", "后天", "现在", "当地", "这里", "那边", "今日",
    "请问", "查询", "看看", "如何", "怎么", "情况", "预报", "气温", "天气",
}


def _extract_city(text: str) -> str:
    """从问句里抠城市名（"衡水天气" → 衡水）；抠不到用默认城市。"""
    m = re.search(r"([\u4e00-\u9fff]{2,8}?)(?:今天|明天|后天|现在)?(?:的)?(?:天气|气温|预报)", text)
    if m and m.group(1) not in _WEATHER_NON_CITY:
        return m.group(1)
    m = re.search(r"(?:天气|气温|预报)[^\u4e00-\u9fff]{0,4}([\u4e00-\u9fff]{2,8})", text)
    if m and m.group(1) not in _WEATHER_NON_CITY:
        return m.group(1)
    # 英文问法（weather in Tokyo）走 open-meteo 的地理编码，
    # 不回落默认城市 —— 否则"问东京答衡水"就是静默答错。
    m = re.search(
        r"(?:weather|temperature|forecast)\s*(?:in|of|for|at)?\s*([A-Za-z][A-Za-z\s\-']{1,30})",
        text,
        re.IGNORECASE,
    )
    if m:
        city = m.group(1).strip(" -'")
        city = re.sub(r"\b(today|tomorrow|now|please|how|is|the)\b", " ", city, flags=re.IGNORECASE)
        city = " ".join(city.split())
        if city:
            return city
    return DEFAULT_WEATHER_CITY


def _resolve_cn_city_code(city: str) -> str:
    """城市名 → 中国天气网编码（衡水 → 101090801）。无需 key。"""
    url = "http://toy1.weather.com.cn/search?cityname=" + urllib.parse.quote(city)
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=_WEATHER_HEADERS), timeout=10
    ) as resp:
        raw = resp.read().decode("utf-8", "replace")
    m = re.search(r'"ref"\s*:\s*"(\d{9})', raw)
    return m.group(1) if m else ""


def _fetch_cn_weather(code: str) -> dict:
    """拉实时（sk_2d）+ 今日预报/预警（weather_index）。"""
    out: dict = {}
    with urllib.request.urlopen(
        urllib.request.Request(f"http://d1.weather.com.cn/sk_2d/{code}.html", headers=_WEATHER_HEADERS),
        timeout=10,
    ) as resp:
        raw = resp.read().decode("utf-8", "replace")
    m = re.search(r"=\s*(\{.*\})", raw, re.DOTALL)
    if m:
        out["realtime"] = json.loads(m.group(1))

    with urllib.request.urlopen(
        urllib.request.Request(
            f"http://d1.weather.com.cn/weather_index/{code}.html", headers=_WEATHER_HEADERS
        ),
        timeout=10,
    ) as resp:
        raw2 = resp.read().decode("utf-8", "replace")
    m2 = re.search(r"cityDZ\s*=\s*(\{.*?\});", raw2, re.DOTALL)
    if m2:
        out["today"] = json.loads(m2.group(1)).get("weatherinfo") or {}
    m3 = re.search(r"alarmDZ\s*=\s*(\{.*?\});", raw2, re.DOTALL)
    if m3:
        out["alarms"] = (json.loads(m3.group(1)) or {}).get("w") or []
    return out


def _fetch_open_meteo_weather(city: str) -> dict:
    """兜底源：open-meteo（无需 key，非中国城市也能用）。"""
    geo_url = (
        "https://geocoding-api.open-meteo.com/v1/search?name="
        + urllib.parse.quote(city)
        + "&count=1&language=zh"
    )
    with urllib.request.urlopen(
        urllib.request.Request(geo_url, headers={"User-Agent": _next_ua(), "Accept": "application/json"}),
        timeout=12,
    ) as resp:
        hits = json.loads(resp.read()).get("results") or []
    if not hits:
        return {}
    loc = hits[0]
    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={loc['latitude']}"
        f"&longitude={loc['longitude']}"
        "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
        "precipitation,wind_speed_10m,weather_code"
        "&daily=temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max"
        "&forecast_days=3&timezone=Asia%2FShanghai"
    )
    with urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": _next_ua()}), timeout=12
    ) as resp:
        return json.loads(resp.read())


def weather(city: str = "") -> str:
    """查某城市实时天气 + 今日预报；返回中文文本（含来源）。"""
    city = (city or "").strip() or DEFAULT_WEATHER_CITY
    data_lines: list[str] = []
    err_notes: list[str] = []

    code = ""
    try:
        code = _resolve_cn_city_code(city)
    except Exception as e:
        err_notes.append(f"城市编码解析失败：{e}")

    if code:
        try:
            data = _fetch_cn_weather(code)
            rt = data.get("realtime") or {}
            if rt.get("temp"):
                data_lines.append(
                    f"{rt.get('cityname', city)} 实时天气（{rt.get('date', '')} {rt.get('time', '')} 更新）："
                )
                data_lines.append(f"  天气：{rt.get('weather', '?')}　气温：{rt.get('temp', '?')}°C")
                data_lines.append(
                    f"  风：{rt.get('WD', '?')} {rt.get('WS', '?')}　湿度：{rt.get('SD', '?')}"
                    f"　气压：{rt.get('qy', '?')}hPa　能见度：{rt.get('njd', '?')}"
                )
                data_lines.append(
                    f"  降水：{rt.get('rain', '0')}mm（24h {rt.get('rain24h', '0')}mm）　AQI：{rt.get('aqi', '?')}"
                )
            today = data.get("today") or {}
            if today.get("temp") and today.get("temp") != "999":
                data_lines.append(
                    f"  今日预报：{today.get('weather', '?')}　{today.get('temp', '?')}"
                    f"　夜间 {today.get('tempn', '?')}　{today.get('wd', '?')} {today.get('ws', '?')}"
                )
            alarms = data.get("alarms") or []
            if alarms:
                titles = "、".join(str((a or {}).get("w1", "")) for a in alarms[:3])
                data_lines.append(f"  ⚠ 气象预警 {len(alarms)} 条：{titles}")
            if data_lines:
                data_lines.append(f"  来源：http://www.weather.com.cn/weather/{code}.shtml")
        except Exception as e:
            err_notes.append(f"中国天气网取数失败：{e}")

    if not data_lines:
        try:
            om = _fetch_open_meteo_weather(city)
            cur = om.get("current") or {}
            daily = om.get("daily") or {}
            if cur:
                data_lines.append(f"{city} 实时天气（open-meteo 备用源）：")
                data_lines.append(
                    f"  天气：{_WMO_CN.get(int(cur.get('weather_code') or -1), '?')}　"
                    f"气温：{cur.get('temperature_2m', '?')}°C（体感 {cur.get('apparent_temperature', '?')}°C）"
                )
                data_lines.append(
                    f"  湿度：{cur.get('relative_humidity_2m', '?')}%　"
                    f"风速：{cur.get('wind_speed_10m', '?')}km/h　降水：{cur.get('precipitation', '?')}mm"
                )
                dates = daily.get("time") or []
                for i, d in enumerate(dates[:3]):
                    hi = (daily.get("temperature_2m_max") or [None] * 3)[i]
                    lo = (daily.get("temperature_2m_min") or [None] * 3)[i]
                    wc = (daily.get("weather_code") or [None] * 3)[i]
                    pop = (daily.get("precipitation_probability_max") or [None] * 3)[i]
                    data_lines.append(
                        f"  {d}：{_WMO_CN.get(int(wc) if wc is not None else -1, '?')}　{lo}~{hi}°C　降水概率 {pop}%"
                    )
                data_lines.append("  来源：https://open-meteo.com/")
        except Exception as e:
            err_notes.append(f"open-meteo 备用源失败：{e}")

    if data_lines:
        if err_notes:
            data_lines.append("  （提示：" + "；".join(err_notes) + "）")
        return "\n".join(data_lines)
    # 两源全挂：必须以"未能获取"开头 —— executor 靠这个前缀把它判为工具失败，
    # 否则模型会拿到一条"成功"的空结果然后自己编。
    detail = ("；".join(err_notes)) if err_notes else "中国天气网无编码（可能不是中国城市）、open-meteo 也无结果"
    return f"未能获取 {city} 的天气数据：{detail}"


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
    elif cmd == "weather":
        print(weather(sys.argv[2] if len(sys.argv) > 2 else ""))
    elif cmd == "market_series":
        print(market_series(sys.argv[2] if len(sys.argv) > 2 else "", 30))
    else:
        print(f"Usage: {sys.argv[0]} search|fetch|weather|market_series <query|url|city> [limit]")
