// yt-dlp Cast web interface. No libraries: it runs on a box with no internet
// access to CDNs and has to stay small.
"use strict";

const TEXT = {
  en: {
    tabPlayer: "Player", tabLog: "Log", playNow: "Play", addToQueue: "Add to queue",
    urlPlaceholder: "Link to a video or playlist (YouTube or any site yt-dlp supports)",
    stopped: "Nothing is playing", playing: "Playing", paused: "Paused", buffering: "Buffering…",
    live: "Live", queue: "Queue", previous: "Previous", back10: "Back 10 s", pause: "Pause / resume",
    forward30: "Forward 30 s", next: "Next", stop: "Stop", filter: "Filter", follow: "Follow",
    download: "Download", resolving: "Resolving the link…", queued: "Added to the queue: {}",
    started: "Playing.", failed: "Could not play: {}", sent: "Sent to Kodi…", notLink: "That is not a web link.",
    logHint: "This add-on's lines and InputStream Adaptive's. More detail: Settings → Diagnostics (Expert level).",
    pinTitle: "PIN", pinText: "Enter the PIN shown in the add-on's settings, under Web interface.", pinOk: "OK",
    pinWrong: "Wrong PIN.", pinLocked: "Too many attempts, wait {} s.", noConnection: "No connection to Kodi.",
    all: "all", problems: "problems", source: "source",
  },
  pl: {
    tabPlayer: "Odtwarzanie", tabLog: "Log", playNow: "Odtwórz", addToQueue: "Dodaj do kolejki",
    urlPlaceholder: "Link do filmu lub playlisty (YouTube albo inny serwis obsługiwany przez yt-dlp)",
    stopped: "Nic nie gra", playing: "Gra", paused: "Pauza", buffering: "Buforowanie…",
    live: "Na żywo", queue: "Kolejka", previous: "Poprzedni", back10: "10 s wstecz", pause: "Pauza / wznów",
    forward30: "30 s naprzód", next: "Następny", stop: "Zatrzymaj", filter: "Filtr", follow: "Śledź",
    download: "Pobierz", resolving: "Rozwiązywanie linku…", queued: "Dodano do kolejki: {}",
    started: "Gra.", failed: "Nie udało się odtworzyć: {}", sent: "Wysłano do Kodi…", notLink: "To nie jest link WWW.",
    logHint: "Linie tej wtyczki i InputStream Adaptive. Więcej szczegółów: Ustawienia → Diagnostyka (poziom Ekspert).",
    pinTitle: "PIN", pinText: "Wpisz PIN widoczny w ustawieniach wtyczki, w zakładce Interfejs WWW.", pinOk: "OK",
    pinWrong: "Zły PIN.", pinLocked: "Za dużo prób, poczekaj {} s.", noConnection: "Brak połączenia z Kodi.",
    all: "wszystko", problems: "problemy", source: "źródło",
  },
};
const LANG = (navigator.language || "en").toLowerCase().startsWith("pl") ? "pl" : "en";
const t = (key, value) => (TEXT[LANG][key] || TEXT.en[key] || key).replace("{}", value ?? "");
const $ = (id) => document.getElementById(id);

// -- API ------------------------------------------------------------------------

class Unauthorized extends Error {}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options });
  if (response.status === 401) throw new Unauthorized();
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw Object.assign(new Error(data.error || response.statusText), { data });
  return data;
}

// -- player -----------------------------------------------------------------------

let pendingRequest = null;
let dragging = false;
let lastStatus = { state: "stopped" };
let queueShownFor = "";

function clock(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(seconds / 3600), m = Math.floor(seconds / 60) % 60, s = seconds % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
}

function showRequest(text, kind) {
  const line = $("request");
  line.hidden = !text;
  line.textContent = text || "";
  line.className = "request" + (kind ? " " + kind : "");
}

function render(status) {
  lastStatus = status;
  const active = status.state !== "stopped";
  $("now").classList.toggle("idle", !active);
  $("state").textContent = !active ? t("stopped")
    : status.buffering ? t("buffering") : status.live ? t("live") : t(status.state);
  $("title").textContent = active ? status.title : "";
  const site = $("site");
  site.textContent = "";
  if (active && (status.site || status.source)) {
    site.append((status.site || "") + (status.source ? " · " : ""));
    if (status.source) {
      const link = document.createElement("a");
      link.href = status.source;
      link.target = "_blank";
      link.rel = "noreferrer noopener";
      link.textContent = t("source");
      site.append(link);
    }
  }
  const thumb = $("thumb");
  thumb.hidden = !(active && status.thumbnail);
  if (active && status.thumbnail && thumb.src !== status.thumbnail) thumb.src = status.thumbnail;

  const seek = $("seek");
  const seekable = active && !status.live && status.duration > 0;
  seek.disabled = !seekable;
  seek.max = seekable ? Math.floor(status.duration) : 0;
  if (!dragging) {
    seek.value = active ? Math.floor(status.time) : 0;
    $("time").textContent = clock(active ? status.time : 0);
  }
  $("duration").textContent = status.live ? t("live") : clock(active ? status.duration : 0);
  document.querySelectorAll(".controls button").forEach((button) => { button.disabled = !active; });
  const inQueue = active && status.playlist === 1;
  document.querySelector('[data-action="previous"]').disabled = !(inQueue && status.position > 0);
  document.querySelector('[data-action="next"]').disabled = !(inQueue && status.position < status.queue_size - 1);
  ["back", "forward"].forEach((a) => { document.querySelector(`[data-action="${a}"]`).disabled = !seekable; });

  if (pendingRequest && status.request) followRequest(status.request);

  const queueKey = status.queue_size + ":" + status.position + ":" + status.playlist;
  if (queueKey !== queueShownFor) {
    queueShownFor = queueKey;
    loadQueue();
  }
}

function followRequest(state) {
  if (state === "pending") return showRequest(t("resolving"));
  if (state.startsWith("queued:")) {
    const count = state.split(":")[1].trim();
    return showRequest(count === "1" ? t("resolving") : t("queued", count));
  }
  if (state === "ok") {
    pendingRequest = null;
    showRequest(t("started"), "ok");
    setTimeout(() => showRequest(""), 4000);
    return;
  }
  if (state.startsWith("failed:")) {
    pendingRequest = null;
    showRequest(t("failed", state.slice(7).trim()), "error");
  }
}

async function loadQueue() {
  try {
    const queue = await api("/api/queue");
    const list = $("queue-items");
    list.textContent = "";
    queue.items.forEach((title, index) => {
      const item = document.createElement("li");
      item.textContent = title;
      if (index === queue.position) item.className = "current";
      item.addEventListener("click", () => api("/api/queue/goto", { index }).catch(handleError));
      list.append(item);
    });
    $("queue").hidden = queue.items.length < 2;
  } catch (error) {
    handleError(error);
  }
}

async function poll() {
  try {
    render(await api("/api/status" + (pendingRequest ? "?request=" + encodeURIComponent(pendingRequest) : "")));
  } catch (error) {
    handleError(error);
  }
  setTimeout(poll, document.hidden ? 5000 : 1000);
}

async function play(url, mode) {
  url = url.trim();
  if (!/^https?:\/\//i.test(url)) return showRequest(t("notLink"), "error");
  showRequest(t("sent"));
  try {
    const answer = await api("/api/play", { url, mode });
    pendingRequest = answer.request;
    if (mode === "now") $("url").value = "";
  } catch (error) {
    handleError(error);
    if (!(error instanceof Unauthorized)) showRequest(t("failed", error.message), "error");
  }
}

function control(action, seconds) {
  return api("/api/control", seconds === undefined ? { action } : { action, seconds }).catch(handleError);
}

// -- log ----------------------------------------------------------------------------

const AREAS = ["all", "problems", "resolve", "manifest", "player", "cast", "http", "updates", "web", "ISA"];
const LINE = /^(\S+) (\S+) T:\d+\s+(\w+) <[^>]+>: (.*)$/;
let logOffset = null;
let logLines = [];
let area = "all";

// A line without Kodi's timestamp continues the entry above (a traceback):
// it takes that entry's level and area, so filters keep the two together.
function parseLines(raws, previous) {
  return raws.map((raw) => {
    const match = LINE.exec(raw);
    const line = match
      ? { time: match[2].slice(0, 8), level: match[3].toLowerCase(),
          message: match[4].replace("[plugin.video.ytdlpcast] ", "") }
      : { time: "", level: previous ? previous.level : "info", message: raw };
    line.key = match || !previous ? line.message : previous.key;
    previous = line;
    return line;
  });
}

function visible(line) {
  if (area === "problems" && !["warning", "error", "fatal"].includes(line.level)) return false;
  if (area === "ISA" && !line.key.includes("inputstream.adaptive")) return false;
  if (!["all", "problems", "ISA"].includes(area) && !line.key.startsWith(area)) return false;
  const filter = $("log-filter").value.trim().toLowerCase();
  return !filter || line.message.toLowerCase().includes(filter);
}

function renderLog() {
  const box = $("log-lines");
  const fragment = document.createDocumentFragment();
  logLines.filter(visible).forEach((line) => {
    const row = document.createElement("div");
    row.className = line.level;
    const time = document.createElement("span");
    time.className = "t";
    time.textContent = line.time ? line.time + " " : "";
    row.append(time, line.message);
    fragment.append(row);
  });
  box.textContent = "";
  box.append(fragment);
  if ($("follow").checked) box.scrollTop = box.scrollHeight;
}

async function pollLog() {
  if ($("log").classList.contains("active") && !document.hidden) {
    try {
      const chunk = await api("/api/log" + (logOffset === null ? "" : "?offset=" + logOffset));
      if (chunk.reset) logLines = [];
      logOffset = chunk.offset;
      if (chunk.lines.length || chunk.reset) {
        logLines = logLines.concat(parseLines(chunk.lines, logLines[logLines.length - 1])).slice(-5000);
        renderLog();
      }
    } catch (error) {
      handleError(error);
    }
  }
  setTimeout(pollLog, 2000);
}

function downloadLog() {
  const text = logLines.filter(visible).map((l) => (l.time ? l.time + " " : "") + l.level + " " + l.message).join("\n");
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([text + "\n"], { type: "text/plain" }));
  link.download = "ytdlpcast-" + new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-") + ".log";
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

// -- PIN --------------------------------------------------------------------------------

function handleError(error) {
  if (error instanceof Unauthorized) {
    $("pin").hidden = false;
    $("pin-input").focus();
  } else if (error instanceof TypeError) {
    $("state").textContent = t("noConnection");
  }
}

async function login(event) {
  event.preventDefault();
  try {
    await api("/api/login", { pin: $("pin-input").value });
    $("pin").hidden = true;
    $("pin-error").hidden = true;
    startPendingPlay();
  } catch (error) {
    const wait = error.data && error.data.retry_after;
    $("pin-error").textContent = wait ? t("pinLocked", wait) : t("pinWrong");
    $("pin-error").hidden = false;
    $("pin-input").select();
  }
}

// -- wiring ---------------------------------------------------------------------------

let pendingPlay = new URLSearchParams(location.search).get("play");

function startPendingPlay() {
  if (!pendingPlay) return;
  const url = pendingPlay;
  pendingPlay = null;
  history.replaceState(null, "", location.pathname);
  $("url").value = url;
  play(url, "now");
}

function init() {
  document.documentElement.lang = LANG;
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => { el.placeholder = t(el.dataset.i18nPlaceholder); });
  document.querySelectorAll("[data-i18n-title]").forEach((el) => { el.title = t(el.dataset.i18nTitle); });

  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    document.querySelectorAll(".tab, .panel").forEach((el) => el.classList.remove("active"));
    tab.classList.add("active");
    $(tab.dataset.tab).classList.add("active");
    if (tab.dataset.tab === "log") renderLog();
  }));

  let mode = "now";
  document.querySelectorAll("#play-form button").forEach((button) =>
    button.addEventListener("click", () => { mode = button.dataset.mode; }));
  $("play-form").addEventListener("submit", (event) => {
    event.preventDefault();
    play($("url").value, mode);
  });

  const seek = $("seek");
  seek.addEventListener("input", () => { dragging = true; $("time").textContent = clock(seek.value); });
  seek.addEventListener("change", () => { dragging = false; control("seek", Number(seek.value)); });

  document.querySelectorAll(".controls button").forEach((button) => button.addEventListener("click", () => {
    const action = button.dataset.action;
    if (action === "toggle") return control(lastStatus.state === "paused" ? "resume" : "pause");
    if (action === "back") return control("seek", Math.max(0, lastStatus.time - 10));
    if (action === "forward") return control("seek", Math.min(lastStatus.duration, lastStatus.time + 30));
    control(action);
  }));

  const chips = $("chips");
  AREAS.forEach((name) => {
    const chip = document.createElement("button");
    chip.textContent = TEXT[LANG][name] || name;
    chip.className = name === area ? "on" : "";
    chip.addEventListener("click", () => {
      area = name;
      chips.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === chip));
      renderLog();
    });
    chips.append(chip);
  });
  $("log-filter").addEventListener("input", renderLog);
  $("download").addEventListener("click", downloadLog);
  $("pin-form").addEventListener("submit", login);

  api("/api/auth").then((auth) => {
    if (auth.pin_required && !auth.authorized) handleError(new Unauthorized());
    else startPendingPlay();
  }).catch(handleError);
  poll();
  pollLog();
}

init();
