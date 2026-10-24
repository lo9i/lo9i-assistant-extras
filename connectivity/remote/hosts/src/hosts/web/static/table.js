// The hosts table, as the app's list-and-details pages: the search box keeps the rows that have its words, a
// column's header sorts by it (again to reverse it), and clicking a row shows its host on the right (its
// details, from app.py's details, and its name in the address, so a reload keeps it). Once the page shows, every
// host is checked again: each row comes over one WebSocket as its check ends (app.py's statuses), the search and
// the order are applied again to it, and the details read again when it's the host shown. Cells also come in
// again from htmx (a tunnel switched).
(() => {
  const table = document.getElementById("hosts");
  if (!table) return;
  const body = table.tBodies[0];
  const search = document.getElementById("filter");
  const none = body.querySelector(".no-match");
  const details = document.getElementById("details");
  let order = null; // { column, direction }
  let picked = details.querySelector("[data-details]")?.dataset.details ?? null;

  const rows = () => [...body.rows].filter((row) => row !== none);
  const value = (row, column) => row.cells[column]?.dataset.value ?? row.cells[column]?.textContent.trim() ?? "";

  function filter() {
    const words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    for (const row of rows()) {
      const text = row.textContent.toLowerCase();
      row.hidden = !words.every((word) => text.includes(word));
      if (!row.hidden) shown++;
    }
    none.hidden = shown > 0;
  }

  function sort() {
    if (!order) return;
    const { column, direction } = order;
    const sorted = rows().sort(
      (a, b) => direction * value(a, column).localeCompare(value(b, column), undefined, { numeric: true }),
    );
    for (const row of sorted) body.insertBefore(row, none);
  }

  for (const header of table.querySelectorAll("th[data-sort]")) {
    header.addEventListener("click", () => {
      const column = header.cellIndex;
      order = { column, direction: order?.column === column ? -order.direction : 1 };
      for (const other of table.tHead.rows[0].cells) other.removeAttribute("aria-sort");
      header.setAttribute("aria-sort", order.direction > 0 ? "ascending" : "descending");
      sort();
    });
  }

  function mark() {
    for (const row of rows()) row.classList.toggle("selected", row.dataset.host === picked);
  }

  function show(host) {
    const path = `hosts/${encodeURIComponent(host)}/details`;
    htmx.ajax("GET", path, { target: details, swap: "innerHTML" });
  }

  function pick(host) {
    picked = host;
    mark();
    show(host);
    const url = new URL(location.href);
    url.searchParams.set("host", host);
    history.replaceState(null, "", url);
  }

  body.addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-host]");
    if (row && !event.target.closest("a, input")) pick(row.dataset.host);
  });

  function replace(host, html) {
    const row = body.querySelector(`tr[data-host="${CSS.escape(host)}"]`);
    const template = document.createElement("template");
    template.innerHTML = html.trim();
    const fresh = template.content.firstElementChild;
    if (!row || !fresh) return;
    row.replaceWith(fresh);
    htmx.process(fresh);
    mark();
    filter();
    sort();
    if (host === picked) show(host);
  }

  function check() {
    const url = new URL(`statuses${location.search}`, document.baseURI);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(url);
    socket.addEventListener("message", (event) => {
      const { host, html } = JSON.parse(event.data);
      replace(host, html);
    });
    // Without the socket (it couldn't open, or closed early), a row still checking asks for itself.
    socket.addEventListener("close", () => {
      for (const row of body.querySelectorAll("tr[data-checking]")) {
        const path = `hosts/${encodeURIComponent(row.dataset.host)}/row`;
        htmx.ajax("GET", path, { target: row, swap: "outerHTML" });
      }
    });
  }

  search.addEventListener("input", filter);
  mark();
  check();
  document.body.addEventListener("htmx:afterSettle", () => {
    filter();
    sort();
  });
})();
