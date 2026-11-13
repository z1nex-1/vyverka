document.documentElement.classList.add("js");
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

/* Окно. Содержимое не копируется, а переносится внутрь и возвращается на место при закрытии:
   формы и обработчики остаются живыми, а без JS тот же блок виден прямо на странице. */
const win = $("#win");
let back = null;
function openWin(node, title, opts = {}) {
  if (!win || !node) return;
  closeWin();
  back = { node, parent: node.parentNode, next: node.nextSibling, focus: document.activeElement };
  $("#win-title").textContent = title || node.dataset.title || "";
  win.classList.toggle("drawer", !!opts.drawer);
  $(".win-body", win).append(node);
  node.classList.add("in-win");
  win.showModal();
  const f = $("input:not([type=hidden]), textarea, select", node);
  if (f && !opts.drawer) f.focus();
}
function closeWin() {
  if (!back) return;
  const { node, parent, next, focus } = back;
  node.classList.remove("in-win");
  parent.insertBefore(node, next);
  back = null;
  if (win.open) win.close();
  if (focus && focus.focus) focus.focus();
}
if (win) {
  win.addEventListener("close", closeWin);
  win.addEventListener("click", (e) => { if (e.target === win) win.close(); });
}
for (const d of $$("dialog")) {
  d.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) d.close(); });
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-win]");
  if (!b) return;
  e.preventDefault();
  openWin($(b.dataset.win));
});

/* Предложение открывается окном справа, а не раскрывается в ленте. */
function openOffer(li) {
  const body = $(".o-body", li);
  const title = $(".o-title", li).firstChild.textContent.trim();
  openWin(body, title, { drawer: true });
}
document.addEventListener("click", (e) => {
  const sum = e.target.closest(".offer summary");
  if (!sum) return;
  e.preventDefault();
  openOffer(sum.closest(".offer"));
});

/* Загрузка */
for (const form of $$("form.upload")) {
  form.addEventListener("submit", (e) => {
    const file = $("input[type=file]", form), text = $("textarea", form);
    if (file.files.length || text.value.trim()) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    text.placeholder = "Выберите файл ТЗ или опишите закупку здесь";
    text.focus();
  });
}
for (const input of $$(".drop input")) {
  const drop = input.closest(".drop");
  const out = $(".drop-file", drop);
  input.addEventListener("change", () => { out.textContent = input.files[0] ? "Выбран: " + input.files[0].name : ""; drop.classList.toggle("has", !!input.files[0]); });
  for (const ev of ["dragenter", "dragover"]) drop.addEventListener(ev, () => drop.classList.add("over"));
  for (const ev of ["dragleave", "drop"]) drop.addEventListener(ev, () => drop.classList.remove("over"));
}
for (const form of $$("form[data-busy]")) {
  form.addEventListener("submit", () => {
    const pol = $("input[name='policy-pick']:checked");
    if (pol && form.getAttribute("action") === "/runs") {
      let h = $("input[name='policy']", form);
      if (!h) { h = Object.assign(document.createElement("input"), { type: "hidden", name: "policy" }); form.append(h); }
      h.value = pol.value;
    }
    const btn = $("button", form);
    if (btn) { btn.disabled = true; btn.classList.add("busy"); const t = $("b", btn) || btn; t.textContent = form.dataset.busy; }
  });
}

/* Меню «Скачать» закрывается кликом мимо */
document.addEventListener("click", (e) => {
  for (const m of $$("details.menu[open]")) if (!m.contains(e.target)) m.open = false;
});

/* Ход расчёта */
const head = $(".run-bar[data-status='running']");
const log = $("#log");
if (head && log && "EventSource" in window) {
  const seen = log.children.length;
  let i = 0;
  const es = new EventSource(`/runs/${head.dataset.run}/events`);
  es.onmessage = (e) => {
    i += 1;
    if (i <= seen) return;
    const ev = JSON.parse(e.data);
    const li = document.createElement("li");
    li.className = "new";
    const t = document.createElement("time");
    t.textContent = ev.at;
    li.append(t, " " + ev.message);
    log.append(li);
    log.scrollTop = log.scrollHeight;
  };
  es.addEventListener("end", () => { es.close(); location.reload(); });
  es.onerror = () => { es.close(); setTimeout(() => location.reload(), 4000); };
}

/* Расчёт: одна позиция на экране, вкладки внутри позиции */
const items = $$(".item[data-pos]");
const links = $$(".items a[data-pos]");
function tab(item, name) {
  const btn = $(`[data-tab='${name}']`, item);
  if (!btn) return;
  for (const b of $$("[data-tab]", item)) b.setAttribute("aria-selected", String(b === btn));
  for (const p of $$("[data-panel]", item)) p.hidden = p.dataset.panel !== name;
}
function showItem(pos, push = true) {
  const item = items.find((x) => x.dataset.pos === String(pos)) || items[0];
  if (!item) return null;
  for (const x of items) x.hidden = x !== item;
  for (const a of links) a.toggleAttribute("aria-current", a.dataset.pos === item.dataset.pos);
  if (push && location.hash !== `#item-${item.dataset.pos}`) history.replaceState(null, "", `#item-${item.dataset.pos}`);
  return item;
}
if (items.length) {
  document.body.classList.add("one-item");
  for (const item of items) {
    tab(item, "in");
    for (const b of $$("[data-tab]", item)) b.addEventListener("click", () => tab(item, b.dataset.tab));
  }
  for (const a of links) a.addEventListener("click", (e) => { e.preventDefault(); showItem(a.dataset.pos); $(".item-stage").scrollTop = 0; });
  route();
  window.addEventListener("hashchange", route);
}
function route() {
  const h = location.hash;
  if (h.startsWith("#o-")) {
    const li = document.getElementById(h.slice(1));
    if (!li) return;
    const item = li.closest(".item");
    showItem(item.dataset.pos, false);
    tab(item, li.closest("[data-panel]").dataset.panel);
    openOffer(li);
    history.replaceState(null, "", `#item-${item.dataset.pos}`);
  } else {
    showItem(h.startsWith("#item-") ? h.slice(6) : (links[0] && links[0].dataset.pos), false);
  }
}

/* Настройки и справка: слева разделы, справа один раздел */
const secs = $$(".set-sec, .guide-sec");
const navs = $$(".set-nav a, .guide-nav a");
function showSec(id) {
  let sec = secs.find((s) => s.id === id);
  if (!sec && id) { const el = document.getElementById(id); sec = el && el.closest(".set-sec, .guide-sec"); }
  sec = sec || secs[0];
  for (const s of secs) s.hidden = s !== sec;
  for (const a of navs) a.toggleAttribute("aria-current", a.getAttribute("href") === "#" + sec.id);
  const el = id && document.getElementById(id);
  if (el && el !== sec) { el.classList.add("flash"); el.scrollIntoView({ block: "center" }); }
  else { const st = $(".stage"); if (st) requestAnimationFrame(() => { st.scrollTop = 0; }); }
}
if (secs.length) {
  document.body.classList.add("one-sec");
  showSec(location.hash.slice(1));
  window.addEventListener("hashchange", () => showSec(location.hash.slice(1)));
}
const toast = $(".toast");
if (toast && !toast.classList.contains("warn")) setTimeout(() => toast.classList.add("gone"), 4000);

/* Клавиши */
document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.target.closest("input, textarea, select, [contenteditable]") || (win && win.open)) return;
  const k = e.key.toLowerCase();
  const go = { n: "/", "т": "/", r: "/runs", "к": "/runs", s: "/settings", "ы": "/settings" }[k];
  if (go) { location.href = go; return; }
  if (e.key === "?") { const kw = $("#keys"); if (items.length && kw) kw.showModal(); else location.href = "/guide"; return; }
  if (!items.length) return;
  const cur = items.findIndex((x) => !x.hidden);
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    const next = items[cur + (e.key === "ArrowDown" ? 1 : -1)];
    if (next) { e.preventDefault(); showItem(next.dataset.pos); }
  } else if (/^[1-5]$/.test(e.key)) {
    const b = $$("[data-tab]", items[cur])[Number(e.key) - 1];
    if (b) b.click();
  } else if (k === "j" || k === "о") {
    const b = $("[data-win='#log-src']"); if (b) b.click();
  }
});
