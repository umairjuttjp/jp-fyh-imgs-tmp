#!/usr/bin/env python3
"""Exact-source audit engine for the JapansPrime catalog.

The engine is deliberately conservative:
- Manufacturer/brand-controlled Japanese product pages are preferred.
- Amazon.co.jp is accepted only when the search evidence contains an exact model/JAN
  or an unusually strong title + size/variant match.
- Retailer, marketplace, review, category, search, article, and social pages are never
  promoted to an official product source.
- Weak or family-only evidence is retained as HOLD/BLOCK, never force-matched.

Usage:
  python source_audit_engine.py --input chunk.json --output result.json
"""
from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/136.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/135.0 Safari/537.36",
]

MARKETING_WORDS = {
    "authentic", "japanese", "japan", "premium", "professional", "traditional",
    "handcrafted", "made", "quality", "original", "official", "advanced", "ultimate",
    "iconic", "elegant", "vibrant", "soothing", "natural", "classic", "luxury",
    "deluxe", "healthy", "rich", "genuine", "artisan", "perfect", "superior",
    "deep", "care", "flavor", "flavour", "set", "pack", "piece", "pieces",
}

RESELLER_DOMAINS = {
    "amazon.co.jp", "amazon.com", "rakuten.co.jp", "rakuten.com", "item.rakuten.co.jp",
    "shopping.yahoo.co.jp", "store.shopping.yahoo.co.jp", "paypaymall.yahoo.co.jp",
    "yodobashi.com", "biccamera.com", "bic-camera.com", "joshinweb.jp", "edion.com",
    "ksdenki.com", "yamada-denkiweb.com", "kojima.net", "monotaro.com", "askul.co.jp",
    "lohaco.yahoo.co.jp", "qoo10.jp", "wowma.jp", "aupaymarket.com", "mercari.com",
    "jp.mercari.com", "fril.jp", "zozo.jp", "zozotown.com", "hands.net",
    "shop-list.com", "cosme.com", "cosme.net", "lipscosme.com", "lips-shopping.com",
    "matsukiyococokara-online.com", "sundrug-online.com", "tomods.jp", "welcia-yakkyoku.co.jp",
    "japanesetaste.com", "int.japanesetaste.com", "yesstyle.com", "stylevana.com",
    "dokodemo.world", "japanwithlovestore.com", "japanhaul.com", "japaniful.com",
    "kakaku.com", "my-best.com", "360life.shinyusha.co.jp", "roomie.jp", "macaro-ni.jp",
    "mognavi.jp", "mogumogu.tokyo", "ameblo.jp", "note.com", "prtimes.jp",
    "youtube.com", "youtu.be", "instagram.com", "facebook.com", "x.com", "twitter.com",
    "pinterest.com", "tiktok.com", "wikipedia.org", "amazonaws.com", "shopify.com",
}

REVIEW_OR_LISTING_PATHS = {
    "review", "reviews", "kuchikomi", "community", "ranking", "category", "categories",
    "search", "products-list", "product-list", "itemlist", "items", "brandlist", "brands",
    "news", "article", "articles", "blog", "column", "feature", "features", "press",
    "campaign", "special", "collection", "collections", "tag", "tags",
}

OFFICIAL_HINTS = (
    "公式", "official", "メーカー", "製品情報", "商品情報", "ブランドサイト",
    "オンラインストア", "online store", "corporate", "株式会社",
)

JP_TLDS = (".jp", ".co.jp", ".or.jp", ".ne.jp", ".com")

session = requests.Session()
session.headers.update({
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.6,en;q=0.4",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})


def norm(text: object) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    value = value.replace("ℓ", "l")
    return re.sub(r"[^0-9a-zぁ-んァ-ヶ一-龯]+", "", value)


def domain_of(url: str) -> str:
    try:
        d = urlparse(url).netloc.lower().split("@")[ -1 ].split(":")[0]
    except Exception:
        return ""
    return d[4:] if d.startswith("www.") else d


def root_domain(domain: str) -> str:
    parts = domain.split(".")
    if len(parts) >= 3 and parts[-2:] in (["co", "jp"], ["or", "jp"], ["ne", "jp"]):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def is_reseller(domain: str) -> bool:
    d = root_domain(domain)
    return any(d == r or domain == r or domain.endswith("." + r) for r in RESELLER_DOMAINS)


def is_amazon(url: str) -> bool:
    d = domain_of(url)
    return d == "amazon.co.jp" or d.endswith(".amazon.co.jp")


def bad_path(url: str) -> bool:
    p = urlparse(url)
    path_parts = [x.lower() for x in p.path.split("/") if x]
    if any(x in REVIEW_OR_LISTING_PATHS for x in path_parts):
        return True
    low = (p.path + "?" + p.query).lower()
    return any(x in low for x in ("/search", "?q=", "?s=", "ranking", "review", "kuchikomi"))


def clean_url(url: str) -> str:
    url = html.unescape(url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    # DuckDuckGo redirect wrapper.
    if "duckduckgo.com/l/?" in url:
        qs = parse_qs(urlparse(url).query)
        if qs.get("uddg"):
            url = unquote(qs["uddg"][0])
    # Bing occasionally wraps destination in a query parameter.
    parsed = urlparse(url)
    if domain_of(url).endswith("bing.com"):
        qs = parse_qs(parsed.query)
        for key in ("url", "u", "r"):
            if qs.get(key) and qs[key][0].startswith("http"):
                url = unquote(qs[key][0])
                break
    return url.split("#")[0]


def get(url: str, timeout: int = 18, retries: int = 2) -> requests.Response | None:
    for attempt in range(retries + 1):
        try:
            session.headers["User-Agent"] = random.choice(USER_AGENTS)
            r = session.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code == 200 and r.content:
                return r
            if r.status_code in (403, 429, 503):
                time.sleep(1.4 + attempt)
        except requests.RequestException:
            if attempt < retries:
                time.sleep(0.7 + attempt)
    return None


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str
    engine: str


def search_bing(query: str, limit: int = 8) -> list[SearchResult]:
    url = "https://www.bing.com/search?q=" + quote_plus(query) + "&setlang=ja-JP&cc=jp&count=10"
    r = get(url, timeout=20, retries=1)
    if not r:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    out: list[SearchResult] = []
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a")
        if not a:
            continue
        href = clean_url(a.get("href", ""))
        if not href.startswith("http"):
            continue
        sn = li.select_one(".b_caption p") or li.select_one("p")
        out.append(SearchResult(href, a.get_text(" ", strip=True), sn.get_text(" ", strip=True) if sn else "", "bing"))
        if len(out) >= limit:
            break
    return out


def search_ddg(query: str, limit: int = 8) -> list[SearchResult]:
    url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query) + "&kl=jp-jp"
    r = get(url, timeout=20, retries=1)
    if not r:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    out: list[SearchResult] = []
    for item in soup.select(".result"):
        a = item.select_one("a.result__a")
        if not a:
            continue
        href = clean_url(a.get("href", ""))
        if not href.startswith("http"):
            continue
        sn = item.select_one(".result__snippet")
        out.append(SearchResult(href, a.get_text(" ", strip=True), sn.get_text(" ", strip=True) if sn else "", "ddg"))
        if len(out) >= limit:
            break
    return out


def search_web(query: str, limit: int = 10) -> list[SearchResult]:
    seen = set()
    merged: list[SearchResult] = []
    for fn in (search_bing, search_ddg):
        for r in fn(query, limit=limit):
            u = r.url.split("?")[0].rstrip("/")
            if u in seen:
                continue
            seen.add(u)
            merged.append(r)
            if len(merged) >= limit:
                return merged
        if merged:
            # One functioning engine is enough for a query; reduces requests and blocks.
            break
    return merged


def text_tokens(text: str) -> set[str]:
    raw = unicodedata.normalize("NFKC", text or "").lower()
    words = re.findall(r"[a-z][a-z0-9]{2,}|[ァ-ヶー]{2,}|[一-龯]{2,}", raw)
    return {w for w in words if w not in MARKETING_WORDS and len(w) >= 2}


def extract_measure_tokens(text: str) -> set[str]:
    t = unicodedata.normalize("NFKC", text or "").lower().replace(" ", "")
    vals = set()
    patterns = [
        r"\d+(?:\.\d+)?(?:ml|mL|l|g|kg|cm|mm)",
        r"\d+(?:\.\d+)?(?:ミリリットル|リットル|グラム|キログラム|センチ|ミリ)",
        r"\d+(?:色|本|個|枚|袋|包|粒|錠|カプセル|回分|点|種)",
        r"\d+(?:\.\d+)?(?:oz|lb)",
    ]
    for p in patterns:
        for m in re.findall(p, t, flags=re.I):
            vals.add(norm(m))
    return vals


def extract_jans(text: str) -> set[str]:
    vals = set()
    for m in re.findall(r"(?<!\d)(\d{8}|\d{12}|\d{13}|\d{14})(?!\d)", str(text or "")):
        vals.add(m)
    return vals


def extract_model_tokens(product: dict, clue_text: str = "") -> set[str]:
    values = [product.get("title", ""), product.get("handle", ""), clue_text]
    for v in product.get("variants", []):
        values.extend([v.get("sku", ""), v.get("title", ""), v.get("barcode", "")])
    candidates: set[str] = set()
    for value in values:
        s = unicodedata.normalize("NFKC", str(value or ""))
        # Explicit hyphenated or compact alphanumeric model strings.
        for token in re.findall(r"(?<![A-Za-z0-9])([A-Za-z]{1,8}[-_/]?[A-Za-z0-9]{2,18}(?:[-_/][A-Za-z0-9]{1,12})*)(?![A-Za-z0-9])", s):
            n = norm(token)
            if 4 <= len(n) <= 26 and any(c.isdigit() for c in n):
                if not re.fullmatch(r"\d+(?:ml|g|kg|cm|mm|l)", n):
                    candidates.add(n)
        # Pure numeric product codes are useful only when at least 4 digits and not a common year/measure.
        for token in re.findall(r"(?<!\d)(\d{4,9})(?!\d)", s):
            if token not in {"2023", "2024", "2025", "2026"}:
                candidates.add(token)
    # Prefer the most specific tokens; keep at most 10.
    return set(sorted(candidates, key=lambda x: (-len(x), x))[:10])


def extract_jsonld(soup: BeautifulSoup) -> list[dict]:
    out = []
    for tag in soup.select('script[type="application/ld+json"]'):
        raw = tag.string or tag.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        for obj in stack:
            if isinstance(obj, dict) and isinstance(obj.get("@graph"), list):
                stack.extend(obj["@graph"])
            if isinstance(obj, dict):
                out.append(obj)
    return out


def fetch_page_identity(url: str, max_chars: int = 180000) -> dict:
    r = get(url, timeout=22, retries=1)
    if not r:
        return {"ok": False, "url": url}
    final = clean_url(r.url)
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype and not r.text.lstrip().startswith("<"):
        return {"ok": False, "url": final}
    soup = BeautifulSoup(r.text[:max_chars], "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    desc = ""
    md = soup.select_one('meta[name="description"], meta[property="og:description"]')
    if md:
        desc = md.get("content", "")
    h1 = " | ".join(x.get_text(" ", strip=True) for x in soup.select("h1")[:3])
    body = soup.get_text(" ", strip=True)
    body = re.sub(r"\s+", " ", body)[:120000]
    ld = extract_jsonld(soup)
    ld_text = json.dumps(ld, ensure_ascii=False, separators=(",", ":"))[:50000]
    combined = " ".join([title, h1, desc, body, ld_text])
    return {
        "ok": True,
        "url": final,
        "domain": domain_of(final),
        "title": title,
        "h1": h1,
        "description": desc,
        "text": combined,
        "jans": sorted(extract_jans(combined)),
        "jsonld": ld[:12],
    }


def clue_from_existing_source(product: dict) -> dict:
    sources = product.get("sources") or {}
    url = sources.get("sourceUrl") or sources.get("source_url") or ""
    if not isinstance(url, str) or not url.startswith("http"):
        return {"url": "", "text": "", "jans": [], "models": []}
    page = fetch_page_identity(url)
    if not page.get("ok"):
        return {"url": url, "text": "", "jans": [], "models": []}
    text = " ".join([page.get("title", ""), page.get("h1", ""), page.get("description", ""), page.get("text", "")[:25000]])
    models = sorted(extract_model_tokens(product, text), key=lambda x: (-len(x), x))[:12]
    return {"url": url, "text": text[:30000], "jans": page.get("jans", [])[:20], "models": models}


def title_query(product: dict, clue: dict) -> str:
    vendor = str(product.get("vendor") or "").strip()
    title = str(product.get("title") or "").strip()
    # Strip generic English marketing suffixes after dash/colon while keeping key identity.
    title = re.sub(r"\s+[–—-]\s+(authentic|premium|professional|made|natural|japanese|soothing|deep|rich|traditional).*$", "", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip()
    models = list(clue.get("models") or []) or sorted(extract_model_tokens(product), key=lambda x: (-len(x), x))
    if models:
        return f'"{vendor}" "{models[0]}" 公式 商品'
    # Preserve the first 9 meaningful words, including capacity.
    words = title.split()
    concise = " ".join(words[:12])
    return f'"{vendor}" {concise} 公式 商品'


def amazon_query(product: dict, clue: dict) -> str:
    vendor = str(product.get("vendor") or "").strip()
    models = list(clue.get("models") or []) or sorted(extract_model_tokens(product), key=lambda x: (-len(x), x))
    if models:
        ident = models[0]
    else:
        ident = " ".join(str(product.get("title") or "").split()[:9])
    return f'site:amazon.co.jp "{vendor}" "{ident}"'


def domain_similarity(vendor: str, domain: str, page_text: str) -> float:
    v = norm(vendor)
    d = norm(domain.split(".")[0])
    if not v:
        return 0.0
    if v in norm(domain) or d in v or (len(d) >= 4 and d in v):
        return 1.0
    vt = text_tokens(vendor)
    pt = text_tokens(page_text[:3000])
    if vt and vt & pt:
        return 0.75
    return 0.0


def overlap_ratio(product_text: str, page_text: str) -> float:
    a = text_tokens(product_text)
    b = text_tokens(page_text)
    if not a:
        return 0.0
    # Product title contains many marketing words already filtered. Require meaningful coverage.
    return len(a & b) / max(1, min(len(a), 14))


def page_is_official(vendor: str, page: dict, result: SearchResult) -> tuple[bool, list[str]]:
    url = page.get("url") or result.url
    domain = domain_of(url)
    reasons = []
    if not domain or is_reseller(domain) or is_amazon(url) or bad_path(url):
        return False, reasons
    combined = " ".join([result.title, result.snippet, page.get("title", ""), page.get("h1", ""), page.get("description", "")])
    sim = domain_similarity(vendor, domain, combined)
    if sim >= 0.75:
        reasons.append("brand/domain identity")
    hint = any(h.lower() in combined.lower() for h in OFFICIAL_HINTS)
    if hint:
        reasons.append("official/manufacturer indicator")
    # Japanese manufacturer domains often have non-obvious corporate names; strong query result + product page structure is allowed.
    product_path = any(x in urlparse(url).path.lower() for x in ("product", "products", "item", "items", "goods", "detail", "brand"))
    if product_path:
        reasons.append("product-detail path")
    official = sim >= 0.75 or (hint and product_path)
    return official, reasons


def evaluate_candidate(product: dict, clue: dict, result: SearchResult, amazon: bool = False) -> dict:
    page = fetch_page_identity(result.url) if not amazon else {
        "ok": True,
        "url": result.url,
        "domain": domain_of(result.url),
        "title": result.title,
        "h1": "",
        "description": result.snippet,
        "text": result.title + " " + result.snippet,
        "jans": sorted(extract_jans(result.title + " " + result.snippet)),
    }
    if not page.get("ok"):
        return {"url": result.url, "score": -999, "status": "UNREACHABLE", "evidence": ["page fetch failed"]}

    combined = " ".join([page.get("title", ""), page.get("h1", ""), page.get("description", ""), page.get("text", "")])
    ncombined = norm(combined)
    product_title = str(product.get("title") or "")
    vendor = str(product.get("vendor") or "")

    known_jans = set(clue.get("jans") or [])
    for v in product.get("variants", []):
        known_jans.update(extract_jans(str(v.get("barcode") or "")))
    model_tokens = set(clue.get("models") or []) | extract_model_tokens(product, clue.get("text", ""))
    measure_tokens = extract_measure_tokens(product_title + " " + clue.get("text", "")[:5000])

    exact_jans = sorted(j for j in known_jans if j and j in combined)
    exact_models = sorted(m for m in model_tokens if len(m) >= 4 and m in ncombined, key=lambda x: (-len(x), x))
    exact_measures = sorted(m for m in measure_tokens if m and m in ncombined)
    title_overlap = overlap_ratio(product_title, combined)
    vendor_match = domain_similarity(vendor, page.get("domain", ""), combined)

    score = 0
    evidence = []
    if exact_jans:
        score += 70
        evidence.append("exact JAN: " + ", ".join(exact_jans[:3]))
    if exact_models:
        score += 52
        evidence.append("exact model: " + ", ".join(exact_models[:4]))
    if exact_measures:
        score += min(24, 12 + 4 * len(exact_measures))
        evidence.append("matching size/count: " + ", ".join(exact_measures[:5]))
    if title_overlap >= 0.75:
        score += 35
        evidence.append(f"title token overlap {title_overlap:.2f}")
    elif title_overlap >= 0.55:
        score += 24
        evidence.append(f"title token overlap {title_overlap:.2f}")
    elif title_overlap >= 0.38:
        score += 12
        evidence.append(f"title token overlap {title_overlap:.2f}")
    if vendor_match >= 0.75:
        score += 20
        evidence.append("brand identity match")

    official = False
    official_reasons: list[str] = []
    if amazon:
        official = False
    else:
        official, official_reasons = page_is_official(vendor, page, result)
        if official:
            score += 25
            evidence.extend(official_reasons)
        else:
            score -= 35
            evidence.append("official ownership not established")

    # Exactness gates. A generic family page never passes merely on brand similarity.
    exact_identifier = bool(exact_jans or exact_models)
    size_gate = bool(exact_measures) or not measure_tokens
    strong_title_gate = title_overlap >= 0.72 and (bool(exact_measures) or not measure_tokens)

    if amazon:
        if exact_identifier and size_gate and title_overlap >= 0.38:
            status = "VERIFIED_AMAZON_JP_EXACT"
        elif strong_title_gate and vendor_match >= 0.75:
            status = "AMAZON_JP_STRONG_CANDIDATE"
        else:
            status = "AMAZON_JP_INSUFFICIENT"
    else:
        if official and exact_identifier and size_gate and title_overlap >= 0.30:
            status = "VERIFIED_OFFICIAL_EXACT"
        elif official and strong_title_gate and vendor_match >= 0.75:
            status = "VERIFIED_OFFICIAL_TITLE_SIZE"
        elif official and (exact_identifier or title_overlap >= 0.45):
            status = "OFFICIAL_CANDIDATE_NEEDS_EXACT_CONFIRMATION"
        else:
            status = "REJECTED_OR_WEAK"

    return {
        "url": page.get("url") or result.url,
        "domain": page.get("domain") or domain_of(result.url),
        "search_title": result.title,
        "page_title": page.get("title", ""),
        "engine": result.engine,
        "score": score,
        "status": status,
        "evidence": evidence,
        "exact_jans": exact_jans,
        "exact_models": exact_models,
        "exact_measures": exact_measures,
        "title_overlap": round(title_overlap, 3),
        "official": official,
    }


def audit_product(product: dict) -> dict:
    started = time.time()
    clue = clue_from_existing_source(product)
    queries = []
    q1 = title_query(product, clue)
    queries.append(q1)

    candidates: list[dict] = []
    seen = set()
    for result in search_web(q1, limit=10):
        url = clean_url(result.url)
        domain = domain_of(url)
        if not url.startswith("http") or not domain or is_reseller(domain) or is_amazon(url) or bad_path(url):
            continue
        key = url.split("?")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        ev = evaluate_candidate(product, clue, result, amazon=False)
        candidates.append(ev)
        # Stop early only for a high-confidence exact official page.
        if ev["status"] == "VERIFIED_OFFICIAL_EXACT" and ev["score"] >= 105:
            break

    # A second title-focused query is used only when model search produced no viable official candidate.
    if not any(c["status"].startswith("VERIFIED_OFFICIAL") for c in candidates):
        vendor = str(product.get("vendor") or "").strip()
        concise = " ".join(str(product.get("title") or "").split()[:11])
        q2 = f'"{vendor}" {concise} 公式'
        if q2 != q1:
            queries.append(q2)
            for result in search_web(q2, limit=8):
                url = clean_url(result.url)
                domain = domain_of(url)
                if not url.startswith("http") or not domain or is_reseller(domain) or is_amazon(url) or bad_path(url):
                    continue
                key = url.split("?")[0].rstrip("/")
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(evaluate_candidate(product, clue, result, amazon=False))

    candidates.sort(key=lambda x: x.get("score", -999), reverse=True)
    verified = [c for c in candidates if c.get("status") in ("VERIFIED_OFFICIAL_EXACT", "VERIFIED_OFFICIAL_TITLE_SIZE")]
    best = verified[0] if verified else (candidates[0] if candidates else None)

    amazon_candidates = []
    if not verified:
        aq = amazon_query(product, clue)
        queries.append(aq)
        for result in search_web(aq, limit=6):
            if not is_amazon(result.url):
                continue
            ev = evaluate_candidate(product, clue, result, amazon=True)
            amazon_candidates.append(ev)
        amazon_candidates.sort(key=lambda x: x.get("score", -999), reverse=True)
        amazon_verified = [c for c in amazon_candidates if c.get("status") == "VERIFIED_AMAZON_JP_EXACT"]
        if amazon_verified:
            best = amazon_verified[0]
            verified = amazon_verified

    if best and best.get("status") in ("VERIFIED_OFFICIAL_EXACT", "VERIFIED_OFFICIAL_TITLE_SIZE", "VERIFIED_AMAZON_JP_EXACT"):
        final_status = best["status"]
        final_url = best["url"]
        source_type = "OFFICIAL_BRAND_JP" if "OFFICIAL" in final_status else "AMAZON_JP"
        confidence = "HIGH" if best.get("score", 0) >= 105 else "MEDIUM"
    elif best and best.get("status") == "OFFICIAL_CANDIDATE_NEEDS_EXACT_CONFIRMATION":
        final_status = "HOLD_OFFICIAL_CANDIDATE"
        final_url = ""
        source_type = ""
        confidence = "LOW"
    else:
        final_status = "UNRESOLVED_NO_EXACT_SOURCE"
        final_url = ""
        source_type = ""
        confidence = "NONE"

    result = {
        "shopify_product_id": product.get("legacyResourceId"),
        "handle": product.get("handle"),
        "store_title": product.get("title"),
        "vendor": product.get("vendor"),
        "status": product.get("status"),
        "store_url": product.get("onlineStoreUrl"),
        "variant_skus": [v.get("sku") for v in product.get("variants", []) if v.get("sku")],
        "variant_barcodes": [v.get("barcode") for v in product.get("variants", []) if v.get("barcode")],
        "existing_reseller_clue": clue.get("url") or "",
        "clue_jans": clue.get("jans") or [],
        "clue_models": clue.get("models") or [],
        "final_source_url": final_url,
        "source_type": source_type,
        "final_status": final_status,
        "confidence": confidence,
        "verification_evidence": best.get("evidence", []) if best else [],
        "matched_model_codes": best.get("exact_models", []) if best else [],
        "matched_jans": best.get("exact_jans", []) if best else [],
        "matched_size_count": best.get("exact_measures", []) if best else [],
        "best_candidate_url": best.get("url", "") if best else "",
        "best_candidate_status": best.get("status", "") if best else "",
        "best_candidate_score": best.get("score") if best else None,
        "queries": queries,
        "official_candidates_reviewed": candidates[:5],
        "amazon_candidates_reviewed": amazon_candidates[:3],
        "elapsed_seconds": round(time.time() - started, 2),
        "engine_version": "2026-08-02.1",
    }
    return result


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--delay", type=float, default=0.35)
    args = p.parse_args()

    products = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if args.start:
        products = products[args.start:]
    if args.limit:
        products = products[:args.limit]

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, product in enumerate(products, 1):
        try:
            row = audit_product(product)
        except Exception as exc:
            row = {
                "shopify_product_id": product.get("legacyResourceId"),
                "handle": product.get("handle"),
                "store_title": product.get("title"),
                "vendor": product.get("vendor"),
                "final_source_url": "",
                "source_type": "",
                "final_status": "ENGINE_ERROR",
                "confidence": "NONE",
                "verification_evidence": [f"{type(exc).__name__}: {exc}"],
                "engine_version": "2026-08-02.1",
            }
        rows.append(row)
        print(json.dumps({
            "n": i,
            "id": row.get("shopify_product_id"),
            "status": row.get("final_status"),
            "url": row.get("final_source_url"),
            "elapsed": row.get("elapsed_seconds"),
        }, ensure_ascii=False), flush=True)
        if args.delay:
            time.sleep(args.delay)
        out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
