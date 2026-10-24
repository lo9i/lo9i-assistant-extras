// A host's terminal: xterm.js attached to lo9i's Herdr session on the host, over a WebSocket (web/terminal.py
// says what goes over it). When the connection drops it connects again on its own, to the same session, which
// kept running on the host meanwhile.
(() => {
  const box = document.getElementById("terminal");
  const host = box.dataset.host;
  const state = document.getElementById("state");
  const notice = document.getElementById("notice");
  const noticeTitle = document.getElementById("notice-title");
  const noticeText = document.getElementById("notice-text");
  const noticeActions = document.getElementById("notice-actions");
  // Seconds between attempts to reconnect, the last one repeated.
  const RETRY_SECONDS = [1, 2, 4, 8, 15];

  const term = new Terminal({
    cursorBlink: true,
    fontSize: 14,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, "DejaVu Sans Mono", monospace',
    // Its card shows through, as the app's surfaces do.
    allowTransparency: true,
    theme: { background: "rgba(0, 0, 0, 0)", foreground: "#ffffff" },
    // Herdr takes the mouse; option-drag selects in the browser instead, for ⌘C.
    macOptionClickForcesSelection: true,
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(box);
  fit.fit();
  new ResizeObserver(() => fit.fit()).observe(box);

  // Herdr copies what's selected with the mouse by sending it to the terminal (OSC 52, "c;<base64>"), as it
  // would to a terminal app; xterm.js leaves that to the page. When the browser won't let a page write to the
  // clipboard on its own (Safari, a page not on https), ⌘C copies it instead.
  let copied = "";
  term.parser.registerOscHandler(52, (data) => {
    const encoded = data.slice(data.indexOf(";") + 1);
    if (!encoded || encoded === "?") return true; // A program asking what's in the clipboard: not answered.
    try {
      copied = new TextDecoder().decode(Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0)));
    } catch {
      return true;
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(copied).catch(copyWithCommand);
    } else {
      copyWithCommand();
    }
    return true;
  });

  function copyWithCommand() {
    const area = document.createElement("textarea");
    area.value = copied;
    area.setAttribute("readonly", "");
    area.style.cssText = "position: fixed; opacity: 0; pointer-events: none;";
    document.body.append(area);
    area.select();
    let done = false;
    try {
      done = document.execCommand("copy");
    } catch {}
    area.remove();
    term.focus();
    if (!done) state.textContent = "Selected: ⌘C copies it";
  }

  // ⌘C with nothing selected in xterm.js copies what Herdr last copied (before xterm.js's own handler runs).
  document.addEventListener(
    "copy",
    (event) => {
      if (term.hasSelection() || !copied || !event.clipboardData) return;
      event.clipboardData.setData("text/plain", copied);
      event.preventDefault();
      event.stopImmediatePropagation();
      if (state.textContent.startsWith("Selected")) state.textContent = "Connected";
    },
    true,
  );

  const encoder = new TextEncoder();
  let socket = null;
  let attempts = 0;
  let retryTimer = null;

  function address(path) {
    const url = new URL(`hosts/${encodeURIComponent(host)}/${path}`, document.baseURI);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    return url;
  }

  function connect() {
    clearTimeout(retryTimer);
    retryTimer = null;
    hideNotice();
    state.textContent = attempts ? "Reconnecting…" : "Connecting…";
    const url = address("terminal");
    url.searchParams.set("cols", term.cols);
    url.searchParams.set("rows", term.rows);
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    let ended = null;
    let shown = false;
    ws.onmessage = (event) => {
      if (typeof event.data === "string") {
        ended = JSON.parse(event.data);
        return;
      }
      if (!shown) {
        // Herdr draws the whole screen when it attaches; what was there before is the last connection's.
        shown = true;
        attempts = 0;
        term.reset();
        state.textContent = "Connected";
        term.focus();
      }
      term.write(new Uint8Array(event.data));
    };
    ws.onclose = () => {
      if (socket !== ws) return;
      socket = null;
      closed(ended);
    };
    socket = ws;
  }

  function closed(ended) {
    if (!ended || ended.reason === "unreachable") return retry(ended?.message ?? "The connection was lost.");
    if (ended.reason === "no-herdr") return offerInstall();
    state.textContent = ended.reason === "detached" ? "Detached" : "Not connected";
    const again = button(ended.reason === "detached" ? "Attach again" : "Try again", connect, "primary");
    const title = ended.reason === "detached" ? "Left the session" : `Can't connect to ${host}`;
    showNotice(title, ended.message, [again, link("Back to hosts", "./")]);
  }

  function retry(why) {
    const seconds = RETRY_SECONDS[Math.min(attempts, RETRY_SECONDS.length - 1)];
    attempts++;
    state.textContent = "Disconnected";
    showNotice(
      "Connection lost",
      `${why} Reconnecting in ${seconds} s. What runs on ${host} keeps running.`,
      [button("Reconnect now", connect)],
      true,
    );
    retryTimer = setTimeout(connect, seconds * 1000);
  }

  function offerInstall() {
    state.textContent = "Herdr missing";
    const install = button("Install Herdr", () => installHerdr(install), "primary");
    showNotice(
      `Herdr isn't installed on ${host}`,
      `This terminal runs inside Herdr on ${host}, which keeps it running when the connection drops, so you ` +
        `come back to it as you left it. Installing puts Herdr in ~/.local/bin on ${host}, with its official ` +
        `installer from herdr.dev.`,
      [install, link("Back to hosts", "./")],
    );
  }

  // Over a WebSocket: the download can take longer than lo9i waits for a request.
  function installHerdr(install) {
    install.disabled = true;
    install.textContent = "Installing…";
    const ws = new WebSocket(address("install"));
    let answer = null;
    ws.onmessage = (event) => (answer = JSON.parse(event.data));
    ws.onclose = () => {
      if (answer?.ok) return connect();
      noticeText.textContent = answer?.message ?? "The installation didn't finish. Try again.";
      install.disabled = false;
      install.textContent = "Try again";
    };
  }

  function showNotice(title, text, actions, banner = false) {
    noticeTitle.textContent = title;
    noticeText.textContent = text;
    noticeActions.replaceChildren(...actions);
    notice.classList.toggle("banner", banner);
    notice.hidden = false;
  }

  function hideNotice() {
    notice.hidden = true;
  }

  function button(label, onClick, kind = "") {
    const element = document.createElement("button");
    element.className = `btn btn--sm ${kind ? `btn--${kind}` : "btn--neutral"}`;
    element.textContent = label;
    element.addEventListener("click", onClick);
    return element;
  }

  function link(label, href) {
    const element = document.createElement("a");
    element.className = "btn btn--sm btn--neutral";
    element.textContent = label;
    element.href = href;
    return element;
  }

  const open = () => socket?.readyState === WebSocket.OPEN;
  term.onData((data) => open() && socket.send(encoder.encode(data)));
  term.onBinary((data) => open() && socket.send(Uint8Array.from(data, (c) => c.charCodeAt(0))));
  term.onResize(({ cols, rows }) => open() && socket.send(JSON.stringify({ type: "resize", cols, rows })));

  // Back online, or back to this tab after a sleep: no need to wait for the next attempt.
  const soon = () => retryTimer && connect();
  window.addEventListener("online", soon);
  document.addEventListener("visibilitychange", () => document.visibilityState === "visible" && soon());

  connect();
})();
