#!/usr/bin/env python3
"""JapansPrime Japanese-market original-source audit engine, v3.

Conservative decision rules:
1. Prefer a brand/manufacturer-controlled Japanese product-detail page.
2. Accept Amazon.co.jp only when the search evidence identifies an exact model/JAN,
   or explicitly marks the result as the brand's official listing and title/size agree.
3. Treat every JapansPrime SKU as internal unless a plausible public model suffix is
   independently visible in source evidence.
4. Never promote a retailer, review, category, search, article, or family page to an
   exact source.
5. Produce a decision for every product. Weak evidence becomes HOLD/BLOCK, never a
   forced source URL.
"""
from __future__ import annotations

import argparse
import collections
import difflib
import html
import json
import random
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests
from bs4 import BeautifulSoup

VERSION = "2026-08-02.3"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/136.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/135.0 Safari/537.36",
]

RESELLER_ROOTS = {
    "amazon.co.jp", "amazon.com", "rakuten.co.jp", "yahoo.co.jp", "yodobashi.com",
    "biccamera.com", "joshinweb.jp", "edion.com", "ksdenki.com", "yamada-denkiweb.com",
    "kojima.net", "monotaro.com", "askul.co.jp", "qoo10.jp", "aupaymarket.com",
    "mercari.com", "fril.jp", "zozo.jp", "hands.net", "cosme.net", "cosme.com",
    "lipscosme.com", "matsukiyococokara-online.com", "sundrug-online.com",
    "japanesetaste.com", "yesstyle.com", "stylevana.com", "dokodemo.world",
    "japanwithlovestore.com", "japanhaul.com", "weee.com", "kakaku.com", "my-best.com",
    "mognavi.jp", "ameblo.jp", "note.com", "prtimes.jp", "wikipedia.org",
    "youtube.com", "instagram.com", "facebook.com", "twitter.com", "x.com",
    "pinterest.com", "tiktok.com", "shop.app", "shopify.com", "lohaco.yahoo.co.jp",
    "wowma.jp", "jp.mercari.com", "paypaymall.yahoo.co.jp", "item.rakuten.co.jp",
    "search.rakuten.co.jp", "store.shopping.yahoo.co.jp", "shopping.yahoo.co.jp",
}

BAD_PATH_WORDS = {
    "review", "reviews", "community", "kuchikomi", "ranking", "category", "categories",
    "search", "brandlist", "brands", "news", "article", "articles", "blog", "column",
    "feature", "features", "press", "campaign", "special", "collection", "collections",
    "tag", "tags", "support", "faq", "history", "about",
}

GENERIC_WORDS = {
    "authentic", "japanese", "japan", "premium", "professional", "traditional", "quality",
    "handcrafted", "natural", "classic", "elegant", "vibrant", "soothing", "healthy",
    "luxury", "rich", "deep", "care", "flavor", "flavour", "made", "original", "official",
    "set", "pack", "piece", "pieces", "count", "box", "with", "and", "for", "the",
    "advanced", "ultimate", "iconic", "perfect", "superior", "genuine", "artisan",
}

# Seed domains are verified brand/manufacturer-controlled domains. Dynamic discovery supplements them.
SEED_DOMAINS = {
    "kewpie": ["kewpie.co.jp"], "shu uemura": ["shuuemura.jp"],
    "dr.ci:labo": ["ci-labo.com"], "dr.ci labo": ["ci-labo.com"],
    "copic": ["copic.jp"], "kao": ["kao.com"], "ipsa": ["ipsa.co.jp"],
    "kanebo": ["kanebo-cosmetics.jp"], "kate": ["nomorerules.net"],
    "kokuyo": ["kokuyo.com"], "kuretake": ["kuretake.co.jp", "order.kuretake.co.jp"],
    "tojiro": ["tojiro-japan.com"], "oigen": ["oigen.jp"], "iwachu": ["iwachu.co.jp"],
    "pentel": ["pentel.co.jp"], "tombow": ["tombow.com"],
    "maruman": ["e-maruman.co.jp"], "midori": ["midori-japan.co.jp", "midori-store.net"],
    "zebra": ["zebra.co.jp"], "shiseido": ["shiseido.co.jp"], "fancl": ["fancl.co.jp"],
    "dhc": ["dhc.co.jp"], "kose": ["kose.co.jp", "maison.kose.co.jp"],
    "decorte": ["decorte.com"], "cosme decorte": ["decorte.com"],
    "cosme decorté": ["decorte.com"], "morinaga": ["morinaga.co.jp"],
    "meiji": ["meiji.co.jp"], "calbee": ["calbee.co.jp"],
    "ajinomoto": ["ajinomoto.co.jp"], "house foods": ["housefoods.jp"],
    "nissin": ["nissin.com"], "pilot": ["pilot.co.jp"], "uni": ["mpuni.co.jp"],
    "mitsubishi pencil": ["mpuni.co.jp"], "muji": ["muji.com"],
    "ryohin keikaku": ["muji.com"], "hario": ["hario.com"],
    "sori yanagi": ["yanagi-support.jp"], "aderia": ["aderia.jp"],
    "vita craft": ["vitacraft.co.jp"], "orihiro": ["orihiro.com"],
    "milbon": ["milbon.co.jp"], "canmake": ["canmake.com"],
    "canmake tokyo": ["canmake.com"], "cezanne": ["cezanne.co.jp"],
    "unicharm": ["unicharm.co.jp"], "lion": ["lion.co.jp"],
    "earth chemical": ["earth.jp"], "earth pharmaceutical": ["earth.jp"],
    "kobayashi pharmaceutical": ["kobayashi.co.jp"], "rohto": ["rohto.co.jp"],
    "hada labo": ["jp.rohto.com"], "pola": ["pola.co.jp"], "albion": ["albion.co.jp"],
    "etvos": ["etvos.com"], "chacott": ["chacott-jp.com"], "covermark": ["covermark.co.jp"],
    "ezaki glico": ["glico.com"], "glico": ["glico.com"], "bourbon": ["bourbon.co.jp"],
    "imuraya": ["imuraya.co.jp"], "uha mikakuto": ["uha-mikakuto.co.jp"],
    "fundokin": ["fundokin.co.jp"], "bull-dog": ["bulldog.co.jp"],
    "hikari miso": ["hikarimiso.co.jp"], "ippodo tea co.": ["ippodo-tea.co.jp"],
    "marukyu koyamaen": ["marukyu-koyamaen.co.jp"], "yawataya isogoro": ["yawataya.co.jp"],
    "green bell": ["greenbell.ne.jp"], "feather": ["feather.co.jp"],
    "feather japan": ["feather.co.jp"], "sumikama": ["kasumi-knives.com"],
    "leye": ["aux-ltd.co.jp"], "wahei freiz": ["wahei.co.jp"],
    "ikehiko": ["ikehiko.net"], "akashiya": ["akashiya-fude.co.jp"],
}

http = requests.Session()
http.headers.update({
    "Accept-Language": "ja-JP,ja;q=0.95,en;q=0.5",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.7",
})

SEARCH_CACHE: dict[str, list] = {}
PAGE_CACHE: dict[str, dict] = {}
VENDOR_DOMAIN_CACHE: dict[str, list[str]] = {}
_LAST_YAHOO = 0.0


def nfkc(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or ""))


def norm(value: object) -> str:
    s = nfkc(value).lower().replace("ℓ", "l")
    return re.sub(r"[^0-9a-zぁ-んァ-ヶ一-龯]+", "", s)


def domain_of(url: str) -> str:
    try:
        d = urlparse(url).netloc.lower().split("@")[ -1 ].split(":")[0]
    except Exception:
        return ""
    return d[4:] if d.startswith("www.") else d


def root_domain(domain: str) -> str:
    parts = [p for p in domain.split(".") if p]
    if len(parts) >= 3 and parts[-2:] in (["co", "jp"], ["or", "jp"], ["ne", "jp"], ["ac", "jp"]):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def is_reseller(url_or_domain: str) -> bool:
    d = domain_of(url_or_domain) if "://" in url_or_domain else url_or_domain
    rd = root_domain(d)
    return any(rd == x or d == x or d.endswith("." + x) for x in RESELLER_ROOTS)


def is_amazon(url: str) -> bool:
    d = domain_of(url)
    return d == "amazon.co.jp" or d.endswith(".amazon.co.jp")


def bad_path(url: str) -> bool:
    p = urlparse(url)
    pieces = [x.lower() for x in p.path.split("/") if x]
    if any(x in BAD_PATH_WORDS for x in pieces):
        return True
    low = (p.path + "?" + p.query).lower()
    return any(x in low for x in ("/search", "?q=", "?s=", "ranking", "review", "kuchikomi"))


def clean_url(url: str) -> str:
    u = html.unescape(url or "").strip()
    if u.startswith("//"):
        u = "https:" + u
    if "duckduckgo.com/l/?" in u:
        qs = parse_qs(urlparse(u).query)
        if qs.get("uddg"):
            u = unquote(qs["uddg"][0])
    return u.split("#")[0]


def get(url: str, timeout: int = 20, retries: int = 1):
    for n in range(retries + 1):
        try:
            http.headers["User-Agent"] = random.choice(USER_AGENTS)
            r = http.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code == 200 and r.content:
                return r
            if r.status_code in (403, 429, 503):
                time.sleep(1.0 + n)
        except requests.RequestException:
            if n < retries:
                time.sleep(0.7 + n)
    return None


@dataclass
class SResult:
    url: str
    title: str
    snippet: str
    engine: str


def search_yahoo(query: str, limit: int = 10) -> list[SResult]:
    global _LAST_YAHOO
    key = "yahoo:" + query
    if key in SEARCH_CACHE:
        return SEARCH_CACHE[key][:limit]
    wait = 0.45 - (time.time() - _LAST_YAHOO)
    if wait > 0:
        time.sleep(wait)
    r = get("https://search.yahoo.co.jp/search?p=" + quote_plus(query), 25, 1)
    _LAST_YAHOO = time.time()
    out: list[SResult] = []
    if r:
        soup = BeautifulSoup(r.text, "html.parser")
        seen = set()
        for a in soup.find_all("a", href=True):
            url = clean_url(a.get("href", ""))
            title = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
            if not url.startswith("http") or len(title) < 4:
                continue
            d = domain_of(url)
            if not d or any(x in d for x in (
                "yahoo.co.jp", "yimg.jp", "lycorp.co.jp", "lycbiz.com", "yahoo-net.jp", "line.me"
            )):
                continue
            dedupe = url.split("#")[0]
            if dedupe in seen:
                continue
            seen.add(dedupe)
            parent = re.sub(r"\s+", " ", a.parent.get_text(" ", strip=True))[:900] if a.parent else ""
            out.append(SResult(url, title[:350], parent, "yahoo_jp"))
            if len(out) >= limit:
                break
    SEARCH_CACHE[key] = out
    return out


def search_ddg(query: str, limit: int = 10) -> list[SResult]:
    key = "ddg:" + query
    if key in SEARCH_CACHE:
        return SEARCH_CACHE[key][:limit]
    r = get("https://html.duckduckgo.com/html/?q=" + quote_plus(query) + "&kl=jp-jp", 22, 1)
    out: list[SResult] = []
    if r:
        soup = BeautifulSoup(r.text, "html.parser")
        for box in soup.select(".result"):
            a = box.select_one("a.result__a")
            if not a:
                continue
            url = clean_url(a.get("href", ""))
            if not url.startswith("http"):
                continue
            sn = box.select_one(".result__snippet")
            out.append(SResult(url, a.get_text(" ", strip=True), sn.get_text(" ", strip=True) if sn else "", "ddg"))
            if len(out) >= limit:
                break
    SEARCH_CACHE[key] = out
    return out


def search(query: str, limit: int = 10) -> list[SResult]:
    rows = search_yahoo(query, limit)
    if not rows:
        rows = search_ddg(query, limit)
    seen = set()
    out = []
    for row in rows:
        key = clean_url(row.url).split("?")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out[:limit]


def page(url: str) -> dict:
    key = clean_url(url)
    if key in PAGE_CACHE:
        return PAGE_CACHE[key]
    r = get(url, 24, 1)
    if not r:
        out = {"ok": False, "url": url, "domain": domain_of(url), "title": "", "h1": "", "text": "", "jans": []}
        PAGE_CACHE[key] = out
        return out
    final = clean_url(r.url)
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype.lower() and not r.text.lstrip().startswith("<"):
        out = {"ok": False, "url": final, "domain": domain_of(final), "title": "", "h1": "", "text": "", "jans": []}
        PAGE_CACHE[key] = out
        return out
    soup = BeautifulSoup(r.text[:260000], "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    h1 = " | ".join(x.get_text(" ", strip=True) for x in soup.select("h1")[:3])
    desc = ""
    md = soup.select_one('meta[name="description"],meta[property="og:description"]')
    if md:
        desc = md.get("content", "")
    body = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:130000]
    scripts = " ".join(x.get_text(" ", strip=True) for x in soup.select('script[type="application/ld+json"]'))[:50000]
    text = " ".join([title, h1, desc, body, scripts])
    out = {
        "ok": True, "url": final, "domain": domain_of(final), "title": title, "h1": h1,
        "description": desc, "text": text,
        "jans": sorted(set(re.findall(r"(?<!\d)(\d{8}|\d{12}|\d{13}|\d{14})(?!\d)", text))),
    }
    PAGE_CACHE[key] = out
    return out


def words(value: str) -> list[str]:
    raw = nfkc(value).lower()
    ws = re.findall(r"[a-z][a-z0-9]{2,}|[ァ-ヶー]{2,}|[一-龯]{2,}", raw)
    return [w for w in ws if w not in GENERIC_WORDS]


def measure_tokens(value: str) -> set[str]:
    compact = nfkc(value).lower().replace(" ", "")
    pats = [
        r"\d+(?:\.\d+)?(?:ml|l|g|kg|cm|mm)",
        r"\d+(?:\.\d+)?(?:ミリリットル|リットル|グラム|キログラム|センチ|ミリ)",
        r"\d+(?:色|本|個|枚|袋|包|粒|錠|カプセル|回分|点|種)",
    ]
    out = set()
    for pat in pats:
        out.update(norm(x) for x in re.findall(pat, compact, re.I))
    return out


def jans(value: str) -> set[str]:
    return set(re.findall(r"(?<!\d)(\d{8}|\d{12}|\d{13}|\d{14})(?!\d)", str(value or "")))


def source_slug_text(product: dict) -> str:
    source = (product.get("sources") or {}).get("sourceUrl") or ""
    if not isinstance(source, str):
        return ""
    slug = urlparse(source).path.rstrip("/").split("/")[-1]
    return unquote(slug).replace("-", " ").replace("_", " ")


def cleaned_title(product: dict) -> str:
    title = nfkc(product.get("title", ""))
    title = re.sub(
        r"\s+[–—-]\s+(?:authentic|premium|professional|traditional|soothing|deep|rich|made|natural).*$",
        "", title, flags=re.I,
    )
    ws = [w for w in title.split() if w.lower().strip(".,:;()[]") not in GENERIC_WORDS]
    return " ".join(ws[:14])


def sku_suffix_models(product: dict) -> set[str]:
    title_measures = measure_tokens(product.get("title", ""))
    title_numbers = set(re.findall(r"\d+", " ".join(title_measures)))
    out = set()
    for variant in product.get("variants", []):
        sku = nfkc(variant.get("sku", "")).upper()
        parts = [p for p in re.split(r"[-_:/.]+", sku) if p]
        suffixes = parts[2:] if len(parts) >= 3 else []
        for token in suffixes:
            n = norm(token)
            if len(n) < 4 or len(n) > 24:
                continue
            if n.isdigit():
                if len(n) < 4 or n in title_numbers:
                    continue
            elif not (any(c.isalpha() for c in n) and any(c.isdigit() for c in n)):
                continue
            if re.fullmatch(r"(?:set|pack|as|mi|rd|wh|bk|bl|gr|pk)?\d{1,3}", n):
                continue
            out.add(n)
    return out


def explicit_models(product: dict, clue_text: str = "") -> set[str]:
    out = set(sku_suffix_models(product))
    for value, allow_numeric in ((product.get("title", ""), False), (clue_text, True)):
        raw = nfkc(value)
        for token in re.findall(
            r"(?<![A-Za-z0-9])([A-Za-z]{1,8}[-_/]?[A-Za-z0-9]{2,14}(?:[-_/][A-Za-z0-9]{1,10})*)(?![A-Za-z0-9])",
            raw,
        ):
            n = norm(token)
            if 4 <= len(n) <= 24 and any(c.isalpha() for c in n) and any(c.isdigit() for c in n):
                if not re.fullmatch(r"\d+(?:ml|l|g|kg|cm|mm)", n):
                    out.add(n)
        if allow_numeric:
            for token in re.findall(r"(?<!\d)(\d{4,9})(?!\d)", raw):
                if token not in {"2023", "2024", "2025", "2026"}:
                    out.add(token)
    return set(sorted(out, key=lambda x: (-len(x), x))[:12])


def seq_similarity(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na[:700], nb[:1500]).ratio()


def word_overlap(a: str, b: str) -> float:
    aa, bb = set(words(a)), set(words(b))
    if not aa:
        return 0.0
    return len(aa & bb) / max(1, min(len(aa), 14))


def brand_key(vendor: str) -> str:
    return re.sub(r"\s+", " ", nfkc(vendor).lower()).strip()


def brand_domain_similarity(vendor: str, domain: str) -> float:
    v = norm(vendor)
    label = norm(root_domain(domain).split(".")[0])
    if not v or not label:
        return 0.0
    if v in norm(domain) or label in v or v in label:
        return 1.0
    return difflib.SequenceMatcher(None, v, label).ratio()


def seed_domains(vendor: str) -> list[str]:
    key = brand_key(vendor)
    out = []
    for seed_key, domains in SEED_DOMAINS.items():
        if seed_key == key or norm(seed_key) == norm(key) or norm(seed_key) in norm(key) or norm(key) in norm(seed_key):
            out.extend(root_domain(d) for d in domains)
    return list(dict.fromkeys(out))


def discover_official_domains(vendor: str) -> list[str]:
    key = brand_key(vendor)
    if key in VENDOR_DOMAIN_CACHE:
        return VENDOR_DOMAIN_CACHE[key]
    scored: dict[str, float] = collections.defaultdict(float)
    for d in seed_domains(vendor):
        scored[d] = max(scored[d], 100.0)
    for q in (f"{vendor} 公式", f"{vendor} 公式サイト"):
        for r in search(q, 10):
            d = root_domain(domain_of(r.url))
            if not d or is_reseller(d):
                continue
            combined = (r.title + " " + r.snippet).lower()
            score = 0.0
            if "公式" in combined or "official" in combined:
                score += 70
            sim = brand_domain_similarity(vendor, d)
            if sim >= 0.75:
                score += 58
            elif sim >= 0.55:
                score += 30
            if d.endswith(".jp"):
                score += 8
            if score >= 60:
                scored[d] = max(scored[d], score)
    domains = [d for d, s in sorted(scored.items(), key=lambda x: (-x[1], x[0])) if s >= 60][:5]
    VENDOR_DOMAIN_CACHE[key] = domains
    return domains


def localized_clues(product: dict) -> tuple[list[str], list[SResult]]:
    vendor = str(product.get("vendor") or "").strip()
    identity = cleaned_title(product) or source_slug_text(product)
    q = f"{vendor} {identity}"
    results = search(q, 12)
    clues = []
    for r in results:
        for text in (r.title, r.snippet):
            text = re.sub(r"\s+", " ", text).strip()
            if text and text not in clues:
                clues.append(text[:220])
    return clues[:10], results


def existing_clue(product: dict) -> dict:
    url = (product.get("sources") or {}).get("sourceUrl") or ""
    if not isinstance(url, str) or not url.startswith("http"):
        return {"url": "", "text": "", "jans": []}
    pg = page(url)
    text = " ".join([
        pg.get("title", ""), pg.get("h1", ""), pg.get("description", ""), pg.get("text", "")[:35000]
    ]) if pg.get("ok") else source_slug_text(product)
    return {"url": url, "text": text, "jans": sorted(jans(text))}


def evaluate(
    product: dict, candidate: SResult, official_domains: list[str], localized: list[str],
    clue_text: str, amazon: bool = False,
) -> dict:
    if amazon:
        pg = {
            "ok": True, "url": candidate.url, "domain": domain_of(candidate.url),
            "title": candidate.title, "h1": "", "description": candidate.snippet,
            "text": candidate.title + " " + candidate.snippet,
        }
    else:
        pg = page(candidate.url)
        if not pg.get("ok"):
            # Search-result evidence can identify a precise official product page even if the site blocks the runner.
            droot = root_domain(domain_of(candidate.url))
            if droot in official_domains and not bad_path(candidate.url):
                pg = {
                    "ok": True, "url": candidate.url, "domain": domain_of(candidate.url),
                    "title": candidate.title, "h1": "", "description": candidate.snippet,
                    "text": candidate.title + " " + candidate.snippet,
                    "search_only": True,
                }
            else:
                return {"url": candidate.url, "status": "UNREACHABLE", "score": -999, "evidence": ["page fetch failed"]}

    url = pg.get("url") or candidate.url
    droot = root_domain(pg.get("domain") or domain_of(url))
    official = not amazon and droot in official_domains and not is_reseller(droot) and not bad_path(url)
    text = " ".join([
        candidate.title, candidate.snippet, pg.get("title", ""), pg.get("h1", ""),
        pg.get("description", ""), pg.get("text", ""),
    ])
    ntext = norm(text)

    models = explicit_models(product, clue_text)
    known_jans = set(jans(clue_text))
    for v in product.get("variants", []):
        known_jans.update(jans(v.get("barcode", "")))
    measures = measure_tokens(product.get("title", "") + " " + source_slug_text(product) + " " + clue_text[:7000])
    exact_models = sorted((m for m in models if len(m) >= 4 and m in ntext), key=lambda x: (-len(x), x))
    exact_jans = sorted(x for x in known_jans if x in text)
    exact_measures = sorted(x for x in measures if x in ntext)

    expected_titles = [product.get("title", ""), cleaned_title(product), source_slug_text(product)] + localized
    candidate_heading = " ".join([pg.get("title", ""), pg.get("h1", ""), candidate.title])
    title_seq = max((seq_similarity(x, candidate_heading) for x in expected_titles if x), default=0.0)
    title_words = max((word_overlap(x, text[:25000]) for x in expected_titles if x), default=0.0)
    brand_sim = brand_domain_similarity(product.get("vendor", ""), pg.get("domain", ""))

    score = 0
    evidence = []
    if official:
        score += 36
        evidence.append("verified official brand domain")
    if exact_jans:
        score += 78
        evidence.append("exact JAN " + ", ".join(exact_jans[:3]))
    if exact_models:
        score += 58
        evidence.append("exact model " + ", ".join(exact_models[:4]))
    if exact_measures:
        score += min(28, 12 + 4 * len(exact_measures))
        evidence.append("matching size/count " + ", ".join(exact_measures[:5]))
    if title_seq >= 0.72:
        score += 40
        evidence.append(f"localized title similarity {title_seq:.2f}")
    elif title_seq >= 0.58:
        score += 28
        evidence.append(f"localized title similarity {title_seq:.2f}")
    elif title_seq >= 0.45:
        score += 15
        evidence.append(f"localized title similarity {title_seq:.2f}")
    if title_words >= 0.65:
        score += 30
        evidence.append(f"title-token overlap {title_words:.2f}")
    elif title_words >= 0.45:
        score += 17
        evidence.append(f"title-token overlap {title_words:.2f}")
    if brand_sim >= 0.75:
        score += 17
        evidence.append("brand/domain name alignment")

    result_text = candidate.title + " " + candidate.snippet
    amazon_official_hint = amazon and ("公式" in result_text or "official" in result_text.lower())
    amazon_vendor_match = amazon and norm(product.get("vendor", "")) in norm(result_text)
    if amazon_official_hint and amazon_vendor_match:
        score += 30
        evidence.append("Amazon Japan result explicitly marked official brand listing")

    size_gate = bool(exact_measures) or not measures
    exact_identifier = bool(exact_models or exact_jans)
    strong_title = title_seq >= 0.62 or title_words >= 0.70

    if amazon:
        if exact_identifier and size_gate and (title_seq >= 0.40 or title_words >= 0.38):
            status = "VERIFIED_AMAZON_JP_EXACT"
        elif amazon_official_hint and amazon_vendor_match and strong_title and size_gate:
            status = "VERIFIED_AMAZON_JP_BRAND_TITLE_SIZE"
        else:
            status = "AMAZON_JP_INSUFFICIENT"
    else:
        if official and exact_identifier and size_gate and (title_seq >= 0.34 or title_words >= 0.32):
            status = "VERIFIED_OFFICIAL_EXACT"
        elif official and strong_title and size_gate:
            status = "VERIFIED_OFFICIAL_TITLE_SIZE"
        elif official and (exact_identifier or title_seq >= 0.42 or title_words >= 0.40):
            status = "HOLD_OFFICIAL_CANDIDATE"
        else:
            status = "REJECTED_OR_WEAK"

    return {
        "url": url, "domain": pg.get("domain") or domain_of(url), "status": status,
        "score": score, "official": official, "search_title": candidate.title,
        "page_title": pg.get("title", ""), "evidence": evidence,
        "matched_models": exact_models, "matched_jans": exact_jans,
        "matched_measures": exact_measures, "localized_title_similarity": round(title_seq, 3),
        "title_token_overlap": round(title_words, 3), "engine": candidate.engine,
        "search_only_validation": bool(pg.get("search_only")),
    }


def audit(product: dict) -> dict:
    started = time.time()
    clue = existing_clue(product)
    localized, broad_results = localized_clues(product)
    official_domains = discover_official_domains(product.get("vendor", ""))
    model_codes = sorted(explicit_models(product, clue.get("text", "")), key=lambda x: (-len(x), x))
    queries = []
    official_candidates = []
    amazon_candidates = []
    seen = set()

    # Evaluate product-search results on official domains immediately.
    for r in broad_results:
        droot = root_domain(domain_of(r.url))
        if droot in official_domains and not bad_path(r.url):
            key = r.url.split("?")[0].rstrip("/")
            if key not in seen:
                seen.add(key)
                official_candidates.append(evaluate(product, r, official_domains, localized, clue.get("text", ""), False))

    identity_phrases = localized[:3] + [cleaned_title(product), source_slug_text(product)]
    for domain in official_domains[:4]:
        site_queries = []
        if model_codes:
            site_queries.append(f'site:{domain} "{model_codes[0]}"')
        for phrase in identity_phrases[:3]:
            if phrase:
                site_queries.append(f"site:{domain} {phrase[:170]}")
        for q in site_queries[:3]:
            queries.append(q)
            for r in search(q, 10):
                if root_domain(domain_of(r.url)) != domain or bad_path(r.url):
                    continue
                key = r.url.split("?")[0].rstrip("/")
                if key in seen:
                    continue
                seen.add(key)
                official_candidates.append(evaluate(product, r, official_domains, localized, clue.get("text", ""), False))

    official_candidates.sort(key=lambda x: x.get("score", -999), reverse=True)
    verified = [x for x in official_candidates if x.get("status") in (
        "VERIFIED_OFFICIAL_EXACT", "VERIFIED_OFFICIAL_TITLE_SIZE",
    )]
    best = verified[0] if verified else (official_candidates[0] if official_candidates else None)

    if not verified:
        vendor = product.get("vendor", "")
        ident = model_codes[0] if model_codes else (localized[0] if localized else cleaned_title(product))
        aq = f"site:amazon.co.jp {vendor} {ident[:170]}"
        queries.append(aq)
        amazon_rows = [r for r in broad_results if is_amazon(r.url)] + [r for r in search(aq, 10) if is_amazon(r.url)]
        amazon_seen = set()
        for r in amazon_rows:
            key = r.url.split("?")[0].rstrip("/")
            if key in amazon_seen:
                continue
            amazon_seen.add(key)
            amazon_candidates.append(evaluate(product, r, official_domains, localized, clue.get("text", ""), True))
        amazon_candidates.sort(key=lambda x: x.get("score", -999), reverse=True)
        amazon_verified = [x for x in amazon_candidates if x.get("status") in (
            "VERIFIED_AMAZON_JP_EXACT", "VERIFIED_AMAZON_JP_BRAND_TITLE_SIZE",
        )]
        if amazon_verified:
            best = amazon_verified[0]
            verified = amazon_verified

    accepted = {
        "VERIFIED_OFFICIAL_EXACT", "VERIFIED_OFFICIAL_TITLE_SIZE",
        "VERIFIED_AMAZON_JP_EXACT", "VERIFIED_AMAZON_JP_BRAND_TITLE_SIZE",
    }
    if best and best.get("status") in accepted:
        final_status = best["status"]
        final_url = best["url"]
        source_type = "OFFICIAL_BRAND_JP" if "OFFICIAL" in final_status else "AMAZON_JP"
        confidence = "HIGH" if best.get("score", 0) >= 108 else "MEDIUM"
    elif best and best.get("status") == "HOLD_OFFICIAL_CANDIDATE":
        final_status = "HOLD_OFFICIAL_CANDIDATE"
        final_url = ""
        source_type = ""
        confidence = "LOW"
    elif official_domains:
        final_status = "UNRESOLVED_ON_KNOWN_OFFICIAL_DOMAIN"
        final_url = ""
        source_type = ""
        confidence = "NONE"
    else:
        final_status = "UNRESOLVED_NO_OFFICIAL_DOMAIN"
        final_url = ""
        source_type = ""
        confidence = "NONE"

    return {
        "shopify_product_id": product.get("legacyResourceId"), "handle": product.get("handle"),
        "store_title": product.get("title"), "vendor": product.get("vendor"),
        "catalog_status": product.get("status"), "store_url": product.get("onlineStoreUrl"),
        "variant_skus": [v.get("sku") for v in product.get("variants", []) if v.get("sku")],
        "variant_barcodes": [v.get("barcode") for v in product.get("variants", []) if v.get("barcode")],
        "existing_reseller_clue": clue.get("url", ""), "clue_jans": clue.get("jans", []),
        "public_model_codes": model_codes, "localized_identity_clues": localized,
        "official_domains": official_domains, "final_source_url": final_url,
        "source_type": source_type, "final_status": final_status, "confidence": confidence,
        "verification_evidence": best.get("evidence", []) if best else [],
        "matched_model_codes": best.get("matched_models", []) if best else [],
        "matched_jans": best.get("matched_jans", []) if best else [],
        "matched_size_count": best.get("matched_measures", []) if best else [],
        "best_candidate_url": best.get("url", "") if best else "",
        "best_candidate_status": best.get("status", "") if best else "",
        "best_candidate_score": best.get("score") if best else None,
        "queries": queries, "official_candidates_reviewed": official_candidates[:6],
        "amazon_candidates_reviewed": amazon_candidates[:4],
        "elapsed_seconds": round(time.time() - started, 2), "engine_version": VERSION,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.1)
    ns = ap.parse_args()
    products = json.loads(Path(ns.input).read_text(encoding="utf-8"))[ns.start:]
    if ns.limit:
        products = products[:ns.limit]
    outp = Path(ns.output)
    outp.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for idx, product in enumerate(products, 1):
        try:
            row = audit(product)
        except Exception as exc:
            row = {
                "shopify_product_id": product.get("legacyResourceId"),
                "handle": product.get("handle"), "store_title": product.get("title"),
                "vendor": product.get("vendor"), "final_source_url": "", "source_type": "",
                "final_status": "ENGINE_ERROR", "confidence": "NONE",
                "verification_evidence": [f"{type(exc).__name__}: {exc}"],
                "engine_version": VERSION,
            }
        rows.append(row)
        outp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({
            "n": idx, "id": row.get("shopify_product_id"), "status": row.get("final_status"),
            "url": row.get("final_source_url"), "domains": row.get("official_domains"),
            "elapsed": row.get("elapsed_seconds"),
        }, ensure_ascii=False), flush=True)
        if ns.delay:
            time.sleep(ns.delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
