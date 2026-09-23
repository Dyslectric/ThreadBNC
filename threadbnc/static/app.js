// Page enhancements for every page. Everything works without JavaScript;
// this makes actions happen in place (with undo), loads more posts without
// leaving the page, and adds keyboard shortcuts.
document.documentElement.classList.add("js");

(function () {
  const FETCH = { "X-ThreadBNC-Fetch": "1" };
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const shown = (el) => !!el && el.offsetParent !== null;

  // ---- messages ------------------------------------------------------------
  function toast(m, onUndo) {
    const box = $("#toasts");
    if (!box) return;
    const t = document.createElement("div");
    t.className = "toast " + (m.kind || "info");
    t.setAttribute("role", m.kind === "error" ? "alert" : "status");
    const text = document.createElement("span");
    text.textContent = m.text;
    t.append(text);
    if (m.link) {
      const a = document.createElement("a");
      a.href = m.link.href;
      a.textContent = m.link.label;
      t.append(a);
    }
    if (m.undo && onUndo) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "link";
      b.textContent = "Undo";
      b.addEventListener("click", () => { t.remove(); onUndo(); });
      t.append(b);
    }
    const close = document.createElement("button");
    close.type = "button";
    close.className = "link toast-close";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "×";
    close.addEventListener("click", () => t.remove());
    t.append(close);
    box.append(t);
    let timer = setTimeout(() => t.remove(), m.undo || m.link ? 10000 : 5000);
    t.addEventListener("mouseenter", () => clearTimeout(timer));
    t.addEventListener("mouseleave", () => { timer = setTimeout(() => t.remove(), 4000); });
  }

  // ---- refreshing parts of the page ------------------------------------------
  // Parts are re-rendered by the server: after an action the page is fetched
  // again and the parts that changed are swapped in by id.
  async function fetchDoc(url) {
    const r = await fetch(url, { credentials: "same-origin" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return new DOMParser().parseFromString(await r.text(), "text/html");
  }

  // Which page an element came from: the current one, or a page "Load more" added.
  const sourceOf = (el) => (el && el.closest("[data-src]") ? el.closest("[data-src]").dataset.src : location.href);
  const currentOf = (id) => document.getElementById(id) || $(`[data-gone-for="${CSS.escape(id)}"]`);

  function swapIn(doc, id) {
    const now = currentOf(id);
    const fresh = doc.getElementById(id);
    if (!now || !fresh) return !!fresh;
    const src = now.dataset.src;
    if (src) fresh.dataset.src = src;
    if (now.tagName === "DETAILS" && now.open) fresh.open = true;
    const wasCurrent = now === current;
    now.replaceWith(document.adoptNode(fresh));
    if (wasCurrent) select(fresh, false);
    watchUnread(fresh);
    watchArticles(fresh);
    return true;
  }

  function swapLive(doc) {
    for (const el of $$("[data-live][id]")) {
      const fresh = doc.getElementById(el.id);
      if (!fresh) continue;
      if (el.tagName === "DETAILS" && el.open) fresh.open = true;
      el.replaceWith(document.adoptNode(fresh));
    }
  }

  // Refresh these ids (grouped by the page each came from) and the live parts
  // (header counts, sidebar). Ids missing from the fresh page get a placeholder.
  async function refresh(ids, gone, undo) {
    const bySrc = new Map();
    for (const id of ids) {
      const src = sourceOf(currentOf(id));
      if (!bySrc.has(src)) bySrc.set(src, []);
      bySrc.get(src).push(id);
    }
    if (!bySrc.has(location.href)) bySrc.set(location.href, []);
    for (const [src, list] of bySrc) {
      const doc = await fetchDoc(src);
      for (const id of list) {
        if (!swapIn(doc, id) && gone) placeholder(id, gone, undo);
      }
      if (src === location.href) swapLive(doc);
    }
  }

  function placeholder(id, label, undo) {
    const el = document.getElementById(id);
    if (!el) return;
    const ph = document.createElement(el.tagName === "LI" ? "li" : "div");
    ph.className = "gone" + (el.classList.contains("tile") ? " gone-tile" : "");
    ph.dataset.goneFor = id;
    if (el.dataset.src) ph.dataset.src = el.dataset.src;
    const text = document.createElement("span");
    text.textContent = label;
    ph.append(text);
    if (undo) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "link";
      b.textContent = "Undo";
      b.addEventListener("click", () => runUndo(undo, [id]));
      ph.append(b);
    }
    if (el === current) current = null;
    el.replaceWith(ph);
  }

  async function post(url, fields) {
    const body = fields instanceof URLSearchParams ? fields : new URLSearchParams();
    if (!(fields instanceof URLSearchParams)) {
      for (const [k, v] of Object.entries(fields || {})) {
        for (const one of Array.isArray(v) ? v : [v]) body.append(k, one);
      }
    }
    const r = await fetch(url, { method: "POST", body, headers: FETCH, credentials: "same-origin" });
    if (!(r.headers.get("content-type") || "").includes("json")) throw new Error("HTTP " + r.status);
    return r.json();
  }

  async function runUndo(undo, ids) {
    try {
      const data = await post(undo.action, undo.fields);
      await refresh(ids);
      for (const m of data.messages || []) toast(m);
    } catch (e) {
      toast({ kind: "error", text: "Couldn't undo: " + e.message });
    }
  }

  // ---- votes -----------------------------------------------------------------
  // A vote shows at once and is sent in the background. The page isn't fetched
  // again afterwards (on a big thread that's a lot to download and re-render),
  // unless the server says something went wrong: then it shows what's true.
  function showVote(box, mine, copies) {
    const up = $("button.vote.up", box), down = $("button.vote.down", box);
    const old = up.classList.contains("on") ? 1 : down.classList.contains("on") ? -1 : 0;
    for (const [btn, dir] of [[up, 1], [down, -1]]) {
      const on = mine === dir;
      const count = $("span", btn);
      const n = count.textContent.match(/-?\d+/);
      if (n) count.textContent = count.textContent.replace(n[0], String(+n[0] + copies * (on - (old === dir))));
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-pressed", String(on));
      btn.title = btn.dataset[on ? "on" : "off"];
      btn.setAttribute("aria-label", btn.title);
      $("input[name=score]", btn.form).value = on ? 0 : dir;
    }
    return old;
  }

  async function vote(form) {
    const box = form.closest(".obj-actions");
    if (box.dataset.busy) return;
    box.dataset.busy = "1";
    const fields = new URLSearchParams(new FormData(form));
    const copies = 1 + $$("input[name=also]", form).length;
    const before = showVote(box, +fields.get("score"), copies);
    try {
      const data = await post(form.action, fields);
      const dest = new URL(data.redirect || location.href, location.href);
      if (dest.pathname !== location.pathname) { location.assign(dest.href); return; }
      if (!data.ok) await refresh([box.id]);
      for (const m of data.messages || []) toast(m);
    } catch (e) {
      showVote(box, before, copies);
      toast({ kind: "error", text: "Your vote didn't go through (" + e.message + "). Try again." });
    } finally {
      delete box.dataset.busy;
    }
  }

  // ---- forms -----------------------------------------------------------------
  document.addEventListener("submit", async (ev) => {
    const form = ev.target;
    const submitter = ev.submitter;
    if (form.dataset.confirm && !form.dataset.confirmed) {
      ev.preventDefault();
      if (!window.confirm(form.dataset.confirm)) return;
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = "confirmed";
      input.value = "1";
      form.append(input);
      form.dataset.confirmed = "1";
      form.requestSubmit(submitter);
      return;
    }
    if (form.hasAttribute("data-theme-form") && submitter) {
      ev.preventDefault();
      setTheme(submitter.value);
      post(form.action, { theme: submitter.value }).catch(() => {});
      return;
    }
    if (form.hasAttribute("data-vote")) {
      ev.preventDefault();
      vote(form);
      return;
    }
    const ids = (form.dataset.inplace || "").split(/\s+/).filter(Boolean);
    if (!ids.length || form.method.toLowerCase() !== "post") return;
    ev.preventDefault();
    if (form.dataset.busy) return;
    form.dataset.busy = "1";
    const fields = new URLSearchParams(new FormData(form, submitter));
    if (submitter) submitter.disabled = true;
    try {
      const data = await post(form.action, fields);
      const dest = new URL(data.redirect || location.href, location.href);
      if (dest.pathname !== location.pathname) { location.assign(dest.href); return; }
      const msgs = data.messages || [];
      const undo = (msgs.find((m) => m.undo) || {}).undo;
      const gone = form.dataset.gone;
      await refresh(ids, data.ok ? gone : null, undo);
      const placed = gone && ids.some((id) => $(`[data-gone-for="${CSS.escape(id)}"]`));
      for (const m of msgs) {
        toast(placed ? { ...m, undo: null } : m, m.undo ? () => runUndo(m.undo, ids) : null);
      }
    } catch (e) {
      toast({ kind: "error", text: "That didn't work (" + e.message + "). Try again, or reload the page." });
    } finally {
      delete form.dataset.busy;
      if (submitter && submitter.isConnected) submitter.disabled = false;
    }
  });

  // "Back" links (the reader, opened from a link in another article): back where
  // you came from, when that's here; otherwise the link's own address.
  document.addEventListener("click", (ev) => {
    const link = ev.target.closest("a[data-back]");
    if (!link || history.length < 2 || !document.referrer.startsWith(location.origin)) return;
    ev.preventDefault();
    history.back();
  });

  // Panels that remember whether they're open (the Following list): the page
  // reads it back from the server, so it stays as you left it.
  document.addEventListener("toggle", (ev) => {
    const panel = ev.target;
    if (!(panel instanceof HTMLDetailsElement) || !panel.dataset.remember) return;
    post(panel.dataset.remember, { collapsed: panel.open ? "0" : "1" }).catch(() => {});
  }, true);

  // Filters that apply as soon as they change (search filters, inbox account).
  document.addEventListener("change", (ev) => {
    const form = ev.target.closest("form[data-autosubmit]");
    if (form && ev.target.matches("select, input[type=checkbox]")) form.requestSubmit();
  });

  function setTheme(value) {
    const root = document.documentElement;
    if (value === "light" || value === "dark") root.dataset.theme = value;
    else delete root.dataset.theme;
    for (const b of $$("[data-theme-form] button[name=theme]")) {
      const on = b.value === value || (b.value === "system" && !root.dataset.theme);
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    }
  }

  // ---- load more ---------------------------------------------------------------
  document.addEventListener("click", async (ev) => {
    const link = ev.target.closest("[data-load-more]");
    if (!link) return;
    const list = $("[data-items]");
    if (!list) return;
    ev.preventDefault();
    link.setAttribute("aria-busy", "true");
    link.textContent = "Loading…";
    try {
      const doc = await fetchDoc(link.href);
      const more = $("[data-items]", doc);
      const added = [];
      for (const item of more ? [...more.children] : []) {
        if (item.id && document.getElementById(item.id)) continue; // shifted onto this page since
        item.dataset.src = link.href;
        added.push(document.adoptNode(item));
      }
      list.append(...added);
      added.forEach((el) => { watchUnread(el); watchArticles(el); });
      const pager = link.closest(".pager");
      const next = $(".pager", doc);
      if (next) pager.replaceWith(document.adoptNode(next)); else pager.remove();
      const first = added.find((el) => el.matches("[data-entry]"));
      if (first && current) select(first);
    } catch (e) {
      location.assign(link.href);
    }
  });

  // ---- galleries: swipe, or step with the arrows (wrapping round) ---------------------
  function stepGallery(g, step) {
    const track = g.querySelector(".gallery-track");
    const n = track.children.length;
    const at = Math.round(track.scrollLeft / track.clientWidth);
    const to = (at + step + n) % n;
    const smooth = Math.abs(to - at) === 1 && !matchMedia("(prefers-reduced-motion: reduce)").matches;
    track.scrollTo({ left: to * track.clientWidth, behavior: smooth ? "smooth" : "instant" });
  }
  document.addEventListener("click", (ev) => {
    const nav = ev.target.closest(".gallery-nav");
    if (nav) stepGallery(nav.closest("[data-gallery]"), Number(nav.dataset.step));
  });
  document.addEventListener("scroll", (ev) => {
    const track = ev.target;
    if (!track.classList || !track.classList.contains("gallery-track")) return;
    const count = track.parentElement.querySelector("[data-at]");
    if (count) count.textContent = Math.round(track.scrollLeft / track.clientWidth) + 1;
  }, true);
  // In the pictures view a gallery takes the shape of its first picture, so paging doesn't resize it.
  function shapeGallery(img) {
    const g = img.closest(".picture-media [data-gallery]");
    if (!g || img.parentElement !== g.querySelector(".gallery-slide") || !img.naturalWidth) return;
    const ratio = Math.min(2.2, Math.max(0.6, img.naturalWidth / img.naturalHeight));
    g.style.setProperty("--ratio", ratio);
  }
  document.addEventListener("load", (ev) => { if (ev.target.tagName === "IMG") shapeGallery(ev.target); }, true);

  // ---- touch: reveal blurred tiles, show vote breakdowns ----------------------------
  const noHover = window.matchMedia("(hover: none)");
  document.addEventListener("click", (ev) => {
    const veil = ev.target.closest("[data-reveal]");
    if (veil) {
      const tile = veil.closest(".veiled");
      if (tile && !tile.classList.contains("revealed") && noHover.matches) {
        ev.preventDefault();
        tile.classList.add("revealed");
      }
    }
    const votes = ev.target.closest(".votes.has-breakdown");
    for (const open of $$(".votes.has-breakdown.open")) {
      if (open !== votes) { open.classList.remove("open"); open.setAttribute("aria-expanded", "false"); }
    }
    if (votes && !ev.target.closest(".vote-breakdown")) toggleVotes(votes);
    // Close open menus when clicking elsewhere.
    for (const menu of $$("details.menu[open]")) {
      if (!menu.contains(ev.target)) menu.open = false;
    }
  });

  function toggleVotes(v) {
    const open = !v.classList.contains("open");
    v.classList.toggle("open", open);
    v.setAttribute("aria-expanded", open ? "true" : "false");
  }

  // ---- marking posts read as they're scrolled past ---------------------------------
  const seenQueue = new Set();
  const wasVisible = new WeakSet();
  let seenTimer = null;
  const headerH = () => ($("header.top") && getComputedStyle($("header.top")).position === "sticky"
    ? $("header.top").offsetHeight : 0);
  let observer = null;

  function watchUnread(root) {
    if (!("IntersectionObserver" in window) || !$("[data-items][data-mark-on-scroll]")) return;
    // A post counts as scrolled past once it was on screen and then went above the header.
    observer = observer || new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting) { wasVisible.add(e.target); continue; }
        const top = e.rootBounds ? e.rootBounds.top : 0;
        if (wasVisible.has(e.target) && e.boundingClientRect.bottom <= top + 1) markSeen(e.target);
      }
    }, { rootMargin: `-${headerH()}px 0px 0px 0px` });
    const els = root.matches && root.matches("[data-ids].unread") ? [root] : $$("[data-ids].unread", root);
    for (const el of els) if (el.closest("[data-mark-on-scroll]")) observer.observe(el);
  }

  function markSeen(el) {
    observer.unobserve(el);
    if (!el.classList.contains("unread")) return;
    el.classList.remove("unread");
    el.classList.add("seen");
    for (const id of (el.dataset.ids || "").split(" ")) if (id) seenQueue.add(id);
    clearTimeout(seenTimer);
    seenTimer = setTimeout(flushSeen, 1500);
  }

  function seenBody() {
    const body = new URLSearchParams();
    for (const id of seenQueue) body.append("ids", id);
    seenQueue.clear();
    return body;
  }

  async function flushSeen() {
    if (!seenQueue.size) return;
    try {
      await post("/feed/seen", seenBody());
      const doc = await fetchDoc(location.href);
      swapLive(doc);
    } catch (e) { /* offline: they stay unread */ }
  }

  window.addEventListener("pagehide", () => {
    if (seenQueue.size && navigator.sendBeacon) navigator.sendBeacon("/feed/seen", seenBody());
  });

  // ---- linked articles' pictures, fetched as posts are scrolled to -------------------
  // A post whose linked article (or the pictures in it) isn't saved yet asks
  // the server for it once it's on screen, then checks back until that's done.
  // Pictures that arrive join the post's carousel while it's on screen, or
  // when it's next scrolled to, so nothing changes size out of sight.
  const asked = new Set();      // thread ids asked for on this page
  const waitingOn = new Set();  // of those, the ones not done yet
  const onScreen = new Set();   // entry element ids
  const unshown = new Set();    // entry element ids with pictures still to show
  let askQueue = [];
  let askTimer = null;
  let checkTimer = null;
  let checkUntil = 0;
  let articleObserver = null;

  function watchArticles(root) {
    if (!("IntersectionObserver" in window)) return;
    articleObserver = articleObserver || new IntersectionObserver((entries) => {
      for (const e of entries) {
        const id = e.target.id;
        if (!e.isIntersecting || !e.target.isConnected) { onScreen.delete(id); continue; }
        onScreen.add(id);
        const tid = id.slice(1);
        if (!asked.has(tid)) {
          asked.add(tid);
          askQueue.push(tid);
          clearTimeout(askTimer);
          askTimer = setTimeout(askArticles, 300);
        }
        if (unshown.has(id)) showPictures([id]);
      }
    });
    const els = root.matches && root.matches("[data-article-waiting]") ? [root] : $$("[data-article-waiting]", root);
    for (const el of els) articleObserver.observe(el);
  }

  async function askArticles() {
    const ids = askQueue;
    askQueue = [];
    try {
      await post("/feed/articles", { ids });
    } catch (e) {
      for (const id of ids) asked.delete(id);  // ask again when they're next on screen
      return;
    }
    for (const id of ids) waitingOn.add(id);
    checkUntil = Date.now() + 3 * 60000;  // then give up: the site may be refusing, or slow
    if (!checkTimer) checkTimer = setTimeout(checkArticles, 2500);
  }

  async function checkArticles() {
    checkTimer = null;
    if (!waitingOn.size) return;
    const ids = [...waitingOn];
    const show = [];
    try {
      const r = await fetch("/feed/articles?" + new URLSearchParams(ids.map((id) => ["ids", id])),
        { headers: FETCH, credentials: "same-origin" });
      const data = await r.json();
      const still = new Set(data.waiting.map(String));
      for (const tid of ids) {
        const el = document.getElementById("p" + tid);
        if (!still.has(tid)) waitingOn.delete(tid);
        if (!el) continue;
        if ((data.pics[tid] || 0) > Number(el.dataset.pics || 0)) {
          if (onScreen.has(el.id)) show.push(el.id); else unshown.add(el.id);
        } else if (!still.has(tid)) {
          articleObserver.unobserve(el);
        }
      }
    } catch (e) { /* try again next time */ }
    if (show.length) await showPictures(show);
    if (waitingOn.size && Date.now() < checkUntil) checkTimer = setTimeout(checkArticles, 2500);
  }

  // Re-render the posts, keeping each carousel at the picture it was showing.
  async function showPictures(ids) {
    const at = new Map();
    for (const id of ids) {
      unshown.delete(id);
      const track = $(`#${CSS.escape(id)} .gallery-track`);
      if (track) at.set(id, track.scrollLeft);
    }
    try { await refresh(ids); } catch (e) { return; }
    for (const [id, left] of at) {
      const track = $(`#${CSS.escape(id)} .gallery-track`);
      if (track && left) track.scrollTo({ left, behavior: "instant" });
    }
  }

  // ---- being here ---------------------------------------------------------------------
  // Subreddits are only checked while someone is using ThreadBNC: tell the
  // server when this tab is visible and used, at most once a minute. A tab
  // left alone (or hidden) stops saying so, and after half an hour the
  // checks stop until you're back.
  let lastBeat = 0;
  function beat() {
    if (document.visibilityState !== "visible") return;
    const now = Date.now();
    if (now - lastBeat < 60000) return;
    lastBeat = now;
    fetch("/presence", { method: "POST", headers: FETCH, credentials: "same-origin" }).catch(() => {});
  }
  for (const ev of ["pointerdown", "keydown", "scroll", "focus"]) {
    window.addEventListener(ev, beat, { passive: true, capture: true });
  }
  document.addEventListener("visibilitychange", beat);
  beat();

  // ---- diving into linked articles (the reader) --------------------------------------
  // A link to another article opens it beside the one you're reading on a wide
  // screen (a column each, two or three on screen, the newest scrolled to), or
  // inside it, under the paragraph with the link, on a narrow one. Opening one
  // from a column replaces the columns after it. Without this they're pages.
  const WIDE = matchMedia("(min-width: 1100px)");
  const deckOf = (el) => el.closest("[data-deck]");
  const cols = (deck) => [...deck.children];

  function paneUrl(href) {
    const u = new URL(href, location.href);
    u.searchParams.set("pane", "1");
    return u.href;
  }

  function opened(link, on) {
    link.classList.toggle("dived", on);
    link.setAttribute("aria-expanded", on ? "true" : "false");
  }

  // Column mode: each column scrolls by itself, so the one you were reading keeps its place.
  function setDiving(deck, on) {
    const first = deck.firstElementChild;
    if (on === deck.classList.contains("diving")) return;
    if (on) {
      const read = window.scrollY + headerH() - (first.getBoundingClientRect().top + window.scrollY);
      deck.classList.add("diving");
      window.scrollTo(0, deck.getBoundingClientRect().top + window.scrollY - headerH() - 12);
      first.scrollTop = Math.max(0, read);
    } else {
      const read = first.scrollTop;
      deck.classList.remove("diving");
      deck.style.removeProperty("--cols");
      window.scrollTo(0, first.getBoundingClientRect().top + window.scrollY - headerH() + read);
    }
  }

  function closeColumnsAfter(deck, col, reopening = false) {
    for (const c of cols(deck).slice(cols(deck).indexOf(col) + 1)) {
      if (c.opener) opened(c.opener, false);
      c.remove();
    }
    deck.style.setProperty("--cols", cols(deck).length);
    if (cols(deck).length === 1 && !reopening) setDiving(deck, false);
  }

  function closeDive(box) {
    if (box.opener) opened(box.opener, false);
    if (box.classList.contains("dive-col")) {
      const deck = deckOf(box);
      closeColumnsAfter(deck, box.previousElementSibling);
      if (box.opener && box.opener.isConnected) box.opener.focus({ preventScroll: true });
    } else {
      box.remove();
      if (box.opener && box.opener.isConnected) { box.opener.focus({ preventScroll: true }); reveal(box.opener); }
    }
  }

  // Where a narrow screen puts it: under the paragraph (or in the list item) the link is in.
  function placeInline(link, box) {
    const block = link.closest("p, li, dd, td, blockquote, figure, h2, h3, h4, h5, h6, pre") || link;
    block.insertAdjacentElement(block.matches("li, dd, td") ? "beforeend" : "afterend", box);
  }

  async function dive(link) {
    const deck = deckOf(link);
    const wide = WIDE.matches;
    const box = document.createElement(wide ? "div" : "section");
    box.className = wide ? "reader-col dive-col" : "dive-inline";
    box.opener = link;
    box.setAttribute("aria-busy", "true");
    box.innerHTML = '<p class="muted dive-loading">Reading…</p>';
    if (wide) {
      const col = link.closest(".reader-col");
      closeColumnsAfter(deck, col, true);
      for (const other of $$("a.dived", col)) opened(other, false);
      setDiving(deck, true);
      deck.append(box);
      deck.style.setProperty("--cols", cols(deck).length);
      deck.scrollTo({ left: deck.scrollWidth, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
    } else {
      placeInline(link, box);
    }
    opened(link, true);
    try {
      const r = await fetch(paneUrl(link.href), { credentials: "same-origin" });
      if (!r.ok || new URL(r.url).origin !== location.origin) throw new Error("HTTP " + r.status);
      const doc = new DOMParser().parseFromString(await r.text(), "text/html");
      const article = $("article.reader", doc);
      if (!article) throw new Error("not an article");
      if (!box.isConnected) return; // closed while it was being read
      box.replaceChildren(document.adoptNode(article));
      box.removeAttribute("aria-busy");
      if (wide) box.scrollTop = 0; else reveal(box);
    } catch (e) {
      // Not something to read here after all (the site itself, say): open it as a page.
      if (box.isConnected) closeDive(box);
      location.assign(link.href);
    }
  }

  document.addEventListener("click", (ev) => {
    const close = ev.target.closest("[data-dive-close]");
    if (close) {
      const box = close.closest(".dive-col, .dive-inline");
      if (box) closeDive(box);
      return;
    }
    const link = ev.target.closest("a.article-link, a[data-dive]");
    if (!link || !deckOf(link) || ev.button !== 0 || ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey) return;
    ev.preventDefault();
    if (link.classList.contains("dived")) {
      const box = [...$$(".dive-col, .dive-inline", deckOf(link))].find((b) => b.opener === link);
      if (box) { closeDive(box); return; }
    }
    dive(link);
  });

  // Crossing between wide and narrow: columns and inline articles don't carry over, so close them.
  WIDE.addEventListener("change", () => {
    for (const deck of $$("[data-deck]")) {
      for (const box of $$(".dive-inline", deck)) closeDive(box);
      if (deck.children.length > 1) closeColumnsAfter(deck, deck.firstElementChild);
    }
  });

  function closeLastDive() {
    const deck = $("[data-deck]");
    const boxes = deck ? $$(".dive-col, .dive-inline", deck) : [];
    if (!boxes.length) return false;
    closeDive(boxes[boxes.length - 1]);
    return true;
  }

  // ---- keyboard shortcuts ---------------------------------------------------------
  let current = null;
  let gPending = null;
  const GO = { f: "/", c: "/communities", k: "/kept", i: "/inbox", t: "/trash", s: "/search" };

  function items() {
    const entries = $$("[data-entry]").filter(shown);
    if (entries.length) return entries;
    return $$("#comments details.comment").filter(shown);
  }

  function select(el, scroll = true) {
    if (current) current.classList.remove("kb-current");
    current = el;
    if (!el) return;
    el.classList.add("kb-current");
    if (!scroll) return;
    reveal(el);
    // Lazy images can grow the item after we've scrolled; reveal again once they load.
    for (const img of el.querySelectorAll("img")) {
      if (!img.complete) img.addEventListener("load", () => { if (current === el) reveal(el); }, { once: true });
    }
  }

  // Scroll just enough to show the whole item (a comment without its replies), or its top if it can't fit.
  function reveal(el) {
    const box = el.getBoundingClientRect();
    let bottom = box.bottom;
    if (el.matches("details.comment")) {
      const replies = el.open && el.querySelector(":scope > .content > .replies");
      if (replies && replies.offsetParent) bottom = replies.getBoundingClientRect().top;
    }
    const covered = parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop) || 0;
    const room = window.innerHeight - 8;
    if (box.top < covered || bottom - box.top > room - covered) window.scrollBy(0, box.top - covered);
    else if (bottom > room) window.scrollBy(0, bottom - room);
  }

  function move(step) {
    const list = items();
    if (!list.length) return;
    let i = list.indexOf(current);
    if (i < 0) {
      // Start from what's on screen.
      i = list.findIndex((el) => el.getBoundingClientRect().bottom > headerH() + 8);
      i = step > 0 ? Math.max(0, i) : Math.max(0, i - 1);
      if (i === -1) i = 0;
      select(list[i]);
      return;
    }
    select(list[Math.min(list.length - 1, Math.max(0, i + step))]);
  }

  function press(sel) {
    const scope = current && current.isConnected ? current : document.getElementById("thread-actions");
    const btn = scope && scope.querySelector(sel);
    if (btn) btn.click();
    return !!btn;
  }

  function focusSearch() {
    const input = $$("[data-search]").find(shown);
    if (input) { input.focus(); input.select(); return; }
    const link = $(".search-link");
    if (link) location.assign(link.href);
  }

  document.addEventListener("keydown", (ev) => {
    if (ev.defaultPrevented || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const t = ev.target;
    if (t.closest && t.closest("input, textarea, select, [contenteditable]")) {
      if (ev.key === "Escape" && t.blur) t.blur();
      return;
    }
    if ($("dialog[open]")) return;
    if (gPending) {
      clearTimeout(gPending);
      gPending = null;
      if (GO[ev.key]) { ev.preventDefault(); location.assign(GO[ev.key]); }
      return;
    }
    switch (ev.key) {
      case "/": ev.preventDefault(); focusSearch(); break;
      case "?": ev.preventDefault(); openShortcuts(); break;
      case "g": gPending = setTimeout(() => { gPending = null; }, 1500); break;
      case "j": ev.preventDefault(); move(1); break;
      case "k": ev.preventDefault(); move(-1); break;
      case "o":
      case "Enter": {
        if (!current || !current.dataset.open || (ev.key === "Enter" && t !== document.body)) return;
        ev.preventDefault();
        location.assign(current.dataset.open);
        break;
      }
      case " ": {
        if (!current || !current.matches("details.comment") || t !== document.body) return;
        ev.preventDefault();
        current.open = !current.open;
        break;
      }
      case "h":
      case "l":
      case "ArrowLeft":
      case "ArrowRight": {
        const g = current && current.isConnected && current.querySelector("[data-gallery]");
        if (!g) return;
        ev.preventDefault();
        stepGallery(g, ev.key === "h" || ev.key === "ArrowLeft" ? -1 : 1);
        break;
      }
      case "s": if (press("[data-key=keep]")) ev.preventDefault(); break;
      case "d": if (press("[data-key=trash]")) ev.preventDefault(); break;
      case "m": if (press("[data-key=read]")) ev.preventDefault(); break;
      case "r": {
        const box = current && current.querySelector("details[data-reply]");
        if (!box) return;
        ev.preventDefault();
        box.open = true;
        const area = box.querySelector("textarea");
        if (area) area.focus();
        break;
      }
      case "n": {
        const next = $("[data-action=next-highlight]");
        if (next) { ev.preventDefault(); next.click(); }
        break;
      }
      case "c": {
        const compose = $("#compose");
        if (compose) { ev.preventDefault(); compose.focus(); }
        break;
      }
      case "Escape":
        if ($("details.menu[open]")) {
          for (const menu of $$("details.menu[open]")) menu.open = false;
        } else if (!current) {
          closeLastDive();
        }
        select(null);
        break;
    }
  });

  function openShortcuts() {
    const d = $("#shortcuts");
    if (d && d.showModal && !d.open) d.showModal();
  }

  document.addEventListener("click", (ev) => {
    if (ev.target.closest("[data-action=shortcuts]")) {
      for (const menu of $$("details.menu[open]")) menu.open = false;
      openShortcuts();
    }
  });

  // One menu open at a time.
  document.addEventListener("toggle", (ev) => {
    const d = ev.target;
    if (d.matches && d.matches("details.menu") && d.open) {
      for (const other of $$("details.menu[open]")) if (other !== d) other.open = false;
    }
  }, true);

  document.addEventListener("DOMContentLoaded", () => {
    watchUnread(document);
    watchArticles(document);
    if (location.hash === "#follow") {
      const input = $("#follow input[name=community]");
      if (input) input.focus();
    }
  });
})();
