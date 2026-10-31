(() => {
  "use strict";
  const TOKEN_KEY = "aether.apiToken";
  const seconds = Number(document.body.dataset.refreshSeconds) || 10;
  const content = document.getElementById("content");
  const status = document.getElementById("api-status");

  // Both refreshes only read: the page fragment and the public health endpoint.
  async function refreshContent() {
    const url = new URL(window.location.href);
    url.searchParams.set("fragment", "true");
    const response = await fetch(url, { cache: "no-store", credentials: "same-origin" });
    if (response.ok) {
      content.innerHTML = await response.text();
    }
  }

  async function refreshHealth() {
    let label = "api: unreachable";
    let state = "down";
    try {
      const response = await fetch("/health", { cache: "no-store" });
      const body = await response.json();
      state = response.ok ? "ok" : "down";
      label = `api: ${body.status || state}`;
    } catch (error) {
      /* offline or server stopped: keep the unreachable label */
    }
    status.textContent = label;
    status.className = `badge s-${state}`;
  }

  async function refresh() {
    if (document.hidden) {
      return;
    }
    await Promise.allSettled([refreshContent(), refreshHealth()]);
  }

  refreshHealth();
  setInterval(refresh, seconds * 1000);

  const tokenInput = document.getElementById("api-token");
  const noteInput = document.getElementById("respond-note");
  const output = document.getElementById("respond-status");

  if (tokenInput) {
    tokenInput.value = sessionStorage.getItem(TOKEN_KEY) || "";
    tokenInput.addEventListener("input", () => {
      if (tokenInput.value) {
        sessionStorage.setItem(TOKEN_KEY, tokenInput.value);
      } else {
        sessionStorage.removeItem(TOKEN_KEY);
      }
    });
    document.getElementById("forget-token").addEventListener("click", () => {
      sessionStorage.removeItem(TOKEN_KEY);
      tokenInput.value = "";
      output.textContent = "Token forgotten.";
    });
  }

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-respond]");
    if (!button) {
      return;
    }
    const token = sessionStorage.getItem(TOKEN_KEY);
    if (!token) {
      output.textContent = "Enter the API token above before responding.";
      return;
    }
    const decision = button.dataset.respond;
    const note = (noteInput && noteInput.value.trim()) || `${decision} via dashboard`;
    const payload = { decision, text: note };
    if (button.dataset.personId) {
      payload.person_id = button.dataset.personId;
    }
    button.disabled = true;
    try {
      const response = await fetch(
        `/authorizations/${encodeURIComponent(button.dataset.requestId)}/respond`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
          body: JSON.stringify(payload),
        },
      );
      output.textContent = response.ok
        ? `Response "${decision}" queued.`
        : response.status === 401
          ? "The API token was rejected."
          : `Request failed (HTTP ${response.status}).`;
      if (response.ok) {
        await refreshContent();
      }
    } catch (error) {
      output.textContent = "The API is unreachable.";
    } finally {
      button.disabled = false;
    }
  });
})();
