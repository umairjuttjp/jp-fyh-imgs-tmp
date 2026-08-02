#!/usr/bin/env python3
"""JapansPrime scalable Japanese-source matcher, v5.

V5 keeps V4's bounded request budget while correcting two pilot findings:
- English count/set expressions such as "24 colors", "5 pack", and "100 count"
  are mandatory exactness attributes, preventing family pages from passing.
- A brand-controlled numeric/detail product page can pass when the exact size/count
  matches and meaningful title tokens overlap, even when the official title is Japanese.

It also improves Amazon Japan and official-site queries and adds product-specific
brand domains for KATE and Nihonbashi Kiya.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import source_audit_engine_v3 as core
import source_audit_engine_v4 as fast

VERSION = "2026-08-02.5"


def enhanced_measure_tokens(value: str) -> set[str]:
    compact = core.nfkc(value).lower().replace(" ", "")
    patterns = [
        r"\d+(?:\.\d+)?(?:ml|l|g|kg|cm|mm|oz|lb)",
        r"\d+(?:\.\d+)?(?:ミリリットル|リットル|グラム|キログラム|センチ|ミリ)",
        r"\d+(?:色|本|個|枚|袋|包|粒|錠|カプセル|回分|点|種)",
        r"\d+(?:colors?|colours?|pieces?|pcs?|count|ct|packs?|sets?|sachets?|bags?|bottles?|tablets?|capsules?)",
    ]
    out = set()
    for pattern in patterns:
        out.update(core.norm(x) for x in re.findall(pattern, compact, re.I))
    return out


# Core scoring calls this function dynamically, so replacing it upgrades every candidate gate.
core.measure_tokens = enhanced_measure_tokens


def extra_domains(product: dict) -> list[str]:
    title = core.norm(product.get("title", ""))
    vendor = core.norm(product.get("vendor", ""))
    out = []
    if "kate" in title:
        out.append("nomorerules.net")
    if "kiya" in vendor or "kiya" in title or "nihonbashikiya" in vendor:
        out.append("kiya-hamono.co.jp")
    if "aohata" in title or "verde" in title or "kewpie" in vendor:
        out.append("kewpie.co.jp")
    if "hadalabo" in vendor or "hadalabo" in title:
        out.append("rohto.co.jp")
    return out


def improved_broad_search(product: dict):
    vendor = str(product.get("vendor") or "").strip()
    title = core.cleaned_title(product)
    slug = core.source_slug_text(product)
    query = f"{vendor} {title} 公式".strip()
    rows = core.search(query, 14)
    clues = []
    # Put catalog identity first so refinement and Amazon queries remain precise.
    for value in (title, slug):
        value = re.sub(r"\s+", " ", value or "").strip()
        if value and value not in clues:
            clues.append(value[:240])
    for row in rows:
        for value in (row.title, row.snippet):
            value = re.sub(r"\s+", " ", value or "").strip()
            if value and value not in clues:
                clues.append(value[:240])
    return clues[:12], rows, query


def improved_discover_domains(product: dict, rows):
    domains, queries = fast.discover_domains(product, rows)
    domains = extra_domains(product) + domains
    return list(dict.fromkeys(domains))[:6], queries


def product_detail_shape(url: str) -> bool:
    path = urlparse(url).path.lower().rstrip("/")
    last = path.split("/")[-1] if path else ""
    if re.search(r"\d{5,}", path):
        return True
    if any(x in path for x in ("/detail/", "/products/", "/product/", "/item/", "/goods/")):
        return last not in {"product", "products", "item", "items", "goods", "detail"}
    return bool(last and len(last) >= 8 and last not in {"sketch", "eyeliner", "cleansing", "lotion"})


def improved_evaluate_unique(product, rows, official_domains, localized, clue_text, amazon, seen, cap):
    results = []
    for row in rows:
        if amazon:
            if not core.is_amazon(row.url):
                continue
        else:
            domain = core.root_domain(core.domain_of(row.url))
            if domain not in official_domains or core.bad_path(row.url):
                continue
        key = row.url.split("?")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        candidate = core.evaluate(product, row, official_domains, localized, clue_text, amazon)
        # Multilingual exact product detail: official domain + exact size/count + meaningful identity overlap.
        if (
            not amazon
            and candidate.get("status") == "HOLD_OFFICIAL_CANDIDATE"
            and candidate.get("official")
            and candidate.get("matched_measures")
            and candidate.get("title_token_overlap", 0) >= 0.38
            and product_detail_shape(candidate.get("url", ""))
        ):
            candidate["status"] = "VERIFIED_OFFICIAL_TITLE_SIZE"
            candidate["score"] = candidate.get("score", 0) + 22
            candidate.setdefault("evidence", []).append(
                "official detail page with exact size/count and multilingual identity overlap"
            )
        results.append(candidate)
        if len(results) >= cap:
            break
    return results


fast.broad_search = improved_broad_search
fast.discover_domains = improved_discover_domains
fast.evaluate_unique = improved_evaluate_unique


def audit(product: dict) -> dict:
    row = fast.audit(product)
    row["engine_version"] = VERSION
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.02)
    ns = ap.parse_args()
    products = json.loads(Path(ns.input).read_text(encoding="utf-8"))[ns.start:]
    if ns.limit:
        products = products[:ns.limit]
    output = Path(ns.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, product in enumerate(products, 1):
        try:
            row = audit(product)
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
                "engine_version": VERSION,
            }
        rows.append(row)
        output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({
            "n": index,
            "id": row.get("shopify_product_id"),
            "status": row.get("final_status"),
            "url": row.get("final_source_url"),
            "elapsed": row.get("elapsed_seconds"),
        }, ensure_ascii=False), flush=True)
        if ns.delay:
            time.sleep(ns.delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
