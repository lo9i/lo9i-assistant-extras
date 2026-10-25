// The assistant's web app says its theme, which this page can't read (it's its own origin): in the page's first
// address (?theme=light) and, when it changes, in a message to its frame (lo9i's core/mcp/install.py). Kept for
// the page's next pages, which come without it.
(() => {
  const KEY = "lo9i.page-theme";
  const apply = (theme) => {
    if (theme === "light") document.documentElement.dataset.theme = "light";
    else delete document.documentElement.dataset.theme;
  };
  const keep = (theme) => {
    try {
      localStorage.setItem(KEY, theme);
    } catch {
      // This page only.
    }
  };
  let theme = new URLSearchParams(location.search).get("theme");
  if (theme) keep(theme);
  else {
    try {
      theme = localStorage.getItem(KEY);
    } catch {
      theme = null;
    }
  }
  apply(theme);
  addEventListener("message", (event) => {
    if (event.source !== parent || event.data?.type !== "lo9i:theme") return;
    apply(event.data.theme);
    keep(event.data.theme);
  });
})();
