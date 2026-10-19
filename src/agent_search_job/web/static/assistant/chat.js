// Trợ lý AI (/assistant): gửi câu hỏi, đọc SSE, vẽ tin nhắn + thẻ job + nguồn, lịch sử, hồ sơ/CV.
import { marked } from "/static/vendor/marked/marked.esm.js";
import DOMPurify from "/static/vendor/dompurify/purify.es.mjs";

const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const thread = $("#thread"), input = $("#input"), sendBtn = $("#send"), deepBtn = $("#deep");
const guest = $(".as-shell").dataset.guest === "true";
let convId = null, controller = null, stage = null;

marked.setOptions({ gfm: true, breaks: true });
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A" && /^https?:/i.test(node.getAttribute("href") || "")) {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});
// chỉ cho link http(s) và đường dẫn nội bộ (/jobs/...), chặn javascript:, data:, //host.
// Chặn img/style/form... để câu trả lời bị tiêm lệnh (JD, review, CV) không thể làm trình duyệt gọi ra ngoài (rò rỉ dữ liệu qua ![](https://...)).
const md = (text) => DOMPurify.sanitize(marked.parse(text), {
  ALLOWED_URI_REGEXP: /^(?:https?:|\/(?!\/))/i,
  FORBID_TAGS: ["img", "style", "form", "input", "textarea", "button", "iframe"],
  FORBID_ATTR: ["style"],
});

const setOrb = (state) => {
  $(".as-orb-css").dataset.state = state;
  stage?.setState(state);
};
import("./stage.js")
  .then((m) => { stage = m.createStage($("#stage"), { orbSlot: $("#orb-slot"), avatarSlot: $("#avatar-slot") }); })
  .catch(() => {}); // không có WebGL2 / lỗi import -> giữ orb CSS

const addMessage = (role, text = "") => {
  document.body.classList.add("has-chat");
  const wrap = el("div", `as-msg ${role}`);
  const bubble = el("div", "as-bubble");
  if (role === "user") bubble.textContent = text;
  else bubble.innerHTML = md(text);
  wrap.append(bubble);
  thread.append(wrap);
  wrap.scrollIntoView({ block: "end" });
  return wrap;
};

const addJobs = (wrap, jobs) => {
  if (!jobs?.length) return;
  const box = wrap.querySelector(".as-jobs") || wrap.appendChild(el("div", "as-jobs"));
  for (const j of jobs) {
    if (box.querySelector(`[data-job="${j.job_id}"]`)) continue;
    const card = el("article", "as-job");
    card.dataset.job = j.job_id;
    const title = el("a", "as-job-title", j.title);
    title.href = j.url;
    const salary = j.salary_text || (j.salary_min_vnd
      ? `${Math.round(j.salary_min_vnd / 1e6)}${j.salary_max_vnd ? "–" + Math.round(j.salary_max_vnd / 1e6) : "+"} triệu`
      : "Thoả thuận");
    card.append(title, el("div", "as-job-co", j.company_name),
      el("div", "as-job-meta", [salary, j.district, j.job_level].filter(Boolean).join(" · ")));
    if (!guest) {
      const save = el("button", "as-job-save", "Lưu việc");
      save.type = "button";
      save.addEventListener("click", async () => {
        const r = await fetch("/api/me/saved-jobs", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ job_id: j.job_id }),
        });
        save.textContent = r.ok ? "Đã lưu ✓" : "Lỗi, thử lại";
        save.disabled = r.ok;
      });
      card.append(save);
    }
    box.append(card);
  }
};

const addSources = (wrap, sources) => {
  if (!sources?.length) return;
  const list = wrap.querySelector(".as-sources") || wrap.appendChild(el("ul", "as-sources"));
  for (const s of sources) {
    if ([...list.querySelectorAll("a")].some((a) => a.href === s.url)) continue;
    const a = el("a", null, s.domain);
    a.href = s.url;
    a.title = s.title;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    const li = el("li");
    li.append(a);
    list.append(li);
  }
};

const resetChat = () => {
  controller?.abort();
  convId = null;
  thread.replaceChildren();
  document.body.classList.remove("has-chat");
};

const toggleDrawer = (id) => {
  document.querySelectorAll(".as-drawer").forEach((d) => { d.hidden = d.id !== id || !d.hidden; });
};

const loadHistory = async () => {
  const r = await fetch("/api/assistant/conversations");
  if (!r.ok) return;
  const list = $("#history-list");
  list.replaceChildren();
  const { items } = await r.json();
  if (!items.length) list.append(el("li", "as-note", "Chưa có hội thoại nào."));
  for (const c of items) {
    const li = el("li");
    const open = el("button", "as-history-open", c.title);
    open.type = "button";
    const del = el("button", "as-history-del", "✕");
    del.type = "button";
    del.setAttribute("aria-label", `Xoá hội thoại: ${c.title}`);
    open.addEventListener("click", async () => {
      const res = await fetch(`/api/assistant/conversations/${c.id}`);
      if (!res.ok) return loadHistory();
      const conv = await res.json();
      resetChat();
      convId = conv.id;
      for (const m of conv.messages) {
        const w = addMessage(m.role, m.content);
        addJobs(w, m.meta?.jobs);
        addSources(w, m.meta?.sources);
      }
      toggleDrawer(null);
    });
    del.addEventListener("click", async () => {
      await fetch(`/api/assistant/conversations/${c.id}`, { method: "DELETE" });
      if (convId === c.id) resetChat();
      loadHistory();
    });
    li.append(open, del);
    list.append(li);
  }
};

const loadProfile = async () => {
  const r = await fetch("/api/assistant/profile");
  if (!r.ok) return;
  const p = await r.json();
  $("#about").value = p.about || "";
  $("#cv-status").textContent = p.cv_filename ? `CV: ${p.cv_filename} (${p.cv_chars} ký tự)` : "Chưa có CV.";
  $("#profile-chip").textContent = p.cv_filename ? "Hồ sơ: Đã có CV" : "+ Thêm CV";
};

const send = async (text) => {
  text = text.trim();
  if (!text || controller) return;
  addMessage("user", text);
  const wrap = addMessage("assistant");
  const bubble = wrap.querySelector(".as-bubble");
  const steps = el("ul", "as-steps");
  wrap.prepend(steps);
  let raw = "", frame = 0;
  controller = new AbortController();
  sendBtn.classList.add("stop");
  sendBtn.lastElementChild.textContent = "Dừng";
  setOrb("thinking");
  try {
    const r = await fetch("/api/assistant/chat", {
      method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
      body: JSON.stringify({ conversation_id: convId, message: text, deep_think: deepBtn.getAttribute("aria-pressed") === "true" }),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "Em gặp lỗi rồi, công chúa thử lại giúp em nha 🥺");
    const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += value;
      let cut;
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, cut);
        buf = buf.slice(cut + 2);
        const name = /^event: (.+)$/m.exec(block)?.[1];
        const data = JSON.parse(/^data: (.*)$/m.exec(block)?.[1] || "{}");
        if (name === "conversation") convId = data.id;
        else if (name === "tool_start") {
          setOrb("thinking");
          const li = el("li", "run", data.label);
          li.dataset.call = data.call_id;
          steps.append(li);
        } else if (name === "tool_end") {
          const li = steps.querySelector(`[data-call="${CSS.escape(data.call_id)}"]`);
          if (li) {
            li.className = data.ok ? "ok" : "fail";
            li.textContent = `${li.textContent.replace(/…$/, "")} · ${data.summary}`;
          }
          addJobs(wrap, data.jobs);
          addSources(wrap, data.sources);
        } else if (name === "token") {
          setOrb("speaking");
          raw += data.text;
          // render markdown tối đa 1 lần mỗi frame cho mượt khi token về nhanh
          frame ||= requestAnimationFrame(() => { frame = 0; bubble.innerHTML = md(raw); });
        } else if (name === "error") throw new Error(data.message);
        else if (name === "done") loadHistory();
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") {
      const p = el("p", "as-error", e.message);
      const retry = el("button", "as-btn ghost", "Thử lại");
      retry.type = "button";
      retry.addEventListener("click", () => { p.remove(); send(text); });
      p.append(" ", retry);
      wrap.append(p);
    }
  } finally {
    cancelAnimationFrame(frame);
    bubble.innerHTML = md(raw || (controller?.signal.aborted ? "_(công chúa đã dừng)_" : ""));
    controller = null;
    sendBtn.classList.remove("stop");
    sendBtn.lastElementChild.textContent = "Gửi";
    setOrb("idle");
  }
};

$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  if (controller) return controller.abort();
  const text = input.value;
  input.value = "";
  input.style.height = "";
  send(text);
});
input.addEventListener("keydown", (e) => {
  // isComposing: không gửi khi đang gõ dấu tiếng Việt bằng bộ gõ (Telex/VNI)
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $("#composer").requestSubmit();
  }
});
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
});
deepBtn.addEventListener("click", () => deepBtn.setAttribute("aria-pressed", String(deepBtn.getAttribute("aria-pressed") !== "true")));
document.querySelectorAll(".as-sugg[data-prompt]").forEach((b) => b.addEventListener("click", () => send(b.dataset.prompt)));
$(".as-more").addEventListener("click", (e) => {
  const open = e.currentTarget.getAttribute("aria-expanded") !== "true";
  e.currentTarget.setAttribute("aria-expanded", String(open));
  $("#suggest-more").hidden = !open;
});
document.querySelectorAll("[data-action]").forEach((b) => b.addEventListener("click", () => {
  if (b.dataset.action === "new") {
    resetChat();
    toggleDrawer(null);
    input.focus();
  } else toggleDrawer(`drawer-${b.dataset.action}`);
}));
document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => { b.closest(".as-drawer").hidden = true; }));
document.querySelectorAll("[data-upload]").forEach((b) => b.addEventListener("click", () => $("#cv-file").click()));
$("#cv-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  $("#cv-status").textContent = "Em đang đọc CV…";
  const r = await fetch("/api/assistant/profile/cv", { method: "POST", body: fd });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    $("#cv-status").textContent = data.detail || "Em không đọc được file này ạ.";
    toggleDrawer("drawer-profile");
    return;
  }
  await loadProfile();
  addMessage("assistant", `Em đọc xong CV **${data.cv_filename}** rồi ạ 💐 Công chúa hỏi em việc nào hợp nhé!`);
});
$("#profile-save").addEventListener("click", async () => {
  const r = await fetch("/api/assistant/profile", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ about: $("#about").value }),
  });
  $("#cv-status").textContent = r.ok ? "Đã lưu hồ sơ ✓" : ((await r.json().catch(() => ({}))).detail || "Lưu lỗi, thử lại giúp em nhé");
});
$("#profile-delete").addEventListener("click", async () => {
  if (!confirm("Xoá toàn bộ hồ sơ và CV?")) return;
  await fetch("/api/assistant/profile", { method: "DELETE" });
  loadProfile();
});

loadHistory();
loadProfile();
