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

# Below this many recorded scans, we don't claim anything about an item's
# price history - "lowest we've seen" is meaningless on the second sighting.
MIN_OBSERVATIONS_FOR_HISTORY = 4


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
    """Shopify's public product feed, paginated 250 at a time.

    A failure on any single page (transient 500s happen, especially on
    larger catalogues at deeper page numbers) stops pagination but keeps
    whatever was already fetched - losing pages 1-3 because page 4 hiccuped
    would throw away real, valid products for no good reason."""
    products = []
    for page in range(1, max_pages + 1):
        url = f"https://{domain}/products.json"
        try:
            resp = requests.get(
                url, params={"limit": 250, "page": page}, headers=REQUEST_HEADERS, timeout=20
            )
            resp.raise_for_status()
            batch = resp.json().get("products", [])
        except Exception as exc:
            print(f"  ! page {page} failed ({exc}) - keeping {len(products)} products already fetched")
            break
        if not batch:
            break
        products.extend(batch)
        if len(batch) < 250:
            break
        if page == max_pages:
            print(f"  ! catalogue truncated at {max_pages} pages ({len(products)} products) - some items not scanned")
        time.sleep(1)
    return products


def migrate_history(raw):
    """Old format was {item_id: last_price}. New format keeps the full shape
    of what we've observed: the low is the number that actually matters, since
    a retailer's 'was' price is theirs to invent but the price we watched it
    sit at for weeks is not."""
    migrated = {}
    for item_id, value in raw.items():
        if isinstance(value, dict):
            migrated[item_id] = value
        else:
            migrated[item_id] = {
                "last": value,
                "low": value,
                "high": value,
                "first_seen": int(time.time()),
                "observations": 1,
            }
    return migrated


def update_history(history, item_id, price):
    """Record this observation and return the record as it stood BEFORE
    this scan, so scoring can compare today against what came before."""
    record = history.get(item_id)
    if record is None:
        history[item_id] = {
            "last": price,
            "low": price,
            "high": price,
            "first_seen": int(time.time()),
            "observations": 1,
        }
        return None

    previous = dict(record)
    record["last"] = price
    record["low"] = min(record["low"], price)
    record["high"] = max(record["high"], price)
    record["observations"] = record.get("observations", 1) + 1
    return previous


def product_text(product):
    # Shopify's public products.json returns tags as one comma-separated
    # string, not a list - joining it character-by-character would silently
    # break every keyword check. Handle both shapes defensively.
    tags = product.get("tags", "")
    tags_text = tags if isinstance(tags, str) else " ".join(tags)
    return " ".join([
        product.get("title", ""),
        product.get("product_type", ""),
        tags_text,
    ]).lower()


def is_excluded(product, exclude_terms):
    text = product_text(product)
    return any(term in text for term in exclude_terms)


def is_relevant(product, must_include_any):
    """A retailer's own category tag isn't reliable enough on its own - a shop
    configured as 'fragrance' may also sell makeup, skincare and haircare, and
    none of those are what this site claims to curate. If must_include_any is
    set, the product needs at least one genuine positive signal (e.g. 'eau de
    parfum') to qualify, rather than just having come from the right domain."""
    if not must_include_any:
        return True
    text = product_text(product)
    return any(term in text for term in must_include_any)


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


def brand_is_notable(vendor, notable_brands):
    if not vendor or not notable_brands:
        return False
    vendor_lower = vendor.strip().lower()
    return any(vendor_lower == b.lower() for b in notable_brands)


def brand_fact(vendor, brand_facts):
    """A generic, verified one-liner about the brand itself - not the specific
    item. Applies automatically to every product from that vendor, so it
    doesn't need rewriting as the feed rotates. Manual notes.json entries
    take priority when a specific item genuinely needs its own line."""
    if not vendor or not brand_facts:
        return ""
    vendor_lower = vendor.strip().lower()
    for name, fact in brand_facts.items():
        if name.lower() == vendor_lower:
            return fact
    return ""


def build_cards(product, shop, exclude_terms, must_include_any, price_history, notes, notable_brands=None, notable_brand_facts=None):
    if is_excluded(product, exclude_terms):
        return []
    if not is_relevant(product, must_include_any):
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

    previous = update_history(price_history, item_id, price)
    record = price_history[item_id]

    previous_price = previous["last"] if previous else None
    observations = record.get("observations", 1)
    observed_low = record["low"]
    observed_high = record["high"]
    first_seen = record.get("first_seen")

    price_dropped = False
    if previous_price is not None and previous_price > price:
        drop = previous_price - price
        if drop > 0.5 and drop / previous_price > 0.01:
            price_dropped = True

    # How much history do we actually have? Below this, any "lowest yet" claim
    # is noise - we'd just be reporting the first price we ever saw.
    has_history = observations >= MIN_OBSERVATIONS_FOR_HISTORY

    # Is this the cheapest we've ever watched it go? This is the claim a
    # retailer cannot manufacture, unlike compare_at_price. Requires that we
    # have actually seen the price move: an item parked at one price forever
    # is technically always "at its low", which would make the badge worthless.
    has_moved = has_history and observed_high > observed_low
    at_observed_low = bool(has_moved and previous and price <= previous["low"])

    # How far below its own typical price is it sitting today?
    below_observed_high = 0
    if has_history and observed_high > 0:
        below_observed_high = round((1 - price / observed_high) * 100)

    # SCORING
    # Discount percentage is deliberately capped low and used only as a weak
    # tiebreak. It's the number a retailer controls entirely - inflating the
    # "was" price costs them nothing - so it cannot be the primary signal.
    # Everything weighted above it is something we observed ourselves.
    is_notable_brand = brand_is_notable(product.get("vendor", ""), notable_brands)
    score = 0
    score += min(discount_pct, 60) * 0.35          # weak signal, capped
    if at_observed_low:
        score += 45                                 # strongest: verified low
    if price_dropped:
        score += 20                                 # moved down since last scan
    score += min(below_observed_high, 50) * 0.4     # below its own typical price
    if has_history:
        score += min(observations, 40) * 0.25       # confidence in the above
    if is_new:
        score += 3                                  # mild freshness nudge only
    if is_notable_brand:
        score += 8                                  # researched reputation, not a gate
    score = round(score, 1)

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
        "compare_at": compare_at,
        "sizes": sizes,
        "discount_pct": discount_pct,
        "is_new": is_new,
        "price_dropped": price_dropped,
        "previous_price": previous_price,
        "at_observed_low": at_observed_low,
        "observed_low": observed_low if has_history else None,
        "observed_high": observed_high if has_history else None,
        "below_observed_high": below_observed_high,
        "observations": observations,
        "tracked_since": first_seen,
        "has_history": has_history,
        "is_notable_brand": is_notable_brand,
        "photo": photo,
        "url": product_url,
        # Manual per-item note wins if one exists. Otherwise fall back to a
        # generic, verified brand fact (if we have one) so items don't need
        # rewriting every time the feed rotates.
        "note": notes.get(item_id) or brand_fact(product.get("vendor", ""), notable_brand_facts),
        "score": score,
    }]


def main():
    config = json.loads(CONFIG_PATH.read_text())
    price_history = migrate_history(load_json(PRICE_HISTORY_PATH, {}))
    notes = load_json(NOTES_PATH, {})
    global_exclude = config.get("global_exclude", [])
    min_discount = config.get("min_discount_pct", 20)
    # Per-category overrides: watch markdowns run 10-25% where fashion runs
    # 40-70%, so one global threshold silently excludes an entire category.
    min_discount_by_category = config.get("min_discount_pct_by_category", {})
    # A shop's assigned category is not the same as what every product in its
    # catalogue actually is - Escentual sells makeup and haircare alongside
    # fragrance, and a "menswear" shop can still list women's items. These two
    # layers catch that: extra terms to always exclude for a given category,
    # and (for categories where it matters) a positive list a product must
    # match at least one of to count as genuinely in-category.
    category_exclude = config.get("category_exclude", {})
    category_must_include_any = config.get("category_must_include_any", {})
    notable_brands = config.get("notable_brands", {}).get("list", [])
    notable_brand_facts = config.get("notable_brands", {}).get("facts", {})
    min_price_by_category = config.get("min_price_by_category", {})
    max_discount_pct = config.get("max_discount_pct", 85)

    feed = []
    errors = []

    for shop in config["shop_watches"]:
        print(f"Scanning {shop['name']} ({shop['domain']})...")
        exclude_terms = global_exclude + category_exclude.get(shop["category"], [])
        must_include_any = category_must_include_any.get(shop["category"])
        try:
            products = fetch_products(shop["domain"], max_pages=shop.get("max_pages", 5))
        except Exception as exc:
            print(f"  ! {shop['name']} failed: {exc}")
            errors.append({"shop": shop["name"], "error": str(exc)})
            continue

        for product in products:
            feed.extend(build_cards(product, shop, exclude_terms, must_include_any, price_history, notes, notable_brands, notable_brand_facts))

    def passes_threshold(item):
        threshold = min_discount_by_category.get(item["category"], min_discount)
        floor = min_price_by_category.get(item["category"], 0)
        # Floor checks the ORIGINAL price, not the discounted one. A £45 item
        # marked down to £15 is exactly the kind of genuine markdown this site
        # exists to surface - checking the sale price would filter it out for
        # being a good deal, which is backwards. A £15 item that was always
        # £15 is the one actually being screened for.
        if item["discount_pct"] > max_discount_pct:
            print(f"  ! implausible discount excluded: {item['title']} claims -{item['discount_pct']}% "
                  f"(£{item['price_amount']} from £{item['compare_at']}) - likely miscoded price data "
                  f"(pre-order deposits, stale RRPs) rather than a real deal")
            return False
        return item["discount_pct"] >= threshold and item["compare_at"] >= floor

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
