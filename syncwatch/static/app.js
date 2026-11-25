(() => {
  const toast = (msg, bad = false) => {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.className = "show" + (bad ? " bad" : "");
    clearTimeout(el._t);
    el._t = setTimeout(() => (el.className = ""), 4000);
  };

  // Buttons with data-post="/api/..." call the API, then follow data-then.
  document.querySelectorAll("[data-post]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
      const label = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '<span class="spin"></span> Working…';
      try {
        const res = await fetch(btn.dataset.post, { method: "POST" });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(body.detail || body.error || `HTTP ${res.status}`);
        const then = btn.dataset.then;
        if (then === "run" && body.id) location.href = `/runs/${body.id}`;
        else if (then === "reload") {
          if (body.fixes) toast(body.fixes.length ? `${body.fixes.length} fixes applied` : "Nothing to fix");
          setTimeout(() => location.reload(), 900);
        } else if (then) location.href = then;
      } catch (err) {
        toast(err.message, true);
        btn.disabled = false;
        btn.innerHTML = label;
      }
    });
  });

  document.querySelectorAll("[data-scenario]").forEach((box) => {
    box.addEventListener("change", async () => {
      const res = await fetch(`/api/scenarios/${box.dataset.scenario}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: box.checked }),
      });
      if (!res.ok) { toast("Could not update scenario", true); box.checked = !box.checked; return; }
      toast(`${box.dataset.scenario} ${box.checked ? "on" : "off"}`);
      setTimeout(() => location.reload(), 500);
    });
  });

  document.querySelectorAll("tr[data-href]").forEach((tr) =>
    tr.addEventListener("click", (e) => { if (!e.target.closest("a")) location.href = tr.dataset.href; })
  );

  const hide = document.getElementById("hide-info");
  if (hide) hide.addEventListener("change", () => document.getElementById("timeline").classList.toggle("only-problems", hide.checked));

  document.querySelectorAll("pre.copy").forEach((pre) =>
    pre.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(pre.textContent.trim()); toast("Copied"); } catch { /* clipboard blocked */ }
    })
  );
})();
