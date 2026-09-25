// Comment tree helpers. Collapsing itself is native <details>; this only adds
// click-the-bar, bulk expand/collapse and jumping between highlighted comments.
// Handlers listen on the document, so they keep working after the comments are
// swapped for a fresh copy (see refresh below).
(function () {
  if (!document.getElementById("comments")) return;
  const comments = () => document.getElementById("comments");
  const all = () => comments().querySelectorAll("details.comment");

  function revealAncestors(el) {
    const root = comments();
    for (let p = el.parentElement; p && p !== root; p = p.parentElement) {
      if (p.tagName === "DETAILS") { p.open = true; p.classList.remove("replies-hidden"); }
    }
  }

  function scrollIntoViewIfNeeded(el) {
    const r = el.getBoundingClientRect();
    // Sticky header + toolbar cover the top of the viewport; see scroll-padding-top.
    const covered = parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop) || 0;
    if (r.top < covered || r.top > window.innerHeight) el.scrollIntoView({ block: "start" });
  }

  document.addEventListener("click", (ev) => {
    const root = ev.target.closest("#comments");
    if (!root || root.closest(".inline-panel")) return; // app.js owns dynamically placed trees
    const rail = ev.target.closest("button.section-rail");
    if (rail) {
      const section = rail.closest("details.comment-section");
      section.open = false;
      scrollIntoViewIfNeeded(section);
      return;
    }
    const bar = ev.target.closest("button.bar");
    if (bar) {
      const d = bar.closest("details.comment");
      d.open = false;
      scrollIntoViewIfNeeded(d);
      return;
    }
    const btn = ev.target.closest("[data-action]");
    if (!btn) return;
    const action = btn.dataset.action;
    const clearHidden = () =>
      root.querySelectorAll(".replies-hidden").forEach((d) => d.classList.remove("replies-hidden"));
    if (action === "expand-all") {
      clearHidden();
      all().forEach((d) => (d.open = true));
      root.querySelectorAll("details.comment-section").forEach((d) => (d.open = true));
    }
    if (action === "collapse-all") { clearHidden(); all().forEach((d) => (d.open = false)); }
    if (action === "collapse-replies") {
      all().forEach((d) => {
        d.open = true;
        if (!d.parentElement.closest("details.comment")) d.classList.add("replies-hidden");
      });
    }
    if (action === "show-replies") btn.closest("details.comment").classList.remove("replies-hidden");
    if (action === "next-highlight") {
      const marks = [...root.querySelectorAll("details.comment.is-new, details.comment.is-changed")];
      const y = 10;
      const next = marks.find((m) => m.getBoundingClientRect().top > y + 1) || marks[0];
      if (next) {
        revealAncestors(next);
        next.open = true;
        next.scrollIntoView({ block: "start" });
        next.classList.add("flash-target");
        setTimeout(() => next.classList.remove("flash-target"), 1200);
      }
    }
  });

  function openHash() {
    const id = location.hash.slice(1);
    const el = id && document.getElementById(id);
    if (el && comments().contains(el)) {
      revealAncestors(el);
      if (el.tagName === "DETAILS") el.open = true;
      el.scrollIntoView({ block: "start" });
    }
  }
  window.addEventListener("hashchange", openHash);
  openHash();

  // Opening the post asked the bouncer to re-read its comments (and save its
  // article). Wait for that, then swap in the fresh post and comments. Comments
  // you opened or collapsed stay that way; a reply you're writing is never thrown away.
  const status = document.getElementById("refreshing");
  if (!status) return;
  const WAIT_MS = 60000;
  const started = Date.now();

  function say(text) {
    status.hidden = false;
    status.textContent = text;
  }

  // `withComments` false: only the post (its votes and article may be new even
  // when reading the comments failed). What you're reading is held still
  // (app.js: steady).
  async function swapFresh(withComments) {
    const r = await fetch(status.dataset.url, { credentials: "same-origin" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const doc = new DOMParser().parseFromString(await r.text(), "text/html");
    threadbnc.steady(() => swapIn(doc, withComments));
  }

  function swapIn(doc, withComments) {
    const post = document.getElementById(status.dataset.post);
    const freshPost = doc.getElementById(status.dataset.post);
    if (post && freshPost) post.replaceWith(document.adoptNode(freshPost));
    if (!withComments) return;
    const now = comments();
    const fresh = doc.getElementById("comments");
    if (!fresh) return;
    const writing = [...now.querySelectorAll("textarea")].some((t) => t.value.trim());
    if (writing) {
      say("New comments arrived. They'll show when you reload, after you post your reply.");
      return;
    }
    const kept = "details.comment[id], details.comment-section[id]";
    const shown = new Map([...now.querySelectorAll(kept)].map((d) => [d.id, d.open]));
    for (const d of fresh.querySelectorAll(kept)) if (shown.has(d.id)) d.open = shown.get(d.id);
    now.replaceWith(document.adoptNode(fresh));
  }

  async function poll() {
    let job;
    try {
      const r = await fetch(`/api/jobs/${status.dataset.job}`, { credentials: "same-origin" });
      job = r.ok ? await r.json() : null;
    } catch (e) {
      job = null;
    }
    if (job && (job.status === "done" || job.status === "failed")) {
      const failed = job.status === "failed";
      try {
        await swapFresh(!failed);
      } catch (e) {
        say("Couldn't load the fresh copy; reload to see it.");
        return;
      }
      if (failed) say(`Couldn't check for new comments (${job.error}). Showing the saved copy.`);
      return;
    }
    if (Date.now() - started > WAIT_MS) {
      say(job && job.error
        ? `Couldn't check for new comments right now (${job.error}). Showing the saved copy; it'll try again.`
        : "Still checking for new comments; reload in a moment to see them.");
      return;
    }
    setTimeout(poll, 1000);
  }
  setTimeout(poll, 700);
})();
