"""
Public deal scanner - menswear / fragrance / watches, genuine markdowns
from authorised retailers only. No secondhand scanning here (Vinted-style):
that's deliberate, see README. Same core logic as the personal Bargain
Watch project's shop_watch.py (sold-out filtering, real-markdown check,
one card per product not per variant) minus the personal size filter,
which doesn't apply to a public feed.
"""

import json
import re
import time
from pathlib import Path
from urllib.parse import quote

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


# ---------------------------------------------------------------------------
# Price history
#
# Version 2 format (one item per line, sorted, so git stores small diffs):
#   {"version": 2, "scan": <scans run so far>, "items": {
#   "<item_id>": {"first_seen": <unix>, "first_scan": <n>, "low": x, "high": y,
#                 "prices": [[<scan n>, price], ...]},
#   ...}}
# "prices" only gets a new entry when the price CHANGES, so an unchanged item's
# line is identical scan after scan. Version 1 rewrote a running sum and a scan
# counter on every item every 3 hours; fine at 2,400 items, but tracking every
# in-scope product (~16,500 across the current shops) would have grown the repo
# by roughly 0.5 GB a year.
#
# The low is the number that matters most: a retailer's was-price is theirs to
# invent, the price watched over weeks is not. Typical price is the average
# over every scan, so one short-lived peak can't pose as the "normal" price.
# ---------------------------------------------------------------------------
HISTORY_VERSION = 2
MAX_PRICE_POINTS = 60  # older change points fold into legacy_sum/legacy_scans


def load_history(path):
    raw = load_json(path, {})
    if isinstance(raw, dict) and raw.get("version") == HISTORY_VERSION:
        return raw
    return migrate_history(raw)


def migrate_history(raw):
    """Converts version 1 ({item_id: {last, low, high, sum, first_seen,
    observations}}, or the even older {item_id: price}) to version 2 without
    losing anything: the old running sum and scan count carry over as
    legacy_sum/legacy_scans, so typical prices and check counts continue
    exactly where they left off."""
    now = int(time.time())
    records = {}
    for item_id, value in raw.items():
        if not isinstance(value, dict):
            value = {"last": value, "low": value, "high": value, "sum": value, "first_seen": now, "observations": 1}
        last = float(value.get("last", 0))
        observations = int(value.get("observations", 1))
        records[item_id] = (value, last, observations)
    # Every scan since the first has seen the oldest items, so the largest
    # observation count is the number of scans run so far.
    scan = max((obs for _, _, obs in records.values()), default=0)
    items = {}
    for item_id, (value, last, observations) in records.items():
        total = float(value.get("sum", last * observations))
        items[item_id] = {
            "first_seen": value.get("first_seen", now),
            "first_scan": scan - observations + 1,
            "low": value.get("low", last),
            "high": value.get("high", last),
            "legacy_sum": round(total - last, 2),
            "legacy_scans": observations - 1,
            "prices": [[scan, last]],
        }
    return {"version": HISTORY_VERSION, "scan": scan, "items": items}


def summarise_record(record, upto_scan):
    """The item's history as of `upto_scan`: last price, low, high, the sum
    of its price over every scan (for the typical/average price) and how many
    scans that covers. Scans an item was missing from (sold out, briefly
    delisted) count at its last known price."""
    total = record.get("legacy_sum", 0.0)
    scans = record.get("legacy_scans", 0)
    last = None
    prices = record["prices"]
    for i, (start, price) in enumerate(prices):
        if start > upto_scan:
            break
        end = prices[i + 1][0] if i + 1 < len(prices) else upto_scan + 1
        end = min(end, upto_scan + 1)
        total += price * (end - start)
        scans += end - start
        last = price
    return {
        "last": last,
        "low": record["low"],
        "high": record["high"],
        "sum": round(total, 2),
        "observations": scans,
        "first_seen": record.get("first_seen"),
    }


def update_history(history, item_id, price):
    """Records this scan's price. Returns (before, after): the item's summary
    as it stood after the previous scan (None if never seen) and as it stands
    now, so scoring can compare today against what came before."""
    scan = history["scan"]
    items = history["items"]
    record = items.get(item_id)
    if record is None:
        record = items[item_id] = {
            "first_seen": int(time.time()),
            "first_scan": scan,
            "low": price,
            "high": price,
            "prices": [[scan, price]],
        }
        return None, summarise_record(record, scan)

    before = summarise_record(record, scan - 1)
    if record["prices"][-1][1] != price:
        record["prices"].append([scan, price])
    record["low"] = min(record["low"], price)
    record["high"] = max(record["high"], price)
    if len(record["prices"]) > MAX_PRICE_POINTS:
        # Fold the oldest change points into the running totals.
        (start, old_price), next_start = record["prices"][0], record["prices"][1][0]
        record["legacy_sum"] = round(record.get("legacy_sum", 0.0) + old_price * (next_start - start), 2)
        record["legacy_scans"] = record.get("legacy_scans", 0) + (next_start - start)
        record["prices"].pop(0)
    return before, summarise_record(record, scan)


def write_history(path, history):
    """One item per line, keys sorted, so an unchanged item produces no diff."""
    keys = sorted(history["items"])
    lines = ['{"version": %d, "scan": %d, "items": {' % (HISTORY_VERSION, history["scan"])]
    for i, key in enumerate(keys):
        comma = "," if i < len(keys) - 1 else ""
        lines.append(json.dumps(key) + ": " + json.dumps(history["items"][key], separators=(",", ":")) + comma)
    lines.append("}}")
    path.write_text("\n".join(lines) + "\n")


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


def term_matches(term, text):
    """Word-boundary-aware match, not a naive substring check. A plain 'in'
    check for the term 'watch' incorrectly matches inside 'swatch' (Swarovski
    uses this for colour/fabric samples) - a real bug that put a crystal
    ornament in the watches category. \\b on the left only (not both sides)
    so 'watch' still correctly matches 'watch', 'watches', 'watchmaker' etc -
    just not when it's a suffix of a longer, unrelated word."""
    return re.search(r"\b" + re.escape(term), text) is not None


def is_excluded(product, exclude_terms):
    text = product_text(product)
    return any(term_matches(term, text) for term in exclude_terms)


def is_relevant(product, must_include_any):
    """A retailer's own category tag isn't reliable enough on its own - a shop
    configured as 'fragrance' may also sell makeup, skincare and haircare, and
    none of those are what this site claims to curate. If must_include_any is
    set, the product needs at least one genuine positive signal (e.g. 'eau de
    parfum') to qualify, rather than just having come from the right domain."""
    if not must_include_any:
        return True
    text = product_text(product)
    return any(term_matches(term, text) for term in must_include_any)


def affiliate_wrap(url, shop, awin_publisher_id=""):
    """Wraps a product URL in an Awin tracking link, but only when all three
    are true: the site has an Awin publisher ID (config awin_publisher_id),
    the shop has its Awin advertiser ID (awin_mid), and the shop is marked
    affiliate_live - i.e. this publisher has actually been APPROVED for that
    advertiser. Sending clicks through Awin before approval earns nothing, so
    until then the plain retailer link is used.

    Format: https://www.awin1.com/cread.php?awinmid=<advertiser>&awinaffid=<publisher>&ued=<destination>
    The destination is percent-encoded so its own ?query&params survive."""
    if (
        shop.get("affiliate_network") == "awin"
        and shop.get("affiliate_live")
        and shop.get("awin_mid")
        and awin_publisher_id
    ):
        return (
            "https://www.awin1.com/cread.php"
            f"?awinmid={quote(str(shop['awin_mid']), safe='')}"
            f"&awinaffid={quote(str(awin_publisher_id), safe='')}"
            f"&ued={quote(url, safe='')}"
        )
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


def build_cards(product, shop, exclude_terms, must_include_any, price_history, notes, notable_brands=None, notable_brand_facts=None, stats=None, awin_publisher_id=""):
    if is_excluded(product, exclude_terms):
        return []
    if not is_relevant(product, must_include_any):
        return []

    domain = shop["domain"]
    category = shop["category"]
    shop_name = shop["name"]

    available_prices = []
    on_sale_variants = []
    for variant in product.get("variants", []):
        # Sold-out variants stay in a shop's feed with their old markdown,
        # so without this check a card can list sizes that are actually
        # greyed out / "notify me". Only an explicit False counts as sold
        # out - if a shop's feed omits the field, everything still shows.
        if variant.get("available") is False:
            continue
        price = parse_price(variant.get("price"))
        if price is None or price <= 0:
            continue
        available_prices.append(price)
        compare_at = parse_price(variant.get("compare_at_price"))
        if compare_at is None or compare_at <= price:
            continue
        on_sale_variants.append((variant, price, compare_at))

    if not available_prices:
        return []

    product_id = product.get("id")
    item_id = f"{domain}-{product_id}"
    is_new = item_id not in price_history["items"]

    # Record EVERY in-scope product's price on every scan, on sale or not.
    # Previously history was only written while an item was already marked
    # down, so the tracker never saw full-price periods: "typical price" was
    # an average of sale prices, and a retailer's was-price could never be
    # checked against anything. Tracking full-price periods is what makes
    # "we watched it sell at the was-price" a claim the site can actually test.
    previous, record = update_history(price_history, item_id, min(available_prices))
    if stats is not None:
        stats["tracked"] += 1

    if not on_sale_variants:
        return []
    if stats is not None:
        stats["on_sale"] += 1

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

    # How far below its own typical price is it sitting today? Using the
    # average of everything we've observed, not just the single highest price
    # ever seen - a one-off peak isn't representative, but a running average
    # genuinely reflects what this item normally costs here.
    typical_price = None
    below_typical_pct = 0
    if has_history and observations > 0:
        typical_price = round(record.get("sum", price * observations) / observations, 2)
        if typical_price > 0:
            below_typical_pct = round((1 - price / typical_price) * 100)

    below_observed_high = 0
    if has_history and observed_high > 0:
        below_observed_high = round((1 - price / observed_high) * 100)

    # Has the tracker itself seen this item selling at (or above) the
    # retailer's claimed was-price? 50p tolerance for rounding. False means
    # the was-price is the retailer's word only - not proof it is invented,
    # since it may pre-date tracking, but not something the site has verified.
    was_price_seen = bool(has_history and observed_high >= compare_at - 0.5)

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
    score += min(below_typical_pct, 50) * 0.4       # below its own typical (average) price
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

    product_url = affiliate_wrap(f"https://{domain}/products/{product.get('handle')}", shop, awin_publisher_id)

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
        "was_price_seen": was_price_seen,
        "typical_price": typical_price,
        "below_typical_pct": below_typical_pct,
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
    price_history = load_history(PRICE_HISTORY_PATH)
    price_history["scan"] += 1
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
    awin_publisher_id = str(config.get("awin_publisher_id", "")).strip()
    # Shopify serves 250 products a page. Several shops have 2,500-8,300
    # products, and a 5-page cap was silently skipping 70-88% of their
    # markdowns. Pagination stops by itself at the last page, so a high cap
    # only costs time on the shops that need it.
    default_max_pages = config.get("default_max_pages", 60)

    feed = []
    errors = []
    shop_stats = []

    for shop in config["shop_watches"]:
        print(f"Scanning {shop['name']} ({shop['domain']})...")
        exclude_terms = global_exclude + category_exclude.get(shop["category"], [])
        must_include_any = category_must_include_any.get(shop["category"])
        stats = {"shop": shop["name"], "products": 0, "tracked": 0, "on_sale": 0, "in_feed": 0}
        shop_stats.append(stats)
        max_pages = shop.get("max_pages", default_max_pages)
        try:
            products = fetch_products(shop["domain"], max_pages=max_pages)
        except Exception as exc:
            print(f"  ! {shop['name']} failed: {exc}")
            errors.append({"shop": shop["name"], "error": str(exc)})
            continue
        stats["products"] = len(products)
        # fetch_products swallows page errors so one bad page doesn't lose the
        # rest; a shop that returns nothing at all (blocked, moved off Shopify,
        # password page) would otherwise vanish from the site without a trace.
        if not products:
            errors.append({"shop": shop["name"], "error": "no products returned"})
        elif len(products) >= max_pages * 250:
            errors.append({"shop": shop["name"], "error": f"catalogue truncated at {max_pages} pages - raise max_pages"})

        for product in products:
            feed.extend(build_cards(product, shop, exclude_terms, must_include_any, price_history, notes, notable_brands, notable_brand_facts, stats, awin_publisher_id))
        print(f"  {stats['products']} products, {stats['tracked']} in scope and tracked, {stats['on_sale']} marked down")

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

    # Dedupe by product family - same shop, same base product name (title
    # stripped of a trailing " - Colourway"). A retailer running a flat
    # clearance price across a dozen colours of the same item shouldn't fill
    # the feed with near-identical cards - genuine variety matters more than
    # showing every colourway of one clearance line. Keep only the single
    # best-scoring variant per family.
    def family_key(item):
        base_title = item["title"].split(" - ")[0].strip().lower()
        return (item["shop"], base_title)

    best_per_family = {}
    for item in feed:
        key = family_key(item)
        if key not in best_per_family or item["score"] > best_per_family[key]["score"]:
            best_per_family[key] = item
    feed = list(best_per_family.values())

    feed.sort(key=lambda item: item["score"], reverse=True)

    # Guarantee each SHOP a floor of slots first - not just each category.
    # A category floor alone doesn't stop one large-catalogue retailer (e.g.
    # Stuarts London) from filling every menswear slot on score alone, since
    # "menswear is represented" and "every menswear shop is represented" are
    # different things. Shop floor runs first; category floor then tops up
    # any category that's still short even after every shop got its floor
    # (e.g. if a whole category only has one shop with qualifying items).
    feed_size = config.get("feed_size", 60)
    shop_floor = config.get("shop_floor", 3)
    category_floor = config.get("category_floor", 6)
    # At most this many items from one brand. Family dedupe only catches
    # colourways of one product; a brand-wide clearance (nine different Lois
    # Jeans styles at the same price) would otherwise fill a third of the feed.
    brand_cap = config.get("brand_cap")
    selected = []
    selected_ids = set()
    brand_counts = {}

    def take(item):
        brand = (item.get("brand") or "").strip().lower()
        if brand_cap and brand and brand_counts.get(brand, 0) >= brand_cap:
            return False
        selected.append(item)
        selected_ids.add(item["id"])
        if brand:
            brand_counts[brand] = brand_counts.get(brand, 0) + 1
        return True

    by_shop = {}
    for item in feed:
        by_shop.setdefault(item["shop"], []).append(item)
    for shop_items in by_shop.values():
        taken = 0
        for item in shop_items:
            if taken >= shop_floor:
                break
            taken += take(item)

    by_category = {}
    for item in feed:
        if item["id"] not in selected_ids:
            by_category.setdefault(item["category"], []).append(item)
    for category_items in by_category.values():
        remaining_floor = max(0, category_floor - sum(
            1 for i in selected if i["category"] == category_items[0]["category"]
        ))
        for item in category_items:
            if remaining_floor <= 0:
                break
            remaining_floor -= take(item)
    for item in feed:
        if len(selected) >= feed_size:
            break
        if item["id"] not in selected_ids:
            take(item)
    selected.sort(key=lambda item: item["score"], reverse=True)
    feed = selected[:feed_size]
    for stats in shop_stats:
        stats["in_feed"] = sum(1 for item in feed if item["shop"] == stats["shop"])

    write_history(PRICE_HISTORY_PATH, price_history)
    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps({
        "generated_at": int(time.time()),
        "errors": errors,
        "shops": shop_stats,
        "feed": feed,
    }, indent=2))
    print(f"Done: {len(feed)} items in feed, {len(errors)} shop errors")


if __name__ == "__main__":
    main()
