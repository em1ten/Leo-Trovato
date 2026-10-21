'use strict';
// Proper Finds front end: loads data.json and renders the feed.
// Kept in its own file so the page can run under a Content-Security-Policy without 'unsafe-inline' scripts.

let allItems = [];
let activeCategory = 'all';
let sizeQuery = '';
let priceRange = null;
let sortOrder = 'recommended';

function fmtMoney(n) {
  return '£' + Number(n).toFixed(2).replace(/\.00$/, '');
}

function timeAgo(unixSeconds) {
  if (num(unixSeconds) === null) return '';
  const mins = Math.max(0, Math.round((Date.now() / 1000 - unixSeconds) / 60));
  if (mins < 60) return `Updated ${mins} min ago`;
  const hours = Math.round(mins / 60);
  return `Updated ${hours}h ago`;
}

function categoryLabel(cat) {
  return { menswear: 'Menswear', fragrance: 'Fragrance', watches: 'Watches' }[cat] || cat;
}

// Escapes text for use in element content AND quoted attribute values.
// (The previous textContent/innerHTML trick left quotes unescaped, so a quote
// in a feed URL could break out of src="..." and add an event handler.)
function escapeHtml(str) {
  return String(str == null ? '' : str).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

// Only http(s) URLs are allowed into href/src; anything else (javascript:, data:) is dropped.
function safeUrl(u) {
  try {
    const url = new URL(String(u), location.href);
    return (url.protocol === 'https:' || url.protocol === 'http:') ? url.href : null;
  } catch (e) {
    return null;
  }
}

function num(n) {
  const x = Number(n);
  return Number.isFinite(x) ? x : null;
}

// Product photos come from Shopify's CDN, which resizes on request via ?width=.
// Cards render ~340px wide at every breakpoint, so originals (often 2000px+,
// up to 3840x4800) were 6-10x larger than needed. 720 is the largest
// candidate on purpose: it gives ~2x density on 3x phones, which looks the
// same for product shots and saves ~20% bytes against a 900 candidate.
const IMG_WIDTHS = [360, 540, 720];
const IMG_SIZES = '(min-width: 1160px) 344px, (min-width: 1040px) calc(33.3vw - 43px), (min-width: 680px) calc(50vw - 44px), calc(100vw - 48px)';
const EAGER_IMAGES = 3; // first row at desktop width; covers the LCP image everywhere

function sizedPhoto(url, width) {
  const u = new URL(url);
  if (u.hostname !== 'cdn.shopify.com') return url;
  u.searchParams.set('width', width);
  return u.href;
}

function photoAttrs(url, eager, first) {
  const isShopify = new URL(url).hostname === 'cdn.shopify.com';
  const src = isShopify ? sizedPhoto(url, 720) : url;
  const srcset = isShopify ? IMG_WIDTHS.map(w => `${sizedPhoto(url, w)} ${w}w`).join(', ') : '';
  return `src="${escapeHtml(src)}"`
    + (srcset ? ` srcset="${escapeHtml(srcset)}" sizes="${IMG_SIZES}"` : '')
    + ` loading="${eager ? 'eager' : 'lazy'}" decoding="async"`
    + (first ? ' fetchpriority="high"' : '');
}

// An item is shown only if it has the fields a card cannot do without.
function isRenderable(item) {
  return item && typeof item === 'object' && safeUrl(item.url) && item.title && num(item.price_amount) !== null;
}

// headingTag: 'h3' under a section heading, 'h2' when the feed is a flat list,
// so the heading outline never skips a level.
function renderCard(item, position, headingTag) {
  const a = document.createElement('a');
  a.className = 'card';
  a.href = safeUrl(item.url);
  a.target = '_blank';
  a.rel = 'noopener noreferrer sponsored';

  const title = String(item.title);
  const brand = item.brand ? String(item.brand) : '';
  const titleStartsWithBrand = brand && title.toLowerCase().startsWith(brand.toLowerCase());
  const fullTitle = (brand && !titleStartsWithBrand) ? brand + ' ' + title : title;
  const discount = num(item.discount_pct);
  const typical = num(item.typical_price);
  const belowTypical = num(item.below_typical_pct);
  const observedLow = num(item.observed_low);
  const observations = num(item.observations);
  const compareAt = num(item.compare_at);
  const photo = safeUrl(item.photo);

  let sizesLine = '';
  if (Array.isArray(item.sizes) && item.sizes.length) {
    const shown = item.sizes.slice(0, 8);
    const extra = item.sizes.length - shown.length;
    sizesLine = shown.map(escapeHtml).join(' · ') + (extra > 0 ? ` +${extra}` : '');
  }

  const thumbInner = photo
    ? `<img class="thumb" ${photoAttrs(photo, position < EAGER_IMAGES, position === 0)} alt="" />`
    : '';

  a.innerHTML = `
    <div class="thumb-wrap">
      ${thumbInner}
      ${discount !== null ? `<span class="tag tabular${item.price_dropped ? ' dropped' : ''}">−${Math.round(discount)}%</span>` : ''}
    </div>
    <div class="card-body">
      <div class="meta-row">
        <span class="shop">${escapeHtml(categoryLabel(item.category))} · ${escapeHtml(item.shop)}</span>
        ${item.is_notable_brand ? '<span class="established">Established house</span>' : ''}
      </div>
      <${headingTag} class="title">${escapeHtml(fullTitle)}</${headingTag}>
      ${item.note ? `<p class="note">${escapeHtml(item.note)}</p>` : ''}
      ${sizesLine ? `<p class="sizes tabular">${sizesLine}</p>` : ''}
      ${item.at_observed_low ? '<span class="lowflag">Lowest we\'ve tracked</span>' : ''}
      ${!item.at_observed_low && item.has_history && belowTypical >= 10 && typical !== null ? `<p class="tracked tabular">${Math.round(belowTypical)}% below its typical price here (usually ${fmtMoney(typical)})</p>` : ''}
      ${item.has_history && observedLow && !item.at_observed_low ? `<p class="tracked tabular">Tracked low ${fmtMoney(observedLow)} · ${observations !== null ? Math.round(observations) : ''} checks</p>` : ''}
      <div class="price-row">
        <span class="tabular">${item.price_is_from ? '<span class="price-from">From</span>' : ''}<span class="price">${fmtMoney(item.price_amount)}</span>${compareAt ? `<span class="was">${fmtMoney(compareAt)}</span>` : ''}</span>
        <span class="link">View →</span>
      </div>
    </div>
  `;
  const img = a.querySelector('img.thumb');
  if (img) img.addEventListener('error', () => img.remove(), { once: true });
  return a;
}

function matchesFilters(item) {
  if (activeCategory !== 'all' && item.category !== activeCategory) return false;
  if (sizeQuery) {
    const q = sizeQuery.trim().toLowerCase();
    const hasSize = (Array.isArray(item.sizes) ? item.sizes : []).some(s => String(s).toLowerCase().includes(q));
    if (!hasSize) return false;
  }
  if (priceRange) {
    const [min, max] = priceRange;
    if (item.price_amount < min || item.price_amount > max) return false;
  }
  return true;
}

function render() {
  const feed = document.getElementById('feed');
  feed.innerHTML = '';
  let position = 0;
  let headingTag = 'h2';
  const addCard = item => feed.appendChild(renderCard(item, position++, headingTag));
  let items = allItems.filter(matchesFilters);
  if (sortOrder === 'discount') {
    items = items.slice().sort((a, b) => (num(b.discount_pct) || 0) - (num(a.discount_pct) || 0));
  }

  const filtersActive = sizeQuery || priceRange || sortOrder !== 'recommended';
  document.getElementById('filterClear').classList.toggle('visible', !!filtersActive);

  if (!items.length) {
    feed.innerHTML = '<p class="empty">Nothing matches right now — try widening the filters.</p>';
  } else if (activeCategory === 'all' && !filtersActive) {
    headingTag = 'h3';
    let rest = items;
    if (items.length >= 6) {
      const featured = items.slice(0, 4);
      const featuredIds = new Set(featured.map(i => i.id));
      rest = items.filter(i => !featuredIds.has(i.id));

      const featuredHead = document.createElement('h2');
      featuredHead.className = 'section-head';
      featuredHead.textContent = 'Best finds right now';
      feed.appendChild(featuredHead);
      featured.forEach(addCard);
    }

    const order = ['menswear', 'fragrance', 'watches'];
    const grouped = {};
    rest.forEach(i => { (grouped[i.category] = grouped[i.category] || []).push(i); });
    const cats = order.filter(c => grouped[c]).concat(
      Object.keys(grouped).filter(c => !order.includes(c))
    );
    cats.forEach(cat => {
      const head = document.createElement('h2');
      head.className = 'section-head';
      head.textContent = categoryLabel(cat);
      feed.appendChild(head);
      grouped[cat].forEach(addCard);
    });
  } else {
    items.forEach(addCard);
  }
  document.getElementById('count').textContent = `${items.length} item${items.length === 1 ? '' : 's'}`;
}

function activateTab(tab) {
  document.querySelectorAll('.tab').forEach(t => {
    t.classList.remove('active');
    t.setAttribute('aria-pressed', 'false');
  });
  tab.classList.add('active');
  tab.setAttribute('aria-pressed', 'true');
  activeCategory = tab.dataset.cat;
  render();
}

document.getElementById('pills').addEventListener('click', (e) => {
  const tab = e.target.closest('.tab');
  if (tab) activateTab(tab);
});

document.getElementById('sizeFilter').addEventListener('input', (e) => {
  sizeQuery = e.target.value;
  render();
});
document.getElementById('priceFilter').addEventListener('change', (e) => {
  const val = e.target.value;
  priceRange = val ? val.split('-').map(Number) : null;
  render();
});
document.getElementById('sortOrder').addEventListener('change', (e) => {
  sortOrder = e.target.value;
  render();
});
document.getElementById('filterClear').addEventListener('click', (e) => {
  // The button hides itself once filters are clear, so keep keyboard focus
  // somewhere sensible instead of dropping it to <body>.
  const hadFocus = document.activeElement === e.currentTarget;
  sizeQuery = '';
  priceRange = null;
  sortOrder = 'recommended';
  document.getElementById('sizeFilter').value = '';
  document.getElementById('priceFilter').value = '';
  document.getElementById('sortOrder').value = 'recommended';
  render();
  if (hadFocus) document.getElementById('sizeFilter').focus();
});

// No cache-busting query string: GitHub Pages serves data.json with
// max-age=600 and an ETag, so repeat visits within 10 minutes reuse it and
// later ones revalidate. The scan only runs every 3 hours. The URL must also
// match the <link rel="preload"> in index.html exactly.
fetch('data.json')
  .then(r => {
    if (!r.ok) throw new Error('data.json HTTP ' + r.status);
    return r.json();
  })
  .then(data => {
    allItems = (Array.isArray(data && data.feed) ? data.feed : []).filter(isRenderable);
    // A non-breaking space keeps the line's height, so nothing below it moves.
    document.getElementById('updated').textContent = timeAgo(data.generated_at) || '\u00a0';
    render();
  })
  .catch(() => {
    document.getElementById('feed').innerHTML = '<p class="empty">Feed not available yet — the first scan hasn\'t run.</p>';
  })
  .finally(() => {
    document.getElementById('feed').removeAttribute('aria-busy');
  });
