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
      added.forEach((el) => watchUnread(el));
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
        for (const menu of $$("details.menu[open]")) menu.open = false;
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
    if (location.hash === "#follow") {
      const input = $("#follow input[name=community]");
      if (input) input.focus();
    }
  });
})();
