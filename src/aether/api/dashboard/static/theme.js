// Applies the saved colour theme before first paint. The theme preference is the only value
// this dashboard keeps in localStorage; the API token never leaves sessionStorage.
(() => {
  "use strict";
  const KEY = "aether.theme";
  const root = document.documentElement;
  const media = window.matchMedia("(prefers-color-scheme: light)");
  root.classList.add("js");

  function stored() {
    try {
      const value = window.localStorage.getItem(KEY);
      return value === "light" || value === "dark" ? value : null;
    } catch (error) {
      return null;
    }
  }

  function effective() {
    return root.dataset.theme || (media.matches ? "light" : "dark");
  }

  function sync() {
    const button = document.getElementById("theme-toggle");
    if (button) {
      const next = effective() === "dark" ? "light" : "dark";
      button.setAttribute("aria-label", `Switch to ${next} theme`);
      button.title = `Switch to ${next} theme (t)`;
    }
  }

  function toggle() {
    const next = effective() === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try {
      window.localStorage.setItem(KEY, next);
    } catch (error) {
      /* storage disabled: the choice still applies to this page */
    }
    sync();
  }

  const saved = stored();
  if (saved) {
    root.dataset.theme = saved;
  }
  media.addEventListener("change", sync);
  document.addEventListener("DOMContentLoaded", () => {
    const button = document.getElementById("theme-toggle");
    if (button) {
      button.addEventListener("click", toggle);
    }
    sync();
  });
  window.AetherTheme = Object.freeze({ toggle, effective });
})();
