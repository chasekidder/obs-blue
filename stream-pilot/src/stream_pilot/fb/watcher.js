// Comment watcher for the Facebook comments popout (template).
//
// Injected at document start by stream_pilot/fb/comments.py, which replaces the
// double-underscore tokens below from stream_pilot/fb/selectors.py (the only file
// that knows Facebook's markup). Every comment is sent through the Playwright
// binding window.__spComment(JSON.stringify({kind, items})):
//   hello  - once, at start (items absent)
//   rescan - the WHOLE visible list in document order (history; Python aligns it
//            against the end of the log, so overlap is skipped, never merged by text)
//   live   - newly appended comments only, in document order
// Items: {author, text, fb_comment_id, created_at}.
//
// Classification (Facebook renders with React *after* DOMContentLoaded and may
// insert older comments at the top, so "everything inserted later is live" would
// duplicate the history after every reload):
//   1. Initial rescan: wait until no new article appeared for SETTLE_MS (at most
//      SETTLE_MAX_MS after the first one), then send ONE rescan. No live batch is
//      ever sent before it. If no article shows up within SETTLE_MAX_MS an empty
//      rescan is sent ("alive") and the same settle logic applies to the first
//      articles that do appear (they are still sent as a rescan).
//   2. Later (debounced): new articles all after the last reported one -> live.
//      Any new article before an already reported one -> rescan of everything.
//      The same goes when no reported article is left in the DOM (the whole list was
//      replaced): a live batch would re-append history that Python can only align.
//      A node whose text is still empty is held back one extra debounce window
//      (it may still be rendering) and reported after that even if still empty.
//   3. A reported node removed from the DOM and replaced by an identical one in
//      the same window (React re-render) is not reported again.
// Nothing here may throw into the page.
(() => {
  'use strict';
  try {
    if (window.__spWatcherInstalled) return;
    if (window !== window.top) return;

    const ARTICLE_SELECTOR = __ARTICLE_SELECTOR__;
    const LABEL_ATTR = __LABEL_ATTR__;
    const LABEL_RE = __LABEL_RE__;
    const COMMENT_ID_RE = __COMMENT_ID_RE__;
    const AUTHOR_LINK_SELECTOR = __AUTHOR_LINK_SELECTOR__;
    const PERMALINK_SELECTOR = __PERMALINK_SELECTOR__;
    const TIME_SELECTOR = __TIME_SELECTOR__;
    const TIME_UTIME_ATTR = __TIME_UTIME_ATTR__;
    const TIME_DATETIME_ATTR = __TIME_DATETIME_ATTR__;
    const COMMENTS_PATH = __COMMENTS_PATH__;

    if (!(String(location.pathname).includes(COMMENTS_PATH) || window.__spForceWatch === true)) return;
    window.__spWatcherInstalled = true;

    const SETTLE_MS = 1000;
    const SETTLE_MAX_MS = 10000;
    const DEBOUNCE_MS = 250;
    const OUTBOX_MAX = 1000;
    const DRAIN_RETRY_MS = 100;
    const DRAIN_RETRY_MAX = 300;
    const SKIP_SELECTOR = PERMALINK_SELECTOR + ', ' + TIME_SELECTOR;
    const INLINE_TAGS = new Set([
      'a', 'abbr', 'b', 'bdi', 'bdo', 'cite', 'code', 'del', 'dfn', 'em', 'i', 'ins', 'kbd',
      'mark', 'q', 's', 'small', 'span', 'strong', 'sub', 'sup', 'time', 'u', 'var', 'wbr',
    ]);
    const IGNORED_TAGS = new Set(['script', 'style', 'noscript', 'template']);

    // ---- transport -------------------------------------------------------

    const outbox = [];
    let drainTimer = null;
    let drainRetries = 0;

    function drain() {
      try {
        if (!outbox.length) return;
        const fn = window.__spComment;
        if (typeof fn !== 'function') {
          // The binding may be installed a moment after the init script runs.
          if (!drainTimer && drainRetries < DRAIN_RETRY_MAX) {
            drainRetries += 1;
            drainTimer = setTimeout(() => { drainTimer = null; drain(); }, DRAIN_RETRY_MS);
          }
          return;
        }
        drainRetries = 0;
        while (outbox.length) {
          const msg = outbox.shift();
          try {
            const r = fn(msg);
            if (r && typeof r.catch === 'function') r.catch(() => {});
          } catch (e) { /* never throw into the page */ }
        }
      } catch (e) { /* ignore */ }
    }

    function send(kind, items) {
      try {
        const payload = { kind };
        if (items) payload.items = items;
        else payload.href = String(location.href);
        outbox.push(JSON.stringify(payload));
        while (outbox.length > OUTBOX_MAX) outbox.shift();
        drain();
      } catch (e) { /* ignore */ }
    }

    // ---- extraction ------------------------------------------------------

    function ownMatches(article, selector) {
      // Matches inside `article` but not inside a nested article (a reply).
      const all = article.querySelectorAll(selector);
      for (const el of all) {
        if (el.closest(ARTICLE_SELECTOR) === article) return el;
      }
      return null;
    }

    function textOf(root, skipEl) {
      const parts = [];
      const walk = (node) => {
        for (let c = node.firstChild; c; c = c.nextSibling) {
          if (c.nodeType === 3) { parts.push(c.nodeValue); continue; }
          if (c.nodeType !== 1) continue;
          if (c === skipEl || c.matches(ARTICLE_SELECTOR) || c.matches(SKIP_SELECTOR)) {
            parts.push(' ');
            continue;
          }
          const tag = c.tagName.toLowerCase();
          if (IGNORED_TAGS.has(tag)) continue;
          if (tag === 'img') {
            const alt = c.getAttribute('alt');
            if (alt) parts.push(alt);
            continue;
          }
          if (tag === 'br') { parts.push(' '); continue; }
          const inline = INLINE_TAGS.has(tag);
          if (!inline) parts.push(' ');
          walk(c);
          if (!inline) parts.push(' ');
        }
      };
      walk(root);
      return parts.join('').replace(/\s+/g, ' ').trim();
    }

    function collapse(s) {
      return String(s || '').replace(/\s+/g, ' ').trim();
    }

    function findAuthorLink(article, author) {
      const want = author ? author.toLowerCase() : null;
      for (const el of article.querySelectorAll(AUTHOR_LINK_SELECTOR)) {
        if (el.closest(ARTICLE_SELECTOR) !== article) continue;
        const t = collapse(el.textContent);
        if (!t) continue;
        if (want === null || t.toLowerCase() === want) return { el, text: t };
      }
      return null;
    }

    function createdAt(article) {
      const el = ownMatches(article, TIME_SELECTOR);
      if (!el) return null;
      const utime = el.getAttribute(TIME_UTIME_ATTR);
      if (utime && /^\d+(\.\d+)?$/.test(utime)) {
        const d = new Date(Number(utime) * 1000);
        if (!isNaN(d.getTime())) return d.toISOString();
      }
      const dt = el.getAttribute(TIME_DATETIME_ATTR);
      return dt ? dt : null;
    }

    // -> {author, text, fb_comment_id, created_at} or null while the node is still an empty shell
    function extract(article) {
      let author = '';
      let authorEl = null;
      const label = LABEL_RE.exec(collapse(article.getAttribute(LABEL_ATTR)));
      if (label && label.groups && label.groups.author) {
        author = collapse(label.groups.author);
        const link = findAuthorLink(article, author);
        if (link) authorEl = link.el;
      } else {
        const link = findAuthorLink(article, null);
        if (link) { author = link.text; authorEl = link.el; }
      }
      let text = textOf(article, authorEl);
      if (!authorEl && author && text.toLowerCase().startsWith(author.toLowerCase())) {
        text = text.slice(author.length).trim();
      }
      let id = null;
      const idLink = ownMatches(article, PERMALINK_SELECTOR);
      if (idLink) {
        const m = COMMENT_ID_RE.exec(idLink.getAttribute('href') || '');
        if (m) id = m[1];
      }
      if (!author && !text && !id) return null;
      return { author, text, fb_comment_id: id, created_at: createdAt(article) };
    }

    function signature(item) {
      return [item.author, item.text, item.fb_comment_id || ''].join('\u0000');
    }

    // ---- state -----------------------------------------------------------

    const reported = new WeakSet();   // article nodes already sent (rescan or live)
    const sigOf = new WeakMap();      // reported node -> signature
    const seen = new WeakSet();       // initial phase: nodes already counted as "growth"
    let removedNodes = [];            // nodes removed from the DOM in the current window
    let phase = 'initial';            // 'initial' until a rescan with items was sent
    let firstArticleAt = 0;
    let lastGrowthAt = 0;
    let emptySent = false;
    const emptySince = new WeakMap(); // later phase: node -> time it was first seen with empty text
    let deferred = false;             // a node was held back this pass: pump again soon
    let pumpTimer = null;
    let observer = null;

    function articles() {
      return Array.from(document.querySelectorAll(ARTICLE_SELECTOR));
    }

    // An item with no text yet may still be rendering: hold it back for one extra
    // debounce window, then accept it as it is (stickers/photos have no text).
    function settled(node, item) {
      if (item.text) return true;
      const now = Date.now();
      const since = emptySince.get(node);
      if (since === undefined) { emptySince.set(node, now); deferred = true; return false; }
      if (now - since >= DEBOUNCE_MS) return true;
      deferred = true;
      return false;
    }

    function sendRescan(all, deferEmpty) {
      const items = [];
      for (const node of all) {
        const item = extract(node);
        if (!item) continue;
        if (deferEmpty && !reported.has(node) && !settled(node, item)) continue;
        reported.add(node);
        sigOf.set(node, signature(item));
        items.push(item);
      }
      send('rescan', items);
      return items.length;
    }

    function schedulePump(ms) {
      if (pumpTimer !== null) return;
      pumpTimer = setTimeout(() => { pumpTimer = null; pump(); }, ms);
    }

    function pump() {
      try {
        if (phase === 'initial') {
          initialStep();
        } else {
          deferred = false;
          try { laterStep(); } finally { if (deferred) schedulePump(DEBOUNCE_MS + 5); }
        }
      } catch (e) { /* ignore */ }
    }

    function initialStep() {
      const now = Date.now();
      const all = articles();
      let grew = false;
      for (const n of all) {
        if (!seen.has(n)) { seen.add(n); grew = true; }
      }
      if (!all.length) return;
      if (!firstArticleAt) { firstArticleAt = now; lastGrowthAt = now; }
      else if (grew) lastGrowthAt = now;
      const due = Math.min(lastGrowthAt + SETTLE_MS, firstArticleAt + SETTLE_MAX_MS);
      if (now < due) { schedulePump(due - now + 5); return; }
      if (sendRescan(all) > 0) {
        phase = 'later';
        removedNodes = [];
      } else {
        // only empty shells so far: keep waiting for them to fill in
        firstArticleAt = now;
        lastGrowthAt = now;
        schedulePump(SETTLE_MS);
      }
    }

    function laterStep() {
      const all = articles();
      let lastIdx = -1;
      all.forEach((n, i) => { if (reported.has(n)) lastIdx = i; });

      // signatures of reported nodes that were really removed (not just moved)
      const removedSigs = new Map();
      for (const n of removedNodes) {
        if (n.isConnected) continue;
        const sig = sigOf.get(n);
        if (sig !== undefined) removedSigs.set(sig, (removedSigs.get(sig) || 0) + 1);
      }
      removedNodes = [];

      const fresh = [];
      all.forEach((node, idx) => {
        if (reported.has(node)) return;
        const item = extract(node);
        if (!item) return; // empty shell: picked up when it fills in
        const sig = signature(item);
        const left = removedSigs.get(sig) || 0;
        if (left > 0) {            // identical re-render of a removed, already reported node
          removedSigs.set(sig, left - 1);
          reported.add(node);
          sigOf.set(node, sig);
          return;
        }
        if (!settled(node, item)) return; // text may still be rendering
        fresh.push({ node, idx, item, sig });
      });
      if (!fresh.length) return;

      if (lastIdx === -1 || fresh.some((f) => f.idx < lastIdx)) {
        // older comments loaded, or the list was re-rendered (possibly wholesale):
        // let the Python alignment skip the overlap
        sendRescan(all, true);
        return;
      }
      for (const f of fresh) { reported.add(f.node); sigOf.set(f.node, f.sig); }
      send('live', fresh.map((f) => f.item));
    }

    function noteRemoved(records) {
      for (const rec of records) {
        for (const n of rec.removedNodes) {
          if (n.nodeType !== 1) continue;
          if (n.matches(ARTICLE_SELECTOR)) removedNodes.push(n);
          for (const a of n.querySelectorAll(ARTICLE_SELECTOR)) removedNodes.push(a);
        }
      }
    }

    function onMutations(records) {
      try {
        if (phase !== 'initial') noteRemoved(records);
        schedulePump(DEBOUNCE_MS);
      } catch (e) { /* ignore */ }
    }

    function start() {
      try {
        observer = new MutationObserver(onMutations);
        observer.observe(document, { childList: true, subtree: true });
        setTimeout(() => {
          // alive signal: no article within SETTLE_MAX_MS
          if (phase === 'initial' && !firstArticleAt && !emptySent) {
            emptySent = true;
            send('rescan', []);
          }
        }, SETTLE_MAX_MS);
        pump();
        schedulePump(DEBOUNCE_MS);
        window.addEventListener('pagehide', () => {
          // best effort: don't hold back the last debounce window across a reload
          if (phase !== 'initial') pump();
        });
      } catch (e) { /* ignore */ }
    }

    send('hello', null);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
    else start();
  } catch (e) { /* never throw into the page */ }
})();
