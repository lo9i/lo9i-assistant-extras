// The browser seen live (web.py, live.py): its tab as pictures, the pointer and the keys sent to it. While the
// browser isn't shown, it says how to show it and tries again every few seconds, so it appears once the
// assistant shows it.
(() => {
  const screenHere = document.body.dataset.screen === "true";
  const frame = document.getElementById("frame");
  const keys = document.getElementById("keys");
  const note = document.getElementById("note");
  const noteTitle = document.getElementById("note-title");
  const noteText = document.getElementById("note-text");
  const url = document.getElementById("url");
  const back = document.getElementById("back");
  const done = document.getElementById("done");
  const RETRY_MS = 3000;
  // Keys that only modify others, sent with them.
  const MODIFIERS = new Set(["Shift", "Control", "Alt", "Meta", "AltGraph", "CapsLock", "Fn", "OS", "Unidentified", "Process", "Dead"]);
  let ws = null;
  let live = false;
  let size = { width: 1, height: 1 };
  let finished = false;

  const say = (title, text = "") => {
    noteTitle.textContent = title;
    noteText.textContent = text;
    note.hidden = false;
    frame.hidden = keys.hidden = true;
    for (const control of [url, back, done]) control.disabled = true;
    url.value = "";
    live = false;
  };
  const off = () => {
    if (finished) say("Done", "What you signed in to stays signed in. The assistant reads those sites as you now.");
    else if (screenHere) say("The browser's window opens on this computer's screen", "When the assistant shows it, to sign in to a site.");
    else say("The browser isn't showing", "Ask the assistant to show you its browser to sign in to a site (“Show me your browser on youtube.com”). It shows here.");
  };
  const send = (message) => ws?.readyState === WebSocket.OPEN && live && ws.send(JSON.stringify(message));

  const connect = () => {
    if (document.hidden) return;
    const address = new URL("live", document.baseURI);
    address.protocol = address.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(address);
    ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.type === "frame") {
        if (!live) {
          live = true;
          finished = false;
          note.hidden = true;
          frame.hidden = keys.hidden = false;
          for (const control of [url, back, done]) control.disabled = false;
          keys.focus({ preventScroll: true });
        }
        size = { width: message.width || frame.naturalWidth || 1, height: message.height || frame.naturalHeight || 1 };
        frame.src = `data:image/jpeg;base64,${message.data}`;
      } else if (message.type === "page") {
        if (document.activeElement !== url) url.value = message.url;
        if (message.title) document.title = `${message.title} — Browser`;
      } else if (message.type === "problem") {
        url.setCustomValidity(message.text);
        url.reportValidity();
      } else if (message.type === "off" || message.type === "closed") {
        off();
      }
    };
    ws.onclose = () => {
      ws = null;
      off();
      setTimeout(connect, RETRY_MS);
    };
  };
  document.addEventListener("visibilitychange", () => !document.hidden && !ws && connect());

  // Where on the browser's tab a point of the picture is, in its CSS pixels.
  const at = (event) => {
    const box = frame.getBoundingClientRect();
    return {
      x: Math.round(((event.clientX - box.left) / box.width) * size.width),
      y: Math.round(((event.clientY - box.top) / box.height) * size.height),
    };
  };
  const BUTTONS = ["left", "middle", "right"];
  let lastDown = { time: 0, x: 0, y: 0, clicks: 0 };
  let touch = null;
  let lastMoveAt = 0;

  keys.addEventListener("pointerdown", (event) => {
    keys.focus({ preventScroll: true });
    keys.setPointerCapture(event.pointerId);
    const point = at(event);
    if (event.pointerType === "touch") {
      touch = { ...point, startX: event.clientX, startY: event.clientY, lastY: event.clientY, lastX: event.clientX, scrolled: false };
      return;
    }
    const again = event.timeStamp - lastDown.time < 400 && Math.hypot(point.x - lastDown.x, point.y - lastDown.y) < 5;
    lastDown = { time: event.timeStamp, ...point, clicks: again ? lastDown.clicks + 1 : 1 };
    send({ type: "mouse", action: "down", ...point, button: BUTTONS[event.button] || "left", clicks: lastDown.clicks });
  });
  keys.addEventListener("pointermove", (event) => {
    if (event.pointerType === "touch") {
      // A finger dragging scrolls the page, as on a phone.
      if (!touch) return;
      if (!touch.scrolled && Math.hypot(event.clientX - touch.startX, event.clientY - touch.startY) < 8) return;
      touch.scrolled = true;
      const scale = size.height / frame.getBoundingClientRect().height;
      send({ type: "mouse", action: "wheel", x: touch.x, y: touch.y, dx: (touch.lastX - event.clientX) * scale, dy: (touch.lastY - event.clientY) * scale });
      touch.lastX = event.clientX;
      touch.lastY = event.clientY;
      return;
    }
    // At most one move every 50 ms: hovering opens menus, but the page needs no more.
    if (event.timeStamp - lastMoveAt < 50) return;
    lastMoveAt = event.timeStamp;
    send({ type: "mouse", action: "move", ...at(event) });
  });
  keys.addEventListener("pointerup", (event) => {
    const point = at(event);
    if (event.pointerType === "touch") {
      if (touch && !touch.scrolled) {
        send({ type: "mouse", action: "down", ...point, button: "left", clicks: 1 });
        send({ type: "mouse", action: "up", ...point, button: "left", clicks: 1 });
      }
      touch = null;
      return;
    }
    send({ type: "mouse", action: "up", ...point, button: BUTTONS[event.button] || "left", clicks: lastDown.clicks });
  });
  keys.addEventListener("contextmenu", (event) => event.preventDefault());
  keys.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      const unit = event.deltaMode === 1 ? 40 : event.deltaMode === 2 ? size.height : 1;
      send({ type: "mouse", action: "wheel", ...at(event), dx: event.deltaX * unit, dy: event.deltaY * unit });
    },
    { passive: false },
  );

  // Characters come as typed (input), so a phone's keyboard and accents work; other keys and shortcuts are
  // pressed. Pasting (Ctrl or Cmd+V) types what was pasted.
  keys.addEventListener("keydown", (event) => {
    if (event.isComposing || MODIFIERS.has(event.key)) return;
    const shortcut = event.ctrlKey || event.metaKey;
    if (shortcut && event.key.toLowerCase() === "v") return;
    if (event.key.length === 1 && !shortcut) return;
    event.preventDefault();
    const held = [];
    if (shortcut) held.push("Control");
    if (event.altKey) held.push("Alt");
    if (event.shiftKey && event.key.length > 1) held.push("Shift");
    send({ type: "key", key: [...held, event.key].join("+") });
  });
  const typed = () => {
    if (keys.value) send({ type: "text", text: keys.value });
    keys.value = "";
  };
  keys.addEventListener("input", (event) => {
    if (event.isComposing) return;
    if (event.inputType === "deleteContentBackward" && !keys.value) send({ type: "key", key: "Backspace" });
    typed();
  });
  keys.addEventListener("compositionend", typed);

  document.getElementById("go").addEventListener("submit", (event) => {
    event.preventDefault();
    url.setCustomValidity("");
    send({ type: "go", url: url.value });
    keys.focus({ preventScroll: true });
  });
  url.addEventListener("input", () => url.setCustomValidity(""));
  back.addEventListener("click", () => send({ type: "back" }));
  done.addEventListener("click", () => {
    send({ type: "done" });
    finished = true;
    off();
  });

  say("Connecting…");
  connect();
})();
