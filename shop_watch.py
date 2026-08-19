"""
Public deal scanner - menswear / fragrance / watches, genuine markdowns
from authorised retailers only. No secondhand scanning here (Vinted-style):
that's deliberate, see README. Same core logic as the personal Bargain
Watch project's shop_watch.py (sold-out filtering, real-markdown check,
one card per product not per variant) minus the personal size filter,
which doesn't apply to a public feed.
"""

import json
import time
from pathlib import Path

import requests

CONFIG_PATH = Path("config.json")
NOTES_PATH = Path("notes.json")
PRICE_HISTORY_PATH = Path("price_history.json")
OUTPUT_PATH = Path("docs/data.json")

REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (deal scanner; contact via repo)"}


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def parse_price(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_products(domain, max_pages=5):
    """Shopify's public product feed, paginated 250 at a time."""
    products = []
    for page in range(1, max_pages + 1):
        url = f"https://{domain}/products.json"
        resp = requests.get(
            url, params={"limit": 250, "page": page}, headers=REQUEST_HEADERS, timeout=20
        )
        resp.raise_for_status()
        batch = resp.json().get("products", [])
        if not batch:
            break
        products.extend(batch)
        if len(batch) < 250:
            break
        if page == max_pages:
            print(f"  ! catalogue truncated at {max_pages} pages ({len(products)} products) - some items not scanned")
        time.sleep(1)
    return products


def is_excluded(product, global_exclude):
    # Shopify's public products.json returns tags as one comma-separated
    # string, not a list - joining it character-by-character would silently
    # break every tag-based exclusion. Handle both shapes defensively.
    tags = product.get("tags", "")
    tags_text = tags if isinstance(tags, str) else " ".join(tags)
    text = " ".join([
        product.get("title", ""),
        product.get("product_type", ""),
        tags_text,
    ]).lower()
    return any(term in text for term in global_exclude)


def affiliate_wrap(url, shop):
    """Placeholder link wrapper - once real affiliate accounts are approved,
    this is the one place that changes: build the Awin/CJ tracking URL
    around `url` using shop['affiliate_id']. Returns the plain retailer
    link untouched until then, so the site works before affiliate approval."""
    if not shop.get("affiliate_id"):
        return url
    # Awin deep-link pattern, once affiliate_id is set:
    # return f"https://www.awin1.com/cread.php?awinmid={shop['affiliate_id']}&awinaffid=YOUR_PUBLISHER_ID&ued={url}"
    return url


def build_cards(product, shop, global_exclude, price_history, notes):
    if is_excluded(product, global_exclude):
        return []

    domain = shop["domain"]
    category = shop["category"]
    shop_name = shop["name"]

    on_sale_variants = []
    for variant in product.get("variants", []):
        # Sold-out variants stay in a shop's feed with their old markdown,
        # so without this check a card can list sizes that are actually
        # greyed out / "notify me". Only an explicit False counts as sold
        # out - if a shop's feed omits the field, everything still shows.
        if variant.get("available") is False:
            continue
        price = parse_price(variant.get("price"))
        compare_at = parse_price(variant.get("compare_at_price"))
        if price is None or compare_at is None or compare_at <= price:
            continue
        on_sale_variants.append((variant, price, compare_at))

    if not on_sale_variants:
        return []

    best_variant, price, compare_at = min(on_sale_variants, key=lambda v: v[1])
    discount_pct = round((1 - price / compare_at) * 100)

    # Available on-sale variant names (sizes for menswear, ml for fragrance).
    # Shopify uses "Default Title" for single-variant products - not a size.
    sizes = []
    for variant, _, _ in on_sale_variants:
        title = (variant.get("title") or "").strip()
        if title and title.lower() != "default title":
            sizes.append(title)

    # If on-sale variants have different prices, the shown price is the
    # cheapest, so the card should say "from".
    price_is_from = len({v[1] for v in on_sale_variants}) > 1

    product_id = product.get("id")
    item_id = f"{domain}-{product_id}"
    is_new = item_id not in price_history
    previous_price = price_history.get(item_id)

    price_dropped = False
    if previous_price is not None and previous_price > price:
        drop = previous_price - price
        if drop > 0.5 and drop / previous_price > 0.01:
            price_dropped = True

    score = min(discount_pct, 70)
    if price_dropped:
        score += 15
    if is_new:
        score += 5

    price_history[item_id] = price

    photo = None
    images = product.get("images") or []
    if images:
        photo = images[0].get("src")

    product_url = affiliate_wrap(f"https://{domain}/products/{product.get('handle')}", shop)

    return [{
        "id": item_id,
        "title": product.get("title", ""),
        "brand": product.get("vendor", ""),
        "category": category,
        "shop": shop_name,
        "price_amount": price,
        "price": f"{price:.2f} GBP",
        "price_is_from": price_is_from,
        "sizes": sizes,
        "discount_pct": discount_pct,
        "is_new": is_new,
        "price_dropped": price_dropped,
        "previous_price": previous_price,
        "photo": photo,
        "url": product_url,
        # Manual curation lookup, keyed by item_id in notes.json. Blank
        # by default - not every card needs a line, see README.
        "note": notes.get(item_id, ""),
        "score": score,
    }]


def main():
    config = json.loads(CONFIG_PATH.read_text())
    price_history = load_json(PRICE_HISTORY_PATH, {})
    notes = load_json(NOTES_PATH, {})
    global_exclude = config.get("global_exclude", [])
    min_discount = config.get("min_discount_pct", 20)
    # Per-category overrides: watch markdowns run 10-25% where fashion runs
    # 40-70%, so one global threshold silently excludes an entire category.
    min_discount_by_category = config.get("min_discount_pct_by_category", {})

    feed = []
    errors = []

    for shop in config["shop_watches"]:
        print(f"Scanning {shop['name']} ({shop['domain']})...")
        try:
            products = fetch_products(shop["domain"], max_pages=shop.get("max_pages", 5))
        except Exception as exc:
            print(f"  ! {shop['name']} failed: {exc}")
            errors.append({"shop": shop["name"], "error": str(exc)})
            continue

        for product in products:
            feed.extend(build_cards(product, shop, global_exclude, price_history, notes))

    def passes_threshold(item):
        threshold = min_discount_by_category.get(item["category"], min_discount)
        return item["discount_pct"] >= threshold

    feed = [item for item in feed if passes_threshold(item)]
    feed.sort(key=lambda item: item["score"], reverse=True)

    # Guarantee each category a floor of slots before filling the rest by
    # score, so 50-70% fashion markdowns can't push watches out entirely.
    feed_size = config.get("feed_size", 60)
    category_floor = config.get("category_floor", 6)
    selected = []
    selected_ids = set()
    by_category = {}
    for item in feed:
        by_category.setdefault(item["category"], []).append(item)
    for category_items in by_category.values():
        for item in category_items[:category_floor]:
            selected.append(item)
            selected_ids.add(item["id"])
    for item in feed:
        if len(selected) >= feed_size:
            break
        if item["id"] not in selected_ids:
            selected.append(item)
            selected_ids.add(item["id"])
    selected.sort(key=lambda item: item["score"], reverse=True)
    feed = selected[:feed_size]

    PRICE_HISTORY_PATH.write_text(json.dumps(price_history))
    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps({
        "generated_at": int(time.time()),
        "errors": errors,
        "feed": feed,
    }, indent=2))
    print(f"Done: {len(feed)} items in feed, {len(errors)} shop errors")


if __name__ == "__main__":
    main()
