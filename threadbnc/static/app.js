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
    document.adoptNode(fresh);
    keepPlaying(now, fresh);
    const wasCurrent = now === current;
    now.replaceWith(fresh);
    if (wasCurrent) select(fresh, false);
    watchUnread(fresh);
    watchArticles(fresh);
    showRate(fresh);
    return true;
  }

  // A post's audio that's playing (or part played) carries on in its re-rendered
  // self: the same player is moved across, in the same task, so it isn't paused.
  function keepPlaying(now, fresh) {
    for (const old of $$("audio", now)) {
      if (old.paused && !old.currentTime) continue;
      const slot = [...$$("audio", fresh)].find((a) => a.getAttribute("src") === old.getAttribute("src"));
      if (slot) slot.replaceWith(old);
    }
  }

  // The live parts (header counts, sidebar) are updated in place, not swapped:
  // see morph.
  function swapLive(doc) {
    for (const el of $$("[data-live][id]")) {
      const fresh = doc.getElementById(el.id);
      if (fresh) morph(el, fresh);
    }
  }

  // Bring an element up to date with its fresh copy in place: what's still
  // there stays the same element, so a list you're scrolling (the sidebar) keeps
  // its place, even mid-fling, a panel you opened or closed stays that way, and
  // what you're typing, and focus, stay put.
  function morph(now, fresh) {
    if (now.nodeType !== Node.ELEMENT_NODE) {
      if (now.nodeValue !== fresh.nodeValue) now.nodeValue = fresh.nodeValue;
      return;
    }
    const yours = (name) => name === "open" && now.tagName === "DETAILS";
    for (const { name } of [...now.attributes]) {
      if (!fresh.hasAttribute(name) && !yours(name)) now.removeAttribute(name);
    }
    for (const { name, value } of fresh.attributes) {
      if (!yours(name) && now.getAttribute(name) !== value) now.setAttribute(name, value);
    }
    const olds = [...now.childNodes], news = [...fresh.childNodes];
    news.forEach((node, i) => {
      const old = olds[i];
      if (!old) now.append(document.adoptNode(node));
      else if (old.nodeName === node.nodeName && (old.id || "") === (node.id || "")) morph(old, node);
      else old.replaceWith(document.adoptNode(node));
    });
    for (const old of olds.slice(news.length)) old.remove();
  }

  // ---- keeping what you're reading where it is ------------------------------------
  // Things that arrive while you read (pictures, previews, comments,
  // discussions) are swapped in as fresh copies. One that grows or shrinks
  // above what you're reading would push it about, and the browser's own scroll
  // anchoring doesn't help when the element it was holding still is the one
  // swapped out. So those swaps go through steady(): it notes where the first
  // thing with an id at the top of the view is, makes the change, and scrolls
  // by however far that moved (fresh copies have the same ids). Only the swap
  // goes in `change`, not the fetch before it, or scrolling while it loaded
  // would be undone.
  function steady(change) {
    const was = topInView();
    change();
    const el = was && document.getElementById(was.id);
    if (!el) return;
    const moved = el.getBoundingClientRect().top - was.top;
    if (moved) scrollerOf(el).scrollBy(0, moved);
  }
  window.threadbnc = { steady };  // for thread.js, whose fresh comments arrive the same way

  // The first element with an id whose top is in view (below the sticky
  // header), or failing that the innermost one running off the top.
  function topInView() {
    const covered = parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop) || 0;
    let across = null;
    for (const el of $$("main [id]")) {
      const box = el.getBoundingClientRect();
      if (!box.height || box.bottom <= covered) continue;
      if (box.top >= innerHeight) break;
      if (el.closest("[data-live]") || getComputedStyle(el).position === "sticky") continue;
      if (box.top >= covered) return { id: el.id, top: box.top };
      across = { id: el.id, top: box.top };
    }
    return across;
  }

  // What scrolls it: a reader column, or the page.
  function scrollerOf(el) {
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      if (/auto|scroll/.test(getComputedStyle(p).overflowY) && p.scrollHeight > p.clientHeight) return p;
    }
    return window;
  }

  // Refresh these ids (grouped by the page each came from) and the live parts
  // (header counts, sidebar). Ids missing from the fresh page get a placeholder.
  // `arrived`: they came in the background, not from something you did, so
  // what you're reading is held still.
  async function refresh(ids, gone, undo, arrived = false) {
    const bySrc = new Map();
    for (const id of ids) {
      const src = sourceOf(currentOf(id));
      if (!bySrc.has(src)) bySrc.set(src, []);
      bySrc.get(src).push(id);
    }
    if (!bySrc.has(location.href)) bySrc.set(location.href, []);
    for (const [src, list] of bySrc) {
      const doc = await fetchDoc(src);
      const swap = () => {
        for (const id of list) {
          if (!swapIn(doc, id) && gone) placeholder(id, gone, undo);
        }
        if (src === location.href) swapLive(doc);
      };
      if (arrived) steady(swap); else swap();
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
    // Bluesky posts have a like button only: no down button.
    const up = $("button.vote.up", box), down = $("button.vote.down", box);
    const old = up.classList.contains("on") ? 1 : down && down.classList.contains("on") ? -1 : 0;
    const redditScore = $(".reddit-score", box);
    if (redditScore) {
      const n = redditScore.textContent.match(/-?\d+/);
      if (n) redditScore.textContent = redditScore.textContent.replace(
        n[0], String(+n[0] + copies * (mine - old)));
    }
    for (const [btn, dir] of [[up, 1], [down, -1]].filter(([b]) => b)) {
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
      // Sent again once this event is over: a form asked to submit while its
      // submit event is still being handled isn't submitted at all.
      setTimeout(() => form.requestSubmit(submitter && submitter.isConnected ? submitter : null));
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
    if (form.hasAttribute("data-trend-act")) {
      ev.preventDefault();
      trendAct(form);
      return;
    }
    if (form.hasAttribute("data-hide")) {
      ev.preventDefault();
      hideSource(form);
      return;
    }
    // Commenting, replying or deleting in comments opened under a post: done
    // here, and the comments shown again, rather than leaving for the post's page.
    const panelComments = form.closest(".inline-panel .comments");
    if (panelComments && !form.dataset.inplace && form.method.toLowerCase() === "post") {
      ev.preventDefault();
      submitInPanel(form, submitter);
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
      // Mark all read: the list is now as of then, so what it marked goes.
      const at = dest.searchParams.get("at");
      if (at && at !== new URL(location.href).searchParams.get("at")) {
        const here = new URL(location.href);
        here.searchParams.set("at", at);
        history.replaceState(history.state, "", here.href);
      }
      const msgs = data.messages || [];
      const undo = (msgs.find((m) => m.undo) || {}).undo;
      const gone = form.dataset.gone;
      await refresh(ids, data.ok ? gone : null, undo);
      if (form.dataset.play) awaitEpisode(form.dataset.play);
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

  // Post forms that can go to several communities, and on your Mastodon and
  // Bluesky accounts: the button says where.
  function countPosts(form) {
    const button = $("button:not([type=button])", form);
    if (!button) return;
    button.dataset.label ??= button.textContent;
    const label = button.dataset.label;
    const typed = ($("input[name=community]", form)?.value || "").split(/[,\s]+/).filter(Boolean).length;
    const n = $$("input[name=community_id]:checked", form).length + typed + (form.action.endsWith("/submit") ? 1 : 0);
    const on = $$("input[name=account_id]:checked", form).map((box) => box.dataset.kind);
    if (!on.length) {
      button.textContent = n > 1 ? `${label} in ${n} communities` : label;
      return;
    }
    const places = [...(n ? [n === 1 ? "1 community" : `${n} communities`] : []), ...on];
    const listed = places.length > 1 ? `${places.slice(0, -1).join(", ")} and ${places.at(-1)}` : places[0];
    button.textContent = `${label} to ${listed}`;
  }
  document.addEventListener("input", (ev) => {
    const form = ev.target.closest("form[data-post-count]");
    if (form) countPosts(form);
  });
  $$("form[data-post-count]").forEach(countPosts);

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
    const g = img.closest(".picture-media [data-gallery], .post-gallery [data-gallery]");
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
      steady(() => swapLive(doc));
    } catch (e) { /* offline: they stay unread */ }
  }

  window.addEventListener("pagehide", () => {
    if (seenQueue.size && navigator.sendBeacon) navigator.sendBeacon("/feed/seen", seenBody());
  });

  // ---- going back to a feed shows the same posts -----------------------------------
  // A feed's list is as of the time it was made (data-as-of). That goes into the
  // address, so Back asks for the same list: posts read since (scrolled past or
  // opened) stay put, and ones that arrived since wait. Reloading, or the Feed
  // link, starts afresh. The server sees a reload coming (web.py: feed_as_of); if
  // it didn't, or the address came from outside (a bookmark), load it afresh here.
  function keepFeedAsOf() {
    const list = $("[data-items][data-as-of]");
    if (!list) return;
    const url = new URL(location.href);
    const nav = ((performance.getEntriesByType && performance.getEntriesByType("navigation")[0]) || {}).type;
    const fromHere = document.referrer.startsWith(location.origin);
    if (list.hasAttribute("data-snapshot") && (nav === "reload" || (nav === "navigate" && !fromHere))) {
      url.searchParams.delete("at");
      location.replace(url.href);
      return;
    }
    if (url.searchParams.get("at") === list.dataset.asOf) return;
    url.searchParams.set("at", list.dataset.asOf);
    history.replaceState(history.state, "", url.href);
  }

  // ---- linked articles' pictures, audio and previews, fetched as posts are scrolled to --
  // A post whose linked article (or the pictures in it) isn't saved yet, whose
  // linked audio file isn't downloaded, or (a subreddit post or YouTube video)
  // whose text, pictures and votes are due to be read, asks the server for it
  // once it's on screen, then checks back until that's done. Pictures that
  // arrive join the post's carousel, a downloaded audio file becomes its
  // player, and text and votes read show in it, while it's on screen or when
  // it's next scrolled to, so nothing changes size out of sight.
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
    const waits = "[data-article-waiting], [data-audio], [data-preview]";
    const els = root.matches && root.matches(waits) ? [root] : $$(waits, root);
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
        const morePics = "pics" in el.dataset && (data.pics[tid] || 0) > Number(el.dataset.pics);
        const audio = (data.audio || {})[tid];
        const previewed = (data.previewed || {})[tid];
        const read = "preview" in el.dataset && previewed !== undefined && previewed !== el.dataset.preview;
        if (morePics || read || (el.dataset.audio && audio && audio !== el.dataset.audio)) {
          if (onScreen.has(el.id) || playWhenReady.has(tid)) show.push(el.id); else unshown.add(el.id);
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
    try { await refresh(ids, null, null, true); } catch (e) { return; }
    for (const [id, left] of at) {
      const track = $(`#${CSS.escape(id)} .gallery-track`);
      if (track && left) track.scrollTo({ left, behavior: "instant" });
    }
    for (const id of ids) {
      const player = $(`#${CSS.escape(id)} audio[data-listen]`);
      if (player && playWhenReady.delete(id.slice(1))) player.play().catch(() => {});  // the browser may want a tap
    }
  }

  // ---- podcast episodes and other audio: carrying on where you left off ------------
  // Play on an episode that isn't saved yet downloads it (POST /t/{id}/play);
  // its bar is checked on like a post scrolled to, and plays once it's there.
  // A player's position is saved as it plays, when it's paused and when you
  // leave the page, and it starts from there next time (data-at).
  const playWhenReady = new Set();  // thread ids

  function awaitEpisode(tid) {
    const player = $(`#p${CSS.escape(tid)} audio[data-listen]`);
    if (player) { player.play().catch(() => {}); return; }  // it was quicker than the page
    playWhenReady.add(tid);
    asked.add(tid);
    waitingOn.add(tid);
    checkUntil = Math.max(checkUntil, Date.now() + 30 * 60000);  // an hour of audio can take a while
    if (!checkTimer) checkTimer = setTimeout(checkArticles, 2500);
  }

  const savedAt = new WeakMap();  // player: when its position was last sent

  function listenBody(a, finished) {
    const body = new URLSearchParams({ position: String(Math.floor(finished ? a.duration || 0 : a.currentTime)) });
    if (Number.isFinite(a.duration)) body.set("duration", String(Math.round(a.duration)));
    if (finished) body.set("finished", "1");
    return body;
  }

  function saveListening(a, finished = false, beacon = false) {
    if (!a.dataset.listen || (!a.currentTime && !finished)) return;
    savedAt.set(a, Date.now());
    const url = `/media/${a.dataset.listen}/position`;
    if (beacon && navigator.sendBeacon) navigator.sendBeacon(url, listenBody(a, finished));
    else post(url, listenBody(a, finished)).catch(() => {});
  }

  // Media events don't bubble, so these listen in the capture phase.
  document.addEventListener("loadedmetadata", (ev) => {
    const a = ev.target;
    if (!(a instanceof HTMLAudioElement) || !a.dataset.at || a.dataset.resumed) return;
    a.dataset.resumed = "1";
    const at = Number(a.dataset.at);
    if (at > 0 && (!Number.isFinite(a.duration) || at < a.duration - 5)) a.currentTime = at;
  }, true);
  document.addEventListener("timeupdate", (ev) => {
    const a = ev.target;
    if (a instanceof HTMLAudioElement && !a.paused && Date.now() - (savedAt.get(a) || 0) > 15000) saveListening(a);
  }, true);
  for (const type of ["pause", "seeked"]) {
    document.addEventListener(type, (ev) => {
      const a = ev.target;
      if (a instanceof HTMLAudioElement && !a.ended) saveListening(a);
    }, true);
  }
  document.addEventListener("ended", (ev) => {
    if (ev.target instanceof HTMLAudioElement) saveListening(ev.target, true);
  }, true);
  window.addEventListener("pagehide", () => {
    for (const a of $$("audio[data-listen]")) if (!a.paused) saveListening(a, false, true);
  });

  // Skipping back 15 seconds or on 30, and the speed every player plays at
  // (saved on the server, so it's the same on your other devices).
  const RATES = ["1", "1.25", "1.5", "1.75", "2", "0.75"];  // as web.py's AUDIO_RATES
  let audioRate = null;

  function currentRate() {
    if (audioRate === null) {
      const b = $(".audio-rate");
      audioRate = b ? b.dataset.rate : "1";
    }
    return audioRate;
  }

  function applyRate(a) {
    a.defaultPlaybackRate = a.playbackRate = Number(currentRate());
  }

  function showRate(root = document) {
    const rate = currentRate();
    for (const b of $$(".audio-rate", root)) {
      b.dataset.rate = rate;
      b.textContent = rate + "×";
      b.title = `Playback speed: ${rate}×. Change it for every player`;
      b.setAttribute("aria-label", `Playback speed ${rate}×`);
    }
  }

  for (const type of ["loadedmetadata", "play"]) {
    document.addEventListener(type, (ev) => {
      if (ev.target instanceof HTMLAudioElement && ev.target.dataset.listen) applyRate(ev.target);
    }, true);
  }

  document.addEventListener("click", (ev) => {
    const button = ev.target.closest(".audio-controls button");
    const a = button && button.closest(".audio-bar") && $("audio[data-listen]", button.closest(".audio-bar"));
    if (!a) return;
    if (button.dataset.skip) {
      const to = a.currentTime + Number(button.dataset.skip);
      a.currentTime = Math.max(0, Number.isFinite(a.duration) ? Math.min(to, a.duration - 1) : to);
    } else if (button.classList.contains("audio-rate")) {
      audioRate = RATES[(RATES.indexOf(currentRate()) + 1) % RATES.length];
      for (const player of $$("audio[data-listen]")) applyRate(player);
      showRate();
      post("/audio/rate", { rate: audioRate }).catch(() => {});
    }
  });

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

  // ---- articles and comments expanded beneath a post -------------------------------
  // The links remain ordinary links without JavaScript.  With it, each opens a
  // reader panel in the post's vertical place; the coloured rail collapses it.
  function inlineThreadId(link) {
    const m = new URL(link.href, location.href).pathname.match(/^\/t\/(\d+)/);
    return m && m[1];
  }

  function inlineOwner(link) {
    const tid = inlineThreadId(link);
    return link.closest("[data-entry], article.post") || (tid && document.getElementById("p" + tid)) || $("article.post");
  }

  function inlineKey(kind, link) {
    const url = new URL(link.href, location.href);
    return `${kind}-${inlineThreadId(link) || url.pathname + url.search}`;  // (/read?url=... is a page per link)
  }

  const inlineSelector = (kind) => ({ article: "a.read-article", comments: "a.inline-comments", post: "a.read-post" })[kind];

  function inlinePanelOwner(panel) {
    return panel.dataset.owner && document.getElementById(panel.dataset.owner);
  }

  function panelsForOwner(owner) {
    if (!owner) return [];
    const scope = owner.matches(".tile, article.post") ? owner.parentElement : owner;
    return $$(".inline-panel", scope).filter((p) => p.dataset.owner === (owner.id || "thread"));
  }

  // The post's text (or video), then the article, then the comments. Panels
  // already in order aren't moved, so a video playing in one carries on.
  const PANEL_ORDER = ["post", "article", "comments"];
  function orderInlinePanels(owner) {
    const panels = panelsForOwner(owner);
    for (const [index, panel] of panels.entries()) {
      const next = panels[index + 1];
      if (!next || next.parentElement !== panel.parentElement) continue;
      if (PANEL_ORDER.indexOf(next.dataset.kind) < PANEL_ORDER.indexOf(panel.dataset.kind)) {
        panel.before(next);
        orderInlinePanels(owner);
        return;
      }
    }
  }

  // Panels shown one after another join up into one box. Returns the ones shown.
  function joinPanels(allPanels) {
    const panels = allPanels.filter((p) => !p.hidden);
    for (const panel of allPanels) panel.classList.remove("panel-continuation", "panel-followed");
    for (const [index, panel] of panels.entries()) {
      panel.classList.toggle("panel-continuation", index > 0);
      panel.classList.toggle("panel-followed", index < panels.length - 1);
    }
    return panels;
  }

  function updateInlinePanels(owner) {
    if (!owner) return;
    orderInlinePanels(owner);
    const panels = joinPanels(panelsForOwner(owner));
    owner.classList.toggle("panel-owner-open", panels.length > 0);
    for (const [index, panel] of panels.entries()) {
      if (!owner.matches(".tile")) continue;
      const ownerBox = owner.getBoundingClientRect();
      const panelBox = panel.getBoundingClientRect();
      const spread = $(".tile-panel-spread", panel);
      if (spread && index === 0) {
        const width = panelBox.width;
        const left = Math.max(0, ownerBox.left - panelBox.left);
        const right = Math.min(width, ownerBox.right - panelBox.left);
        const height = Math.max(14, panelBox.top - ownerBox.bottom + 1);
        panel.style.setProperty("--spread-depth", `${height}px`);
        spread.setAttribute("viewBox", `0 0 ${width} ${height}`);
        const pointer = (left + right) / 2;
        const pointerHalf = Math.min(22, Math.max(14, (right - left) * .09));
        const shelfTop = Math.max(9, height * .48);
        const shape = `M 0 ${shelfTop} H ${pointer - pointerHalf} L ${pointer} 0 L ${pointer + pointerHalf} ${shelfTop} H ${width} V ${height} H 0 Z`;
        const edges = `M 0 ${shelfTop} H ${pointer - pointerHalf} L ${pointer} 0 L ${pointer + pointerHalf} ${shelfTop} H ${width}`;
        $(".tile-panel-spread-fill", spread).setAttribute("d", shape);
        $(".tile-panel-spread-edges", spread).setAttribute("d", edges);
      }
    }
  }

  addEventListener("resize", () => $$(".tile.panel-owner-open").forEach(updateInlinePanels), { passive: true });

  function closeOtherTileInRow(owner) {
    if (!owner || !owner.matches(".tile")) return;
    const rowTop = owner.offsetTop;
    const otherTiles = [...owner.parentElement.children].filter((el) =>
      el.matches(".tile") && el !== owner && el.offsetTop === rowTop);
    for (const tile of otherTiles) {
      for (const panel of panelsForOwner(tile)) {
        if (!panel.hidden) setInlineExpanded(panel, false);
      }
    }
  }

  function setInlineExpanded(panel, on) {
    panel.hidden = !on;
    if (!on) stopVideos(panel);
    for (const link of $$(inlineSelector(panel.dataset.kind))) {
      if (inlineKey(panel.dataset.kind, link) === panel.dataset.key) link.setAttribute("aria-expanded", on ? "true" : "false");
    }
    updateInlinePanels(inlinePanelOwner(panel));
  }

  function makeInlinePanel(kind, link) {
    const owner = inlineOwner(link);
    if (!owner) return null;
    const panel = document.createElement("section");
    panel.className = `inline-panel ${kind}-panel`;
    panel.dataset.kind = kind;
    panel.dataset.key = inlineKey(kind, link);
    panel.opener = link;
    panel.innerHTML = `<button type="button" class="inline-panel-rail" aria-label="Collapse ${kind}" title="Collapse ${kind}"></button><div class="inline-panel-body"></div>`;
    panel.dataset.owner = owner.id || "thread";
    if (owner.matches(".tile")) {
      panel.classList.add("tile-inline-panel");
      panel.insertAdjacentHTML("afterbegin", '<svg class="tile-panel-spread" aria-hidden="true" focusable="false"><path class="tile-panel-spread-fill"></path><path class="tile-panel-spread-edges"></path></svg>');
      const rowTop = owner.offsetTop;
      const row = [...owner.parentElement.children].filter((el) => el.matches(".tile") && el.offsetTop === rowTop);
      let after = row[row.length - 1] || owner;
      while (after.nextElementSibling && after.nextElementSibling.matches(".inline-panel") &&
             after.nextElementSibling.dataset.owner === owner.id) after = after.nextElementSibling;
      after.insertAdjacentElement("afterend", panel);
    } else if (owner.matches("article.post") && kind === "article") {
      let slot = owner.nextElementSibling;
      if (!slot || !slot.matches(".thread-article-panels")) {
        slot = document.createElement("div");
        slot.className = "inline-panels thread-article-panels";
        owner.insertAdjacentElement("afterend", slot);
      }
      panel.classList.add("card-inline-panel");
      slot.append(panel);
    } else {
      const host = owner.matches(".post-card") ? $(".pc-main", owner) : owner;
      let slot = $(":scope > .inline-panels", host);
      if (!slot) {
        slot = document.createElement("div");
        slot.className = "inline-panels";
        host.append(slot);
      }
      panel.classList.add("card-inline-panel");
      slot.append(panel);
    }
    setInlineExpanded(panel, true);
    return panel;
  }

  function collapseInlinePanel(panel) {
    setInlineExpanded(panel, false);
    if (panel.opener && panel.opener.isConnected) panel.opener.focus({ preventScroll: true });
  }

  async function loadInlineArticle(panel, link) {
    const body = $(".inline-panel-body", panel);
    panel.setAttribute("aria-busy", "true");
    body.innerHTML = '<p class="muted inline-loading">Reading…</p>';
    const r = await fetch(paneUrl(link.href), { credentials: "same-origin" });
    if (!r.ok || new URL(r.url).origin !== location.origin) throw new Error("HTTP " + r.status);
    const doc = new DOMParser().parseFromString(await r.text(), "text/html");
    const article = $("article.reader", doc);
    if (!article) throw new Error("not an article");
    const deck = document.createElement("div");
    deck.className = "reader-deck inline-reader-deck";
    deck.dataset.deck = "";
    const col = document.createElement("div");
    col.className = "reader-col";
    col.append(document.adoptNode(article));
    deck.append(col);
    body.replaceChildren(deck);
    panel.removeAttribute("aria-busy");
  }

  async function loadInlinePost(panel, link) {
    const body = $(".inline-panel-body", panel);
    panel.setAttribute("aria-busy", "true");
    body.innerHTML = '<p class="muted inline-loading">Loading post text…</p>';
    const url = new URL(link.href, location.href);
    url.hash = "";
    url.searchParams.set("refreshed", "1");
    url.searchParams.set("inline", "1");
    const parts = postParts(await fetchDoc(url.href));
    if (!parts.length) throw new Error("post text not found");
    body.replaceChildren(...parts);
    panel.removeAttribute("aria-busy");
  }

  // What a post's page shows of the post itself: its video's player (with
  // the button to download and archive it), then its text (or description).
  function postParts(doc) {
    const video = $("article.post > .post-media .video-box, article.post > .post-media video", doc);
    const player = video && video.closest(".post-media");
    const pictures = $("article.post > .post-gallery", doc);
    const content = $("article.post > .md", doc);
    if (content) content.classList.add("expanded-post-content");
    return [player, pictures, content].filter(Boolean).map((el) => document.adoptNode(el));
  }

  function commentRoot(panel) { return $(".comments", panel); }

  // Comments (and copies' sections) you opened or collapsed stay that way in a fresh copy.
  const KEPT_OPEN = "details.comment[id], details.comment-section[id]";
  function keepOpen(now, fresh) {
    const shown = new Map($$(KEPT_OPEN, now).map((d) => [d.id, d.open]));
    for (const d of $$(KEPT_OPEN, fresh)) if (shown.has(d.id)) d.open = shown.get(d.id);
    fresh.id = now.id;
  }

  const writing = (root) => $$("textarea", root).some((t) => t.value.trim());

  // The panel's comments again, as saved (after you comment, say). `show`: an
  // element id in them to scroll to and flash, like your new comment.
  async function reloadPanelComments(panel, show) {
    const now = commentRoot(panel);
    const url = new URL(panel.dataset.commentsUrl || `/t/${now.dataset.thread}?inline=1`, location.href);
    url.searchParams.set("refreshed", "1");
    const fresh = $(".comments", await fetchDoc(url.href));
    if (!fresh || !now.isConnected) return;
    keepOpen(now, fresh);
    now.replaceWith(document.adoptNode(fresh));
    const el = show && fresh.querySelector(`#${CSS.escape(show)}`);
    if (!el) return;
    for (let p = el.parentElement; p && p !== fresh; p = p.parentElement) {
      if (p.matches("details")) { p.open = true; p.classList.remove("replies-hidden"); }
    }
    reveal(el);
    el.classList.add("flash-target");
    setTimeout(() => el.classList.remove("flash-target"), 1200);
  }

  async function submitInPanel(form, submitter) {
    if (form.dataset.busy) return;
    form.dataset.busy = "1";
    if (submitter) submitter.disabled = true;
    const panel = form.closest(".inline-panel");
    try {
      const data = await post(form.action, new URLSearchParams(new FormData(form, submitter)));
      for (const m of data.messages || []) toast(m);
      if (data.ok) {
        const hash = new URL(data.redirect || location.href, location.href).hash.slice(1);
        await reloadPanelComments(panel, hash);
      }
    } catch (e) {
      toast({ kind: "error", text: "That didn't work (" + e.message + "). Try again." });
    } finally {
      delete form.dataset.busy;
      if (submitter && submitter.isConnected) submitter.disabled = false;
    }
  }

  function setCommentsStatus(panel, text, bad = false) {
    let status = $("[data-inline-comment-status]", panel);
    if (!status) {
      status = document.createElement("p");
      status.className = "small muted";
      status.dataset.inlineCommentStatus = "";
      commentRoot(panel).querySelector(".comments-head").insertAdjacentElement("afterend", status);
    }
    status.classList.toggle("bad-text", bad);
    status.textContent = text;
  }

  async function waitForCommentJobs(panel, jobs, freshUrl) {
    if (!jobs.length) return;
    setCommentsStatus(panel, "Checking for new comments…");
    const started = Date.now();
    let failed = "";
    while (Date.now() - started < 60000) {
      let done = true;
      for (const id of jobs) {
        try {
          const r = await fetch(`/api/jobs/${id}`, { credentials: "same-origin" });
          const job = r.ok ? await r.json() : null;
          if (!job || !["done", "failed"].includes(job.status)) done = false;
          if (job && job.status === "failed") failed = job.error || "the check failed";
        } catch (e) { done = false; }
      }
      if (done) {
        if (failed) { setCommentsStatus(panel, `Couldn't check for new comments (${failed}). Showing the saved copy.`, true); return; }
        try {
          const now = commentRoot(panel);
          if (now && writing(now)) {
            setCommentsStatus(panel, "New comments arrived. They'll show once you've posted yours.");
            return;
          }
          const doc = await fetchDoc(panel.dataset.commentsUrl || freshUrl);
          const fresh = $(".comments", doc);
          if (fresh && now && now.isConnected) {
            keepOpen(now, fresh);
            steady(() => now.replaceWith(document.adoptNode(fresh)));
          }
        } catch (e) { setCommentsStatus(panel, "New comments arrived; reload to show them."); }
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    setCommentsStatus(panel, "Still checking for new comments; leave this open or try again in a moment.");
  }

  async function startInlineCommentCheck(panel, tid, existingStatus) {
    if (panel.dataset.checking) return;
    panel.dataset.checking = "true";
    let jobs = existingStatus && existingStatus.dataset.job ? [existingStatus.dataset.job] : [];
    let freshUrl = existingStatus && existingStatus.dataset.url;
    try {
      if (!jobs.length) {
        const r = await fetch(`/t/${tid}/comments/check`, { method: "POST", headers: FETCH, credentials: "same-origin" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        jobs = (await r.json()).jobs || [];
        freshUrl = `/t/${tid}?refreshed=1&inline=1`;
      }
      if (existingStatus) existingStatus.remove();
      await waitForCommentJobs(panel, jobs, freshUrl);
    } finally {
      delete panel.dataset.checking;
    }
  }

  async function loadInlineComments(panel, link) {
    const body = $(".inline-panel-body", panel);
    const tid = inlineThreadId(link);
    panel.setAttribute("aria-busy", "true");
    body.innerHTML = '<p class="muted inline-loading">Loading comments…</p>';
    let root = tid && $(".comments[data-inline-source]");
    const commentsUrl = new URL(link.href, location.href);
    commentsUrl.hash = "";
    commentsUrl.searchParams.set("inline", "1");
    panel.dataset.commentsUrl = commentsUrl.href;
    if (root && inlineOwner(link).matches("article.post")) {
      body.replaceChildren(root);
    } else {
      const doc = await fetchDoc(commentsUrl.href);
      root = $(".comments", doc);
      if (!root) throw new Error("comments not found");
      root.id = `comments-${tid}`;
      body.replaceChildren(document.adoptNode(root));
    }
    panel.removeAttribute("aria-busy");
    const status = $("#refreshing", root);
    startInlineCommentCheck(panel, tid, status).catch((e) =>
      setCommentsStatus(panel, `Couldn't check for new comments (${e.message}). Showing the saved copy.`, true));
  }

  async function sortInlineComments(panel, link) {
    if (panel.dataset.sorting) return;
    panel.dataset.sorting = "true";
    panel.setAttribute("aria-busy", "true");
    const current = commentRoot(panel);
    const url = new URL(link.href, location.href);
    url.hash = "";
    url.searchParams.set("inline", "1");
    url.searchParams.set("refreshed", "1");
    try {
      const doc = await fetchDoc(url.href);
      const fresh = $(".comments", doc);
      if (!fresh) throw new Error("comments not found");
      keepOpen(current, fresh);
      current.replaceWith(document.adoptNode(fresh));
      panel.dataset.commentsUrl = url.href;
    } finally {
      delete panel.dataset.sorting;
      panel.removeAttribute("aria-busy");
    }
  }

  async function toggleInline(link, kind) {
    const key = inlineKey(kind, link);
    let panel = $$(".inline-panel").find((p) => p.dataset.key === key);
    if (panel) {
      panel.opener = link;
      if (!panel.hidden) { collapseInlinePanel(panel); return; }
      closeOtherTileInRow(inlineOwner(link));
      setInlineExpanded(panel, true);
      if (kind === "comments") startInlineCommentCheck(panel, inlineThreadId(link), null).catch((e) =>
        setCommentsStatus(panel, `Couldn't check for new comments (${e.message}). Showing the saved copy.`, true));
      reveal(panel);
      return;
    }
    closeOtherTileInRow(inlineOwner(link));
    panel = makeInlinePanel(kind, link);
    if (!panel) { location.assign(link.href); return; }
    try {
      if (kind === "article") await loadInlineArticle(panel, link);
      else if (kind === "post") await loadInlinePost(panel, link);
      else await loadInlineComments(panel, link);
      reveal(panel);
    } catch (e) {
      panel.remove();
      location.assign(link.href);
    }
  }

  // ---- diving into linked articles (the reader) --------------------------------------
  // A link to another article opens it beside the one you're reading on a wide
  // screen (a column each, two or three on screen, the newest scrolled to), or
  // inside it, under the paragraph with the link, on a narrow one. Opening one
  // from a column replaces the columns after it. Without this they're pages.
  // A YouTube link opens its video's box the same way, and anywhere else
  // (a post, a comment) under the paragraph with the link; a livestream's
  // link (Twitch, YouTube live, Owncast) its player.
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

  function diveDepth(link) {
    const parent = link.closest(".dive-col, .dive-inline");
    return parent ? +(parent.dataset.depth || 0) + 1 : 0;
  }

  async function dive(link) {
    const deck = deckOf(link);
    const wide = !!deck && WIDE.matches && !deck.classList.contains("inline-reader-deck");
    const box = document.createElement(wide ? "div" : "section");
    const depth = diveDepth(link);
    box.className = wide ? "reader-col dive-col" : "dive-inline";
    box.classList.add(`dive-depth-${depth % 8}`);
    box.dataset.depth = String(depth);
    box.opener = link;
    box.setAttribute("aria-busy", "true");
    box.innerHTML = '<button type="button" class="dive-rail" aria-label="Collapse this article" title="Collapse this article"></button><div class="dive-content"><p class="muted dive-loading">Reading…</p></div>';
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
      const article = $("article.reader, article.video-box, article.live-box", doc);
      if (!article) throw new Error("not an article");
      if (!box.isConnected) return; // closed while it was being read
      $(".dive-content", box).replaceChildren(document.adoptNode(article));
      box.removeAttribute("aria-busy");
      if (wide) box.scrollTop = 0; else reveal(box);
    } catch (e) {
      // Not something to read here after all (the site itself, say): open it as a page.
      if (box.isConnected) closeDive(box);
      location.assign(link.href);
    }
  }

  document.addEventListener("click", (ev) => {
    const rail = ev.target.closest(".inline-panel-rail");
    if (rail) { collapseInlinePanel(rail.closest(".inline-panel")); return; }
    const diveRail = ev.target.closest(".dive-rail");
    if (diveRail) { closeDive(diveRail.closest(".dive-col, .dive-inline")); return; }
    const close = ev.target.closest("[data-dive-close]");
    if (close) {
      const box = close.closest(".dive-col, .dive-inline");
      if (box) closeDive(box);
      else {
        const panel = close.closest(".inline-panel");
        if (panel) collapseInlinePanel(panel);
      }
      return;
    }
    const link = ev.target.closest("a.article-link, a[data-dive], a.video-link, a.live-link");
    if (!link || !(deckOf(link) || link.matches(".video-link, .live-link"))) return;
    if (ev.button !== 0 || ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey) return;
    ev.preventDefault();
    if (link.classList.contains("dived")) {
      const box = [...$$(".dive-col, .dive-inline", deckOf(link) || document)].find((b) => b.opener === link);
      if (box) { closeDive(box); return; }
    }
    dive(link);
  });

  document.addEventListener("click", (ev) => {
    const link = ev.target.closest("a.read-article, a.inline-comments, a.read-post");
    if (!link || ev.button !== 0 || ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey) return;
    ev.preventDefault();
    toggleInline(link, link.classList.contains("read-article") ? "article" :
      link.classList.contains("read-post") ? "post" : "comments");
  });

  // Comment controls in panels loaded after the page's thread.js ran.
  document.addEventListener("click", (ev) => {
    const root = ev.target.closest(".inline-panel .comments");
    if (!root) return;
    const sortLink = ev.target.closest(".comments-head .seg a");
    if (sortLink && ev.button === 0 && !ev.ctrlKey && !ev.metaKey && !ev.shiftKey && !ev.altKey) {
      ev.preventDefault();
      const panel = root.closest(".inline-panel");
      sortInlineComments(panel, sortLink).catch((e) =>
        setCommentsStatus(panel, `Couldn't sort comments (${e.message}).`, true));
      return;
    }
    const all = () => $$("details.comment", root);
    const rail = ev.target.closest("button.section-rail");
    if (rail) {
      const section = rail.closest("details.comment-section");
      section.open = false;
      reveal(section);
      return;
    }
    const bar = ev.target.closest("button.bar");
    if (bar) { bar.closest("details.comment").open = false; return; }
    const btn = ev.target.closest("[data-action]");
    if (!btn) return;
    const clear = () => $$(".replies-hidden", root).forEach((d) => d.classList.remove("replies-hidden"));
    if (btn.dataset.action === "expand-all") {
      clear();
      all().forEach((d) => (d.open = true));
      $$("details.comment-section", root).forEach((d) => (d.open = true));
    }
    if (btn.dataset.action === "collapse-all") { clear(); all().forEach((d) => (d.open = false)); }
    if (btn.dataset.action === "collapse-replies") all().forEach((d) => {
      d.open = true;
      if (!d.parentElement.closest("details.comment")) d.classList.add("replies-hidden");
    });
    if (btn.dataset.action === "show-replies") btn.closest("details.comment").classList.remove("replies-hidden");
    if (btn.dataset.action === "next-highlight") {
      const marks = $$("details.comment.is-new, details.comment.is-changed", root);
      const next = marks.find((m) => m.getBoundingClientRect().top > headerH() + 1) || marks[0];
      if (next) {
        for (let p = next.parentElement; p && p !== root; p = p.parentElement) {
          if (p.matches("details.comment, details.comment-section")) { p.open = true; p.classList.remove("replies-hidden"); }
        }
        next.open = true;
        reveal(next);
      }
    }
  });

  // ---- video links' titles, and their boxes ----------------------------------------------
  // A link whose YouTube video's title isn't known here yet shows its address
  // until the title is asked of YouTube, once the link is on the page. A box
  // (a link's, or a post's that is a video's link) plays the video in its
  // site's player, which goes in once it's shown; one saving its video checks
  // back until it's saved, then plays the saved copy. A YouTube box asks how
  // big the video is once it's shown, so the player needn't wait for that.
  const askedTitles = new Set();
  let titleTimer = null;

  function askTitles() {
    titleTimer = null;
    const links = $$("a.video-link.untitled").filter((a) => !askedTitles.has(a.pathname.split("/").pop()));
    const ids = [...new Set(links.map((a) => a.pathname.split("/").pop()))];
    if (!ids.length) return;
    ids.forEach((id) => askedTitles.add(id));
    fetch("/youtube/titles?" + new URLSearchParams(ids.map((id) => ["ids", id])),
      { credentials: "same-origin", headers: FETCH })
      .then((r) => (r.ok ? r.json() : {}))
      .then((titles) => {
        for (const a of $$("a.video-link.untitled")) {
          const title = titles[a.pathname.split("/").pop()];
          if (title) { a.textContent = title; a.classList.remove("untitled"); }
        }
      })
      .catch(() => {});
  }

  // The box again: what's below the player is swapped in, and the player
  // stays, so a video playing carries on; unless the saved copy has come and
  // the site's player wasn't started, when the whole box is. `probe`: find
  // out how big the video is (YouTube's, which takes a few seconds).
  async function reloadVideoBox(box, probe = false) {
    const url = new URL(box.dataset.src, location.href);
    if (probe) url.searchParams.set("probe", "1");
    const r = await fetch(url, { credentials: "same-origin" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const fresh = $("article.video-box", new DOMParser().parseFromString(await r.text(), "text/html"));
    if (!fresh || !box.isConnected) return;
    const savedNow = $("video.media[src^='/media/']", fresh) && !$("video.media[src^='/media/']", box);
    if (savedNow && !("played" in box.dataset)) {
      const nav = $(".reader-nav", box);
      if (nav && !box.closest(".dive-col, .dive-inline")) $(".reader-nav", fresh).replaceWith(nav); // the page of its own: no Close there
      steady(() => box.replaceWith(document.adoptNode(fresh)));
      return;
    }
    box.toggleAttribute("data-video-saving", fresh.hasAttribute("data-video-saving"));
    steady(() => $(".video-save", box).replaceWith(document.adoptNode($(".video-save", fresh))));
  }

  function watchSaving() {
    for (const box of $$("article.video-box[data-video-saving]:not([data-watched])")) {
      box.dataset.watched = "1";
      const check = () => {
        if (!box.isConnected) return;
        reloadVideoBox(box)
          .then(() => {
            if (box.isConnected && box.hasAttribute("data-video-saving")) setTimeout(check, 5000);
            else delete box.dataset.watched;
          })
          .catch(() => setTimeout(check, 15000));
      };
      setTimeout(check, 5000);
    }
  }

  function askSizes() {
    for (const save of $$("article.video-box .video-save[data-video-probe]:not([data-asked])")) {
      save.dataset.asked = "1";
      const box = save.closest("article.video-box");
      reloadVideoBox(box, true).catch(() => {
        const size = $(".video-box-size", box);
        if (size) size.textContent = "Couldn't find out how big the video is.";
      });
    }
  }

  // Whether a box's player was started: the site's (in its frame) takes the
  // focus from the page when it's clicked; a file's plays.
  addEventListener("blur", () => setTimeout(() => {
    const frame = document.activeElement;
    const box = frame && frame.tagName === "IFRAME" && frame.closest("article.video-box");
    if (box) box.dataset.played = "1";
  }));
  document.addEventListener("play", (ev) => {
    const box = ev.target.closest && ev.target.closest("article.video-box");
    if (box) box.dataset.played = "1";
  }, true);

  // A hidden panel's videos stop: the saved copy's pauses, and a site's player
  // is put in again, from the start.
  function stopVideos(root) {
    for (const v of $$("article.video-box video, .post-media video", root)) v.pause();
    for (const frame of $$(".video-player[data-started] iframe", root)) {
      const slot = frame.parentElement;
      delete slot.dataset.started;
      slot.replaceChildren();
    }
  }

  document.addEventListener("submit", (ev) => {
    const form = ev.target.closest("form[data-video-save]");
    if (!form) return;
    ev.preventDefault();
    const box = form.closest("article.video-box");
    const button = $("button", form);
    if (button) button.disabled = true;
    post(form.action, new URLSearchParams(new FormData(form)))
      .then(() => reloadVideoBox(box))
      .catch((e) => {
        if (button) button.disabled = false;
        toast({ kind: "error", text: "Couldn't start saving the video (" + e.message + "). Try again." });
      });
  });

  // ---- livestreams: their players, and which sites are Owncast servers ---------------
  // A livestream's box gets its player once it's on the page (so closing the
  // box stops it), as a video's box does. Twitch's player wants the name this
  // site is reached by. Links to sites' front pages might be Owncast servers:
  // they're asked about once shown (the server remembers), and the ones that
  // are open theirs too.
  function startPlayers() {
    for (const el of $$("article.live-box:not([data-started]), .video-player[data-embed]:not([data-started])")) {
      el.dataset.started = "1";
      const frame = document.createElement("iframe");
      let src = el.dataset.embed;
      if ("parent" in el.dataset) src += "&parent=" + encodeURIComponent(location.hostname);
      frame.src = src;
      frame.title = el.dataset.label || "Player";
      frame.allow = "autoplay; fullscreen; picture-in-picture; encrypted-media";
      frame.allowFullscreen = true;
      frame.referrerPolicy = "strict-origin-when-cross-origin"; // YouTube's player won't play without it
      (el.matches(".live-box") ? $(".live-player", el) : el).replaceChildren(frame);
    }
  }

  const askedHosts = new Set();
  let hostTimer = null;

  function owncastHost(a) {
    if (a.closest(".live-box") || !a.href) return null;
    try {
      const u = new URL(a.href);
      if (u.protocol !== "https:" || u.pathname !== "/" || u.search || u.hash || u.username) return null;
      if (u.origin === location.origin) return null;
      return u.host;
    } catch (e) { return null; }
  }

  function askOwncast() {
    hostTimer = null;
    const hosts = [...new Set($$("a[target=_blank]").map(owncastHost).filter((h) => h && !askedHosts.has(h)))];
    if (!hosts.length) return;
    hosts.forEach((h) => askedHosts.add(h));
    fetch("/live/owncast?" + new URLSearchParams(hosts.slice(0, 50).map((h) => ["hosts", h])),
      { credentials: "same-origin", headers: FETCH })
      .then((r) => (r.ok ? r.json() : []))
      .then((found) => {
        const owncast = new Set(found);
        for (const a of $$("a[target=_blank]")) {
          const host = owncastHost(a);
          if (!host || !owncast.has(host)) continue;
          a.href = "/live/owncast/" + host;
          a.removeAttribute("target");
          a.classList.add("live-link");
          a.title = "Owncast live stream: watch it here";
        }
      })
      .catch(() => {});
  }

  new MutationObserver(() => {
    if (!titleTimer) titleTimer = setTimeout(askTitles, 200);
    if (!hostTimer) hostTimer = setTimeout(askOwncast, 200);
    watchSaving();
    askSizes();
    startPlayers();
  }).observe(document.documentElement, { childList: true, subtree: true });

  // Crossing between wide and narrow: columns and inline articles don't carry over, so close them.
  WIDE.addEventListener("change", () => {
    for (const deck of $$("[data-deck]")) {
      if (deck.classList.contains("inline-reader-deck")) continue;
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
  const GO = { f: "/", c: "/communities", r: "/trending", a: "/trending/articles", k: "/kept", i: "/inbox", t: "/trash", s: "/search" };

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
        const compose = $("#comments .compose textarea");
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

  // ---- where an article is discussed ------------------------------------------------
  // Opening a post or an article asks other places about it (discussions.py).
  // Its Discussions section says so (data-job) until that's done, then the
  // answer is swapped in. Readers opened into the page get the same, as they arrive.
  const DISCUSSIONS_WAIT_MS = 90000;

  function watchDiscussions(root) {
    const boxes = root.matches && root.matches("[data-discussions][data-job]") ? [root] : $$("[data-discussions][data-job]", root);
    for (const box of boxes) {
      if (box.dataset.watching) continue;
      box.dataset.watching = "true";
      awaitDiscussions(box);
    }
  }

  async function awaitDiscussions(box) {
    const started = Date.now();
    while (box.isConnected && Date.now() - started < DISCUSSIONS_WAIT_MS) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      try {
        const r = await fetch(`/api/jobs/${box.dataset.job}`, { credentials: "same-origin" });
        const job = r.ok ? await r.json() : null;
        if (job && (job.status === "done" || job.status === "failed")) break;
      } catch (e) { /* try again */ }
    }
    if (!box.isConnected) return;
    try {
      const fresh = $("[data-discussions]", await fetchDoc(box.dataset.discussions));
      if (fresh && box.isConnected) steady(() => box.replaceWith(document.adoptNode(fresh)));
    } catch (e) { /* the saved list stays; reloading shows the rest */ }
  }

  // Each one opens into the panels a post opens into in the feed: its text,
  // then its comments, asked for the first time it's opened. A post saved
  // here is its own page's (its comments to reply to and vote on, checked for
  // new ones); one found elsewhere is read there (discussion_peek.html).
  function discussionPanel(kind, label, parts) {
    const panel = document.createElement("section");
    panel.className = `inline-panel ${kind}-panel card-inline-panel`;
    panel.dataset.kind = kind;
    panel.innerHTML = `<button type="button" class="inline-panel-rail" aria-label="Collapse the ${label}" title="Collapse the ${label}"></button><div class="inline-panel-body"></div>`;
    $(".inline-panel-body", panel).append(...parts);
    return panel;
  }

  async function loadSavedDiscussion(d, body) {
    const tid = d.dataset.thread;
    const url = new URL(`/t/${tid}?inline=1`, location.href);
    const doc = await fetchDoc(url.href);
    const comments = $(".comments", doc);
    if (!comments) throw new Error("comments not found");
    comments.id = `comments-${tid}`;
    let text = postParts(doc);
    if (!text.length) {
      const none = document.createElement("p");
      none.className = "muted";
      none.textContent = "No text, just the link.";
      text = [none];
    }
    const box = document.createElement("div");
    box.className = "discussion-peek inline-panels discussion-panels";
    box.dataset.peekBody = "";
    const panel = discussionPanel("comments", "comments", [document.adoptNode(comments)]);
    panel.dataset.commentsUrl = url.href;
    box.append(discussionPanel("post", "text", text), panel);
    body.replaceWith(box);
    startInlineCommentCheck(panel, tid, $("#refreshing", comments)).catch((e) =>
      setCommentsStatus(panel, `Couldn't check for new comments (${e.message}). Showing the saved copy.`, true));
    return box;
  }

  function showDiscussionPanel(d, kind, on) {
    const panel = $(`:scope > .discussion-panel > .discussion-panels > .inline-panel[data-kind="${kind}"]`, d);
    if (!panel) return;
    panel.hidden = !on;
    const button = $(`:scope > .discussion-panel .discussion-toggle[data-show="${kind}"]`, d);
    if (button) button.setAttribute("aria-expanded", String(on));
    joinPanels($$(":scope > .inline-panel", panel.parentElement));
  }

  document.addEventListener("click", (ev) => {
    const toggle = ev.target.closest(".discussion-toggle");
    const rail = !toggle && ev.target.closest(".discussion-panels > .inline-panel > .inline-panel-rail");
    if (!toggle && !rail) return;
    const d = (toggle || rail).closest("details.discussion");
    if (!d) return;  // (a trending post's, below)
    ev.stopPropagation();
    if (toggle) showDiscussionPanel(d, toggle.dataset.show, toggle.getAttribute("aria-expanded") !== "true");
    else showDiscussionPanel(d, rail.closest(".inline-panel").dataset.kind, false);
  }, true);

  // ---- arranging the Following list (/following) -------------------------------------
  // A row is dragged into place (into a folder, or out of one: folders stay at
  // the top level), or moved with its arrows or into a folder with its menu.
  // Each list's rows are then numbered in order, and a community's folder set
  // to the one it's in: that's what the form sends.
  function renumberLayout(form) {
    for (const list of $$(".le-list", form)) {
      if ("hiddenList" in list.dataset) continue;
      [...list.children].forEach((row, n) => {
        const pos = $(".le-pos", row);
        if (pos) pos.value = n + 1;
        const folder = row.matches(".le-community") && $(".le-in", row);
        if (folder) folder.value = list.dataset.list;
      });
    }
  }

  function unhideRow(row) {
    const box = $("input[name^=hide_]", row);
    if (box && box.checked && !row.parentElement.matches("[data-hidden-list]")) {
      box.checked = false;
      row.classList.remove("le-hidden");
    }
  }

  document.addEventListener("click", (ev) => {
    const button = ev.target.closest(".layout-editor [data-move]");
    if (!button) return;
    const row = button.closest(".le-row");
    const up = Number(button.dataset.move) < 0;
    const next = up ? row.previousElementSibling : row.nextElementSibling;
    const folder = row.parentElement.closest(".le-folder");
    if (next) {
      if (up) next.before(row); else next.after(row);
    } else if (folder && row.matches(".le-community")) {  // off the end of a folder: out of it
      if (up) folder.before(row); else folder.after(row);
    }
    renumberLayout(row.closest("form"));
    button.focus();
  });

  document.addEventListener("change", (ev) => {
    const select = ev.target.closest(".layout-editor .le-in");
    if (select) {
      const form = select.closest("form");
      const row = select.closest(".le-row");
      const list = $$(".le-list", form).find((l) => l.dataset.list === select.value && !("hiddenList" in l.dataset));
      if (list && row.parentElement !== list) list.append(row);
      unhideRow(row);
      renumberLayout(form);
      return;
    }
    const hide = ev.target.closest(".layout-editor input[name^=hide_]");
    if (hide) hide.closest(".le-row").classList.toggle("le-hidden", hide.checked);
  });

  let draggedRow = null;
  const DROP_MARKS = ["le-drop-before", "le-drop-after", "le-drop-into"];
  const clearDropMarks = () => $$(".le-drop-before, .le-drop-after, .le-drop-into").forEach((el) =>
    el.classList.remove(...DROP_MARKS));

  // Where a row dropped here would go: {into: list} or {before|after: row}; null where it can't.
  function dropPlace(ev) {
    if (!draggedRow || !ev.target.closest) return null;
    const community = draggedRow.matches(".le-community");
    const line = ev.target.closest(".layout-editor .le-folder > .le-line");
    if (line && community && line.parentElement !== draggedRow) return { into: $(".le-members", line.parentElement) };
    let row = ev.target.closest(".layout-editor .le-row");
    if (row && !community) row = row.closest(".le-top > .le-row");  // folders stay at the top level
    if (row && row !== draggedRow && !draggedRow.contains(row) && !row.closest("[data-hidden-list]")) {
      const box = (row.matches(".le-folder") ? $(".le-line", row) : row).getBoundingClientRect();
      return ev.clientY < box.top + box.height / 2 ? { before: row } : { after: row };
    }
    const list = ev.target.closest(".layout-editor .le-list");
    if (list && !list.matches("[data-hidden-list]") && (community || list.matches(".le-top")) &&
        !draggedRow.contains(list) && !row) return { into: list };
    return null;
  }

  document.addEventListener("dragstart", (ev) => {
    const row = ev.target.closest && ev.target.closest(".layout-editor .le-row");
    if (!row || ev.target.closest("input, select, button")) return;
    draggedRow = row;
    row.classList.add("le-dragging");
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", row.dataset.community || row.dataset.folder || "");
  });
  document.addEventListener("dragend", () => {
    if (draggedRow) draggedRow.classList.remove("le-dragging");
    draggedRow = null;
    clearDropMarks();
  });
  document.addEventListener("dragover", (ev) => {
    const place = dropPlace(ev);
    if (!place) return;
    ev.preventDefault();
    clearDropMarks();
    if (place.into) place.into.classList.add("le-drop-into");
    else (place.before || place.after).classList.add(place.before ? "le-drop-before" : "le-drop-after");
  });
  document.addEventListener("drop", (ev) => {
    const place = dropPlace(ev);
    if (!place) return;
    ev.preventDefault();
    const row = draggedRow;
    if (place.into) place.into.append(row);
    else if (place.before) place.before.before(row);
    else place.after.after(row);
    clearDropMarks();
    unhideRow(row);
    renumberLayout(row.closest("form"));
  });

  // ---- hiding someone, or a feed: gone at once, the server told meanwhile -------------
  // Every post of theirs on the page (and anything opened under one) goes as
  // soon as you've said yes; the page isn't reloaded. Undo, or the server
  // saying no, brings them back.
  async function hideSource(form) {
    const key = form.elements.key.value;
    const cards = [...new Set($$("form[data-hide]").filter((f) => f.elements.key.value === key)
      .map((f) => f.closest("[data-entry]")).filter(Boolean))];
    const panels = cards.flatMap((c) => panelsForOwner(c).filter((p) => !p.hidden));
    const show = (on) => {
      for (const c of cards) c.hidden = !on;
      for (const p of panels) p.hidden = !on;
    };
    show(false);
    try {
      const data = await post(form.action, new URLSearchParams(new FormData(form)));
      if (!data.ok) show(true);
      for (const m of data.messages || []) toast(m, m.undo ? () => undoHide(m.undo, show) : null);
    } catch (e) {
      show(true);
      toast({ kind: "error", text: "Couldn't hide them (" + e.message + "). Try again." });
    } finally {
      delete form.dataset.confirmed;  // asked again next time
      const asked = form.querySelector("input[name=confirmed]");
      if (asked) asked.remove();
    }
  }

  async function undoHide(undo, show) {
    show(true);
    try {
      const data = await post(undo.action, undo.fields);
      for (const m of data.messages || []) toast(m);
    } catch (e) {
      show(false);
      toast({ kind: "error", text: "Couldn't undo: " + e.message });
    }
  }

  // ---- a trending post's like and repost buttons ----------------------------------
  // Shown done at once, the count with them; put back if it didn't go through.
  function showTrendAct(form, on) {
    const button = $("button", form);
    const n = $(".n", button);
    if (button.classList.contains("on") !== on && n) n.textContent = Math.max(0, (+n.textContent || 0) + (on ? 1 : -1));
    button.classList.toggle("on", on);
    button.setAttribute("aria-pressed", String(on));
    $("input[name=on]", form).value = on ? "0" : "1";  // what pressing it again does
  }

  async function trendAct(form) {
    if (form.dataset.busy) return;
    form.dataset.busy = "1";
    const fields = new URLSearchParams(new FormData(form));
    const on = fields.get("on") === "1";
    const button = $("button", form);
    button.setAttribute("aria-busy", "true");
    showTrendAct(form, on);
    try {
      const data = await post(form.action, fields);
      if (!data.ok) showTrendAct(form, !on);
      for (const m of data.messages || []) toast(m);
    } catch (e) {
      showTrendAct(form, !on);
      toast({ kind: "error", text: "That didn't go through (" + e.message + "). Try again." });
    } finally {
      delete form.dataset.busy;
      button.removeAttribute("aria-busy");
    }
  }

  // ---- trending posts' pictures, downloaded as they're scrolled to --------------------
  // A post whose pictures aren't here yet asks for them once it's on screen,
  // then checks back until they're downloaded, and shows them in its card.
  // A picture pressed shows bigger, in place; pressed again, small.
  const pictureQueue = new Set();
  const pictureWaiting = new Set();
  let pictureTimer = null;
  let pictureCheck = null;
  let pictureUntil = 0;
  let pictureObserver = null;

  function watchTrendPictures(root) {
    if (!("IntersectionObserver" in window)) return;
    pictureObserver = pictureObserver || new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (!e.isIntersecting || !e.target.dataset.post || !("pictures" in e.target.dataset)) continue;
        pictureObserver.unobserve(e.target);
        pictureQueue.add(e.target.dataset.post);
      }
      if (pictureQueue.size && !pictureTimer) pictureTimer = setTimeout(askTrendPictures, 300);
    }, { rootMargin: "200px 0px" });
    const cards = $$(".trending-post[data-pictures]", root);
    if (root.matches && root.matches(".trending-post[data-pictures]")) cards.push(root);  // one "Load more" added
    for (const card of cards) pictureObserver.observe(card);
  }

  function showTrendPictures(ready) {
    for (const [key, pics] of Object.entries(ready || {})) {
      const box = document.getElementById("pics-" + key);
      if (!box || !pics.length || box.childElementCount) continue;
      for (const pic of pics) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "trend-pic";
        button.dataset.full = pic.full;
        button.title = "Show it bigger";
        button.setAttribute("aria-pressed", "false");
        const img = document.createElement("img");
        img.src = pic.src;
        img.alt = "";
        img.loading = "lazy";
        button.append(img);
        box.append(button);
      }
      steady(() => {
        box.hidden = false;
        const note = box.parentElement && $(".trend-media-note", box.parentElement);
        if (note && !note.textContent.includes("video") && !note.textContent.includes("quotes")) note.remove();
      });
    }
  }

  async function askTrendPictures() {
    pictureTimer = null;
    const posts = [...pictureQueue];
    pictureQueue.clear();
    try {
      const data = await post("/trending/pictures", { post: posts });
      showTrendPictures(data.ready);
      for (const p of posts) if (data.waiting.includes(keyOfPost(p))) pictureWaiting.add(p);
    } catch (e) { return; }
    // Pictures are downloaded one a second from each site, so a page of them can take a few minutes.
    pictureUntil = Date.now() + 300000;
    if (pictureWaiting.size && !pictureCheck) pictureCheck = setTimeout(checkTrendPictures, 2500);
  }

  const keyOfPost = (p) => (($$(".trending-post").find((c) => c.dataset.post === p) || {}).id || "").slice(3);

  async function checkTrendPictures() {
    pictureCheck = null;
    const posts = [...pictureWaiting];
    if (!posts.length) return;
    try {
      const url = new URL("/trending/pictures", location.href);
      for (const p of posts) url.searchParams.append("post", p);
      const r = await fetch(url, { credentials: "same-origin" });
      const data = await r.json();
      showTrendPictures(data.ready);
      for (const p of posts) if (!data.waiting.includes(keyOfPost(p))) pictureWaiting.delete(p);
    } catch (e) { /* tried again below */ }
    if (pictureWaiting.size && Date.now() < pictureUntil) pictureCheck = setTimeout(checkTrendPictures, 3000);
  }

  document.addEventListener("click", (ev) => {
    const pic = ev.target.closest("button.trend-pic");
    if (!pic) return;
    const img = $("img", pic);
    const big = !pic.classList.contains("big");
    if (big && !pic.dataset.small) pic.dataset.small = img.src;
    img.src = big ? pic.dataset.full : pic.dataset.small;
    pic.classList.toggle("big", big);
    pic.setAttribute("aria-pressed", String(big));
    pic.title = big ? "Show it smaller" : "Show it bigger";
  });

  // ---- a trending post's text and replies, read where it was posted ----------------
  // Replies opens them under the post, as a found discussion opens
  // (discussion_peek.html); either rail, or Replies again, closes them.
  function showTrendPeek(link, box, on) {
    box.hidden = !on;
    link.setAttribute("aria-expanded", String(on));
    if (on) {
      for (const panel of $$(":scope > .inline-panel", box)) panel.hidden = false;
      joinPanels($$(":scope > .inline-panel", box));
      reveal(box);
    } else stopVideos(box);
  }

  document.addEventListener("click", async (ev) => {
    const rail = ev.target.closest(".trend-peek-box > .inline-panel > .inline-panel-rail");
    if (rail) {
      ev.stopPropagation();
      const box = rail.closest(".trend-peek-box");
      if (box.opener) { showTrendPeek(box.opener, box, false); box.opener.focus({ preventScroll: true }); }
      return;
    }
    const link = ev.target.closest("a.trend-peek");
    if (!link || ev.button !== 0 || ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey) return;
    ev.preventDefault();
    ev.stopPropagation();
    const main = link.closest(".pc-main");
    let box = main && $(":scope > .trend-peek-box", main);
    if (box) { showTrendPeek(link, box, box.hidden); return; }
    if (!main || link.getAttribute("aria-busy")) return;
    link.setAttribute("aria-busy", "true");
    const loading = document.createElement("p");
    loading.className = "muted small trend-peek-box";
    loading.textContent = "Reading its replies…";
    main.append(loading);
    try {
      const fresh = $("[data-peek-body]", await fetchDoc(link.dataset.peek));
      if (!fresh) throw new Error("nothing to show");
      box = document.adoptNode(fresh);
      box.classList.add("trend-peek-box");
      box.opener = link;
      loading.replaceWith(box);
      showTrendPeek(link, box, true);
    } catch (e) {
      loading.textContent = "Couldn't read its replies. Try again in a moment.";
      loading.classList.add("bad-text");
      setTimeout(() => loading.remove(), 4000);
    } finally {
      link.removeAttribute("aria-busy");
    }
  }, true);

  document.addEventListener("toggle", async (e) => {
    const d = e.target;
    if (!d.matches || !d.matches("details.discussion") || !d.open || d.dataset.peeked) return;
    const body = $("[data-peek-body]", d);
    if (!body || (!d.dataset.thread && !d.dataset.peek)) return;
    d.dataset.peeked = "true";
    body.innerHTML = '<p class="muted">Loading…</p>';
    try {
      let box;
      if (d.dataset.thread) box = await loadSavedDiscussion(d, body);
      else {
        box = $("[data-peek-body]", await fetchDoc(d.dataset.peek));
        if (box) body.replaceWith(document.adoptNode(box));
      }
      if (box) joinPanels($$(":scope > .inline-panel", box));
    } catch (err) {
      body.innerHTML = '<p class="bad-text">Couldn\'t load it. Close and open it again to try again.</p>';
      delete d.dataset.peeked;
    }
  }, true);

  document.addEventListener("DOMContentLoaded", () => {
    keepFeedAsOf();
    watchUnread(document);
    watchArticles(document);
    watchDiscussions(document);
    watchTrendPictures(document);
    new MutationObserver((changes) => {
      for (const c of changes) for (const el of c.addedNodes) if (el.nodeType === 1) {
        watchDiscussions(el);
        watchTrendPictures(el);
      }
    }).observe(document.body, { childList: true, subtree: true });
    askTitles();
    watchSaving();
    askOwncast();
    startPlayers();
    if (location.hash === "#follow") {
      const input = $("#follow input[name=community]");
      if (input) input.focus();
    }
    if (location.hash === "#comments" || /^#o\d+$/.test(location.hash)) {
      const link = $("#thread-actions .inline-comments");
      if (link) link.click();
    }
  });
})();
