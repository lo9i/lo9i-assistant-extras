// The hosts table, as Watchlist's tables look: the search box keeps the rows that have its words, a column's
// header sorts by it (again to reverse it), and clicking a row opens its host. Each row is checked after the
// page loads and comes in again (htmx), so the search and the order are applied again then.
(() => {
  const table = document.getElementById("hosts");
  if (!table) return;
  const body = table.tBodies[0];
  const search = document.getElementById("filter");
  const none = body.querySelector(".no-match");
  let order = null; // { column, direction }

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

  body.addEventListener("click", (event) => {
    if (event.target.closest("a, button, input")) return;
    const row = event.target.closest("tr[data-href]");
    if (row) location.href = new URL(row.dataset.href, document.baseURI).href;
  });

  search.addEventListener("input", filter);
  document.body.addEventListener("htmx:afterSettle", () => {
    filter();
    sort();
  });
})();
