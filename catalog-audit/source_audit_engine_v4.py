#!/usr/bin/env python3
"""Scalable JapansPrime Japanese-source matcher, v4.

This is a conservative, request-bounded wrapper around v3's identity extraction,
page validation, and exactness scoring. It avoids fetching reseller clue pages and
limits each product to:
- one broad Yahoo Japan search,
- at most two official-domain refinement searches,
- one Amazon Japan fallback search when necessary,
- page fetches only for top official candidates.

Weak evidence remains HOLD/BLOCK. Every input row receives a decision.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import source_audit_engine_v3 as core

VERSION = "2026-08-02.4"
ACCEPTED = {
    "VERIFIED_OFFICIAL_EXACT",
    "VERIFIED_OFFICIAL_TITLE_SIZE",
    "VERIFIED_AMAZON_JP_EXACT",
    "VERIFIED_AMAZON_JP_BRAND_TITLE_SIZE",
}


def quick_clue(product: dict) -> dict:
    url = (product.get("sources") or {}).get("sourceUrl") or ""
    if not isinstance(url, str):
        url = ""
    slug = core.source_slug_text(product)
    text = " ".join([str(product.get("title") or ""), slug])
    known_jans = set()
    for variant in product.get("variants", []):
        known_jans.update(core.jans(variant.get("barcode", "")))
    return {"url": url, "text": text, "jans": sorted(known_jans)}


def broad_search(product: dict) -> tuple[list[str], list[core.SResult], str]:
    vendor = str(product.get("vendor") or "").strip()
    title = core.cleaned_title(product)
    slug = core.source_slug_text(product)
    identity = title
    if slug and core.norm(slug) not in core.norm(title):
        identity = (title + " " + slug)[:220]
    query = f"{vendor} {identity}".strip()
    rows = core.search(query, 12)
    clues = []
    for row in rows:
        for value in (row.title, row.snippet):
            value = re.sub(r"\s+", " ", value or "").strip()
            if value and value not in clues:
                clues.append(value[:240])
    return clues[:10], rows, query


def discover_domains(product: dict, rows: list[core.SResult]) -> tuple[list[str], list[str]]:
    vendor = str(product.get("vendor") or "").strip()
    domains = list(core.seed_domains(vendor))
    queries = []
    for row in rows:
        domain = core.root_domain(core.domain_of(row.url))
        if not domain or core.is_reseller(domain) or core.is_amazon(row.url):
            continue
        combined = (row.title + " " + row.snippet)
        official_hint = "公式" in combined or "official" in combined.lower()
        brand_match = core.brand_domain_similarity(vendor, domain) >= 0.55
        if official_hint and (brand_match or core.norm(vendor) in core.norm(combined)):
            domains.append(domain)
    domains = list(dict.fromkeys(domains))[:5]
    if not domains and vendor:
        q = f"{vendor} 公式サイト"
        queries.append(q)
        for row in core.search(q, 8):
            domain = core.root_domain(core.domain_of(row.url))
            combined = row.title + " " + row.snippet
            if not domain or core.is_reseller(domain):
                continue
            if ("公式" in combined or "official" in combined.lower()) and (
                core.brand_domain_similarity(vendor, domain) >= 0.5
                or core.norm(vendor) in core.norm(combined)
            ):
                domains.append(domain)
        domains = list(dict.fromkeys(domains))[:4]
    return domains, queries


def evaluate_unique(
    product: dict,
    rows: list[core.SResult],
    official_domains: list[str],
    localized: list[str],
    clue_text: str,
    amazon: bool,
    seen: set[str],
    cap: int,
) -> list[dict]:
    out = []
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
        out.append(core.evaluate(product, row, official_domains, localized, clue_text, amazon))
        if len(out) >= cap:
            break
    return out


def audit(product: dict) -> dict:
    started = time.time()
    clue = quick_clue(product)
    localized, broad_rows, broad_query = broad_search(product)
    official_domains, domain_queries = discover_domains(product, broad_rows)
    model_codes = sorted(core.explicit_models(product, clue["text"]), key=lambda x: (-len(x), x))
    queries = [broad_query] + domain_queries

    seen_official: set[str] = set()
    official_candidates = evaluate_unique(
        product, broad_rows, official_domains, localized, clue["text"], False, seen_official, 3
    )

    official_candidates.sort(key=lambda x: x.get("score", -999), reverse=True)
    verified = [x for x in official_candidates if x.get("status") in {
        "VERIFIED_OFFICIAL_EXACT", "VERIFIED_OFFICIAL_TITLE_SIZE"
    }]

    # One refinement query for each of at most two established official domains.
    if not verified:
        identity = model_codes[0] if model_codes else (
            localized[0] if localized else core.cleaned_title(product)
        )
        for domain in official_domains[:2]:
            q = f"site:{domain} {identity[:180]}"
            queries.append(q)
            rows = core.search(q, 10)
            official_candidates.extend(evaluate_unique(
                product, rows, official_domains, localized, clue["text"], False, seen_official, 2
            ))
            official_candidates.sort(key=lambda x: x.get("score", -999), reverse=True)
            verified = [x for x in official_candidates if x.get("status") in {
                "VERIFIED_OFFICIAL_EXACT", "VERIFIED_OFFICIAL_TITLE_SIZE"
            }]
            if verified:
                break

    best = verified[0] if verified else (official_candidates[0] if official_candidates else None)

    amazon_candidates = []
    if not verified:
        seen_amazon: set[str] = set()
        amazon_candidates.extend(evaluate_unique(
            product, broad_rows, official_domains, localized, clue["text"], True, seen_amazon, 4
        ))
        amazon_verified = [x for x in amazon_candidates if x.get("status") in {
            "VERIFIED_AMAZON_JP_EXACT", "VERIFIED_AMAZON_JP_BRAND_TITLE_SIZE"
        }]
        if not amazon_verified:
            vendor = str(product.get("vendor") or "").strip()
            identity = model_codes[0] if model_codes else (
                localized[0] if localized else core.cleaned_title(product)
            )
            q = f"site:amazon.co.jp {vendor} {identity[:180]}"
            queries.append(q)
            rows = core.search(q, 10)
            amazon_candidates.extend(evaluate_unique(
                product, rows, official_domains, localized, clue["text"], True, seen_amazon, 4
            ))
            amazon_verified = [x for x in amazon_candidates if x.get("status") in {
                "VERIFIED_AMAZON_JP_EXACT", "VERIFIED_AMAZON_JP_BRAND_TITLE_SIZE"
            }]
        amazon_candidates.sort(key=lambda x: x.get("score", -999), reverse=True)
        if amazon_verified:
            amazon_verified.sort(key=lambda x: x.get("score", -999), reverse=True)
            best = amazon_verified[0]
            verified = amazon_verified

    if best and best.get("status") in ACCEPTED:
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
        "shopify_product_id": product.get("legacyResourceId"),
        "handle": product.get("handle"),
        "store_title": product.get("title"),
        "vendor": product.get("vendor"),
        "catalog_status": product.get("status"),
        "store_url": product.get("onlineStoreUrl"),
        "variant_skus": [v.get("sku") for v in product.get("variants", []) if v.get("sku")],
        "variant_barcodes": [v.get("barcode") for v in product.get("variants", []) if v.get("barcode")],
        "existing_reseller_clue": clue.get("url", ""),
        "clue_jans": clue.get("jans", []),
        "public_model_codes": model_codes,
        "localized_identity_clues": localized,
        "official_domains": official_domains,
        "final_source_url": final_url,
        "source_type": source_type,
        "final_status": final_status,
        "confidence": confidence,
        "verification_evidence": best.get("evidence", []) if best else [],
        "matched_model_codes": best.get("matched_models", []) if best else [],
        "matched_jans": best.get("matched_jans", []) if best else [],
        "matched_size_count": best.get("matched_measures", []) if best else [],
        "best_candidate_url": best.get("url", "") if best else "",
        "best_candidate_status": best.get("status", "") if best else "",
        "best_candidate_score": best.get("score") if best else None,
        "queries": queries,
        "official_candidates_reviewed": sorted(
            official_candidates, key=lambda x: x.get("score", -999), reverse=True
        )[:5],
        "amazon_candidates_reviewed": sorted(
            amazon_candidates, key=lambda x: x.get("score", -999), reverse=True
        )[:4],
        "elapsed_seconds": round(time.time() - started, 2),
        "engine_version": VERSION,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.05)
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
