// Add-account form: when the server offers single sign-on that leads back to
// ThreadBNC, show a "Sign in with ..." button for each provider.
(function () {
  const input = document.getElementById("server");
  const box = document.getElementById("sso");
  if (!input || !box) return;
  let timer = null;
  let asked = "";

  function show(data) {
    box.replaceChildren();
    if (data.ready && data.providers.length) {
      const row = document.createElement("div");
      row.className = "sso-buttons";
      for (const p of data.providers) {
        const a = document.createElement("a");
        a.className = "sso-button";
        a.href = "/accounts/sso/start?" + new URLSearchParams({ server: data.domain, provider: p.id });
        a.textContent = "Sign in with " + p.name;
        row.append(a);
      }
      const hint = document.createElement("p");
      hint.className = "small muted";
      hint.textContent = "Or log in with a password below.";
      box.append(row, hint);
    } else if (data.note) {
      const p = document.createElement("p");
      p.className = "small muted";
      p.textContent = data.note;
      box.append(p);
    }
  }

  async function check() {
    const server = input.value.trim();
    if (server === asked) return;
    asked = server;
    if (!server.includes(".")) { box.replaceChildren(); return; }
    try {
      const r = await fetch("/accounts/sign-in-options?" + new URLSearchParams({ server }),
                            { headers: { Accept: "application/json" } });
      if (r.ok && input.value.trim() === server) show(await r.json());
    } catch (e) { /* offline or server unreachable: the password form still works */ }
  }

  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(check, 600); });
  input.addEventListener("change", check);
  if (input.value.trim()) check();
})();
