// Comment tree helpers. Collapsing itself is native <details>; this only adds
// click-the-bar, bulk expand/collapse and jumping between highlighted comments.
(function () {
  const root = document.getElementById("comments");
  if (!root) return;
  const all = () => root.querySelectorAll("details.comment");

  function revealAncestors(el) {
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

  root.addEventListener("click", (ev) => {
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
    if (action === "expand-all") { clearHidden(); all().forEach((d) => (d.open = true)); }
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
    if (el && root.contains(el)) {
      revealAncestors(el);
      if (el.tagName === "DETAILS") el.open = true;
      el.scrollIntoView({ block: "start" });
    }
  }
  window.addEventListener("hashchange", openHash);
  openHash();
})();
