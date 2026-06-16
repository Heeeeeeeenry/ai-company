"""URL Knowledge Base — maps query patterns to known reliable data sources.

When web_search fails, the Researcher falls back to these URLs instead of guessing.
Covers: stocks, commodities, currencies, weather, Wikipedia for general topics.

Pattern: (keywords, URL template with {symbol} placeholder)
"""

import re
from typing import Optional

# Stock ticker aliases → Yahoo Finance symbol
_STOCK_ALIASES = {
    "苹果": "AAPL", "apple": "AAPL",
    "微软": "MSFT", "microsoft": "MSFT",
    "谷歌": "GOOGL", "google": "GOOGL", "alphabet": "GOOGL",
    "亚马逊": "AMZN", "amazon": "AMZN",
    "特斯拉": "TSLA", "tesla": "TSLA",
    "英伟达": "NVDA", "nvidia": "NVDA",
    "meta": "META", "facebook": "META",
    "百度": "BIDU", "baidu": "BIDU",
    "阿里巴巴": "BABA", "alibaba": "BABA",
    "腾讯": "TCEHY", "tencent": "TCEHY",
    "台积电": "TSM", "tsmc": "TSM",
    "amd": "AMD", "intel": "INTC",
    "netflix": "NFLX", "奈飞": "NFLX",
    "uber": "UBER", "airbnb": "ABNB",
    "spacex": None,  # Private — no stock
}

# Commodity → Wikipedia / source URL
_COMMODITY_URLS = {
    "金价": "https://en.wikipedia.org/wiki/Gold_as_an_investment",
    "黄金": "https://en.wikipedia.org/wiki/Gold_as_an_investment",
    "gold": "https://en.wikipedia.org/wiki/Gold_as_an_investment",
    "银价": "https://en.wikipedia.org/wiki/Silver_as_an_investment",
    "白银": "https://en.wikipedia.org/wiki/Silver_as_an_investment",
    "silver": "https://en.wikipedia.org/wiki/Silver_as_an_investment",
    "油价": "https://en.wikipedia.org/wiki/Price_of_oil",
    "石油": "https://en.wikipedia.org/wiki/Price_of_oil",
    "oil": "https://en.wikipedia.org/wiki/Price_of_oil",
    "铜价": "https://en.wikipedia.org/wiki/Copper",
    "copper": "https://en.wikipedia.org/wiki/Copper",
    "比特币": "https://en.wikipedia.org/wiki/Bitcoin",
    "bitcoin": "https://en.wikipedia.org/wiki/Bitcoin",
    "btc": "https://en.wikipedia.org/wiki/Bitcoin",
    "以太坊": "https://en.wikipedia.org/wiki/Ethereum",
    "ethereum": "https://en.wikipedia.org/wiki/Ethereum",
    "eth": "https://en.wikipedia.org/wiki/Ethereum",
}

# Currency pair patterns → XE or Wikipedia
def _match_forex(query: str) -> Optional[str]:
    """Match currency pairs like USD/CNY, 美元人民币."""
    aliases = {
        "美元": "USD", "人民币": "CNY", "欧元": "EUR", "日元": "JPY",
        "英镑": "GBP", "港币": "HKD", "澳元": "AUD",
        "usd": "USD", "cny": "CNY", "eur": "EUR", "jpy": "JPY",
        "gbp": "GBP", "hkd": "HKD", "aud": "AUD",
    }
    # Try to find two currencies in the query
    found = []
    for alias, code in aliases.items():
        if alias in query.lower():
            found.append(code)
    if len(found) >= 2:
        pair = f"{found[0]}{found[1]}"
        return f"https://www.xe.com/currencyconverter/convert/?From={found[0]}&To={found[1]}"
    return None


def find_urls(query: str) -> list[str]:
    """Find known reliable URLs for a research query.

    Returns list of URLs to try with web_fetch, empty if no match.
    """
    urls = []
    q = query.lower()

    # 1. Stock price queries — match company name near price/stock keywords
    company_names = (
        r'苹果|apple|微软|microsoft|谷歌|google|亚马逊|amazon'
        r'|特斯拉|tesla|英伟达|nvidia|meta|facebook|百度|baidu'
        r'|阿里巴巴|alibaba|阿里|腾讯|tencent|台积电|tsmc|amd|intel'
        r'|netflix|奈飞|uber|airbnb'
    )
    stock_kw = r'股票|股价|stock|price'
    
    # Pattern: company near stock keywords (either order)
    stock_match = re.search(
        rf'(?:{stock_kw}).*?({company_names})|({company_names}).*?(?:{stock_kw})',
        q, re.IGNORECASE
    )
    if stock_match:
        company = stock_match.group(1) or stock_match.group(2)
    else:
        # Try direct ticker
        stock_match = re.search(
            r'\b(AAPL|MSFT|GOOGL|AMZN|TSLA|NVDA|META|BIDU|BABA|TCEHY|TSM|AMD|INTC|NFLX|UBER|ABNB)\b',
            q, re.IGNORECASE
        )
        company = stock_match.group(1) if stock_match else None

    if company:
        symbol = company.upper()
        # Map alias to ticker
        for alias, ticker in _STOCK_ALIASES.items():
            if symbol.lower() == alias.lower() and ticker:
                symbol = ticker
                break
        urls.append(f"https://finance.yahoo.com/quote/{symbol}/history")
        urls.append(f"https://en.wikipedia.org/wiki/{symbol}")

    # 2. Commodity queries
    for keyword, url in _COMMODITY_URLS.items():
        if keyword in q:
            urls.append(url)
            break

    # 3. Forex / exchange rate
    forex_url = _match_forex(q)
    if forex_url:
        urls.append(forex_url)

    # 4. General knowledge → Wikipedia
    has_url = bool(urls)
    if not has_url:
        # Extract likely topic for Wikipedia
        topic_match = re.search(r'(?:什么是|什么是|关于|about|介绍)\s*(.{2,15})', q)
        if topic_match:
            topic = topic_match.group(1).strip()
            urls.append(f"https://en.wikipedia.org/wiki/{topic.replace(' ', '_')}")

    return urls
