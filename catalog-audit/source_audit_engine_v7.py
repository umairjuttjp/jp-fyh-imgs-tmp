#!/usr/bin/env python3
"""JapansPrime production source matcher, v7.

Production gates added after V6 pilot review:
- Set/count attributes must appear in a result heading or product URL, not merely
  somewhere in a broad family page body.
- Network requests are capped to prevent one blocked official site from stalling a chunk.
- Amazon Japan can pass on exact branded title + exact size when no model/JAN exists.
- Known official Kao and product-brand domains are expanded.
- Tracking query parameters are removed from final source URLs.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import source_audit_engine_v6 as tightened

core = tightened.core
fast = tightened.fast
VERSION = "2026-08-02.7"

# Expand verified brand-controlled domains.
core.SEED_DOMAINS.setdefault("kao", [])
for domain in ("kao.com", "kao-kirei.com"):
    if domain not in core.SEED_DOMAINS["kao"]:
        core.SEED_DOMAINS["kao"].append(domain)
core.SEED_DOMAINS.setdefault("kanebo", [])
if "nomorerules.net" not in core.SEED_DOMAINS["kanebo"]:
    core.SEED_DOMAINS["kanebo"].append("nomorerules.net")
core.SEED_DOMAINS.setdefault("kiya", [])
core.SEED_DOMAINS.setdefault("nihonbashi kiya", [])
for key in ("kiya", "nihonbashi kiya"):
    if "kiya-hamono.co.jp" not in core.SEED_DOMAINS[key]:
        core.SEED_DOMAINS[key].append("kiya-hamono.co.jp")

# Bound every HTTP request, including page fetches that V6 showed could take ~50 seconds.
_ORIGINAL_GET = core.get

def bounded_get(url: str, timeout: int = 20, retries: int = 1):
    return _ORIGINAL_GET(url, min(timeout, 12), 0)

core.get = bounded_get


def count_requirements(product: dict) -> list[tuple[str, str]]:
    text = core.nfkc(product.get("title", "")).lower().replace(" ", "")
    requirements = []
    patterns = [
        (r"(\d+)(?:colors?|colours?)", "color"),
        (r"(\d+)(?:pieces?|pcs?)", "piece"),
        (r"(\d+)(?:count|ct)", "count"),
        (r"(\d+)(?:packs?)", "pack"),
        (r"(\d+)(?:sets?)", "set"),
        (r"(\d+)(?:sachets?)", "sachet"),
        (r"(\d+)(?:bags?)", "bag"),
        (r"(\d+)(?:bottles?)", "bottle"),
        (r"(\d+)(?:tablets?)", "tablet"),
        (r"(\d+)(?:capsules?)", "capsule"),
        (r"(\d+)(?:色|本|個|枚|袋|包|粒|錠|点|種)", "jp_count"),
    ]
    for pattern, kind in patterns:
        for number in re.findall(pattern, text, re.I):
            requirements.append((number, kind))
    return list(dict.fromkeys(requirements))


def count_query_hint(product: dict) -> str:
    hints = []
    for number, kind in count_requirements(product):
        if kind == "color":
            hints.extend([f"{number}色", f"{number} color set"])
        elif kind in {"piece", "count", "pack", "set"}:
            hints.extend([f"{number}個", f"{number} pack"])
        elif kind == "sachet":
            hints.extend([f"{number}包", f"{number} sachets"])
        elif kind == "bag":
            hints.extend([f"{number}袋", f"{number} bags"])
        elif kind == "tablet":
            hints.extend([f"{number}錠", f"{number} tablets"])
        elif kind == "capsule":
            hints.extend([f"{number}カプセル", f"{number} capsules"])
        else:
            hints.append(number)
    return " ".join(dict.fromkeys(hints))


def production_broad_search(product: dict):
    vendor = str(product.get("vendor") or "").strip()
    title = core.cleaned_title(product)
    slug = core.source_slug_text(product)
    hint = count_query_hint(product)
    query = " ".join(x for x in (vendor, title, hint, "公式") if x).strip()
    rows = core.search(query, 16)
    clues = []
    first_identity = " ".join(x for x in (title, hint) if x).strip()
    for value in (first_identity, slug):
        value = re.sub(r"\s+", " ", value or "").strip()
        if value and value not in clues:
            clues.append(value[:260])
    for row in rows:
        for value in (row.title, row.snippet):
            value = re.sub(r"\s+", " ", value or "").strip()
            if value and value not in clues:
                clues.append(value[:260])
    return clues[:14], rows, query


def heading_satisfies_counts(product: dict, candidate: dict) -> bool:
    requirements = count_requirements(product)
    if not requirements:
        return True
    heading = core.norm(" ".join([
        candidate.get("search_title", ""),
        candidate.get("page_title", ""),
        candidate.get("url", ""),
    ]))
    return all(number in heading for number, _kind in requirements)


def production_evaluate_unique(product, rows, official_domains, localized, clue_text, amazon, seen, cap):
    results = []
    vendor_norm = core.norm(product.get("vendor", ""))
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

        # Apply V6 multilingual detail-page promotion.
        if (
            not amazon
            and candidate.get("status") == "HOLD_OFFICIAL_CANDIDATE"
            and candidate.get("official")
            and candidate.get("matched_measures")
            and candidate.get("title_token_overlap", 0) >= 0.38
            and tightened.product_detail_shape(candidate.get("url", ""))
        ):
            candidate["status"] = "VERIFIED_OFFICIAL_TITLE_SIZE"
            candidate["score"] = candidate.get("score", 0) + 22
            candidate.setdefault("evidence", []).append(
                "official detail page with exact size/count and multilingual identity overlap"
            )

        # A generic family page cannot satisfy a set/count merely because the body lists that set.
        if candidate.get("status", "").startswith("VERIFIED_") and not heading_satisfies_counts(product, candidate):
            candidate["status"] = "HOLD_OFFICIAL_CANDIDATE" if not amazon else "AMAZON_JP_INSUFFICIENT"
            candidate["score"] = max(0, candidate.get("score", 0) - 45)
            candidate.setdefault("evidence", []).append(
                "required set/count is absent from page heading and URL"
            )

        # Exact branded Amazon product identity when model/JAN is unavailable.
        if amazon and candidate.get("status") == "AMAZON_JP_INSUFFICIENT":
            result_identity = core.norm(" ".join([
                candidate.get("search_title", ""), candidate.get("page_title", "")
            ]))
            vendor_match = bool(vendor_norm and vendor_norm in result_identity)
            exact_size = bool(candidate.get("matched_measures"))
            strong_title = (
                candidate.get("title_token_overlap", 0) >= 0.75
                or candidate.get("localized_title_similarity", 0) >= 0.60
            )
            if vendor_match and exact_size and strong_title and heading_satisfies_counts(product, candidate):
                candidate["status"] = "VERIFIED_AMAZON_JP_BRAND_TITLE_SIZE"
                candidate["score"] = candidate.get("score", 0) + 34
                candidate.setdefault("evidence", []).append(
                    "exact branded Amazon Japan title and size/count match"
                )

        results.append(candidate)
        if len(results) >= cap:
            break
    return results


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if core.is_amazon(url):
        # Stable ASIN path; all search and language parameters are unnecessary.
        match = re.search(r"(/dp/[A-Z0-9]{10})", parsed.path, re.I)
        path = match.group(1) if match else parsed.path
        return urlunparse((parsed.scheme or "https", parsed.netloc, path, "", "", ""))
    kept = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        low = key.lower()
        if low.startswith("utm_") or low in {"srsltid", "gclid", "fbclid", "ref", "ref_", "source"}:
            continue
        kept.append((key, value))
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(kept), ""))


fast.broad_search = production_broad_search
fast.evaluate_unique = production_evaluate_unique


def audit(product: dict) -> dict:
    row = fast.audit(product)
    row["final_source_url"] = canonical_url(row.get("final_source_url", ""))
    row["best_candidate_url"] = canonical_url(row.get("best_candidate_url", ""))
    for key in ("official_candidates_reviewed", "amazon_candidates_reviewed"):
        for candidate in row.get(key, []) or []:
            candidate["url"] = canonical_url(candidate.get("url", ""))
    row["engine_version"] = VERSION
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--delay", type=float, default=0.02)
    args = parser.parse_args()
    products = json.loads(Path(args.input).read_text(encoding="utf-8"))[args.start:]
    if args.limit:
        products = products[:args.limit]
    output = Path(args.output)
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
            "n": index, "id": row.get("shopify_product_id"),
            "status": row.get("final_status"), "url": row.get("final_source_url"),
            "elapsed": row.get("elapsed_seconds"),
        }, ensure_ascii=False), flush=True)
        if args.delay:
            time.sleep(args.delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
