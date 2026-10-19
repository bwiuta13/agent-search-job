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
let convId = null, controller = null, stage = null, opener = null;
// Bám đáy khi đang stream: chỉ cuộn theo khi công chúa đang ở gần cuối hội thoại, đang kéo lên đọc thì để yên.
let stick = true;
thread.addEventListener("scroll", () => { stick = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 60; });
const follow = () => { if (stick) thread.scrollTop = thread.scrollHeight; };
// Chỉ hiện thông báo lỗi khi nó là chuỗi do API của mình trả về (detail / SSE error.message); còn lại (mạng, JSON hỏng, detail dạng mảng 422) dùng câu tiếng Việt chung.
const ERR_GENERIC = "Em gặp lỗi rồi, công chúa thử lại giúp em nha 🥺";
class ApiError extends Error {}
const apiText = (v, fallback) => (typeof v === "string" && v ? v : fallback);

marked.setOptions({ gfm: true, breaks: true });
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A" && /^https?:/i.test(node.getAttribute("href") || "")) {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});
// Allowlist (không phải denylist): chỉ giữ thẻ chữ/bảng/link, KHÔNG có img/video/audio/svg/style... để câu trả lời bị tiêm lệnh
// (JD, review, CV) không thể làm trình duyệt tự gọi ra ngoài (rò rỉ dữ liệu qua ![](https://...), <video poster>, <table background>...).
// Link chỉ http(s) và đường dẫn nội bộ (/jobs/...), chặn javascript:, data:, //host và /\host (trình duyệt coi \ như /). start/align là số/chữ, không phải URL.
const md = (text) => DOMPurify.sanitize(marked.parse(text), {
  ALLOWED_TAGS: ["p", "br", "strong", "em", "b", "i", "del", "code", "pre", "blockquote", "ul", "ol", "li", "a",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "table", "thead", "tbody", "tr", "th", "td"],
  ALLOWED_ATTR: ["href", "title", "start", "align"],
  ADD_URI_SAFE_ATTR: ["start", "align"],
  ALLOWED_URI_REGEXP: /^(?:https?:|\/(?![\/\\]))/i,
});

const setOrb = (state) => {
  $(".as-orb-css").dataset.state = state;
  stage?.setState(state);
};
// Thỏ mascot 3D chỉ hiện từ 1320px (ô nhân vật ẩn dưới mức này, kể cả mobile) -> chỉ tải GLB 2.8MB khi màn đủ rộng;
// màn hẹp mở rộng ra sau đó thì tải lúc đó. Tải lỗi -> bỏ qua (ô nhân vật trống), lần đổi kích thước sau thử lại.
const wideForCharacter = matchMedia("(min-width: 1320px)");
let characterLoading = false;
const loadCharacter = () => {
  if (!stage || characterLoading || !wideForCharacter.matches) return;
  characterLoading = true;
  stage.setCharacter("/static/assistant/character.glb").catch((e) => {
    characterLoading = false;
    console.warn("Không tải được nhân vật 3D", e);
  });
};
wideForCharacter.addEventListener("change", loadCharacter);
import("./stage.js")
  .then((m) => {
    stage = m.createStage($("#stage"), { orbSlot: $("#orb-slot"), avatarSlot: $("#avatar-slot") });
    loadCharacter();
  })
  .catch(() => {}); // không có WebGL2 / lỗi import -> giữ orb CSS, không có nhân vật

// Thỏ nói: bong bóng chat cạnh nhân vật 3D — báo "đang nghĩ" và câu báo hoàn thành
// "Thưa Công Chúa, em đã làm xong yêu cầu của Công Chúa rồi ạ". Bong bóng là DOM thường
// (không vẽ trong canvas) để chọn chữ/chuột được và không tốn thêm render 3D.
const bubbleTimer = { id: 0 };
const bunnySay = (text, { sticky = false } = {}) => {
  const say = $("#bunny-say");
  if (!say) return;
  clearTimeout(bubbleTimer.id);
  say.textContent = text;
  say.classList.add("on");
  if (!sticky) bubbleTimer.id = setTimeout(() => say.classList.remove("on"), 6000);
};

// Công chúa kéo chuột trên ô nhân vật -> xoay nhân vật (pointer-events từ CSS bật trên .as-avatar-slot)
(() => {
  const slot = $("#avatar-slot");
  if (!slot) return;
  let down = false, lastX = 0, moved = 0;
  slot.addEventListener("pointerdown", (e) => {
    down = true; moved = 0; lastX = e.clientX;
    slot.setPointerCapture(e.pointerId);
  });
  slot.addEventListener("pointermove", (e) => {
    if (!down) return;
    const dx = e.clientX - lastX;
    lastX = e.clientX; moved += Math.abs(dx);
    stage?.rotate(dx);
  });
  slot.addEventListener("pointerup", () => {
    down = false;
    if (moved < 4) stage?.poke(); // chạm nhẹ (không kéo) -> thỏ nhảy 1 nhịp
  });
})();

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

// Thẻ job/nguồn đã được server bỏ trùng và giới hạn số lượng (tool_end và meta lưu giống nhau) -> vẽ thẳng.
// Thẻ, nguồn nằm TRÊN bong bóng chữ: chữ đang stream luôn ở đáy tin nhắn, chỗ mắt (và follow()) đang nhìn.
const addJobs = (wrap, jobs) => {
  if (!jobs?.length) return;
  const box = wrap.querySelector(".as-jobs") || wrap.insertBefore(el("div", "as-jobs"), wrap.querySelector(".as-sources, .as-bubble"));
  // không có salary_text thì ghép từ số: lẻ tới 0,1 triệu, dưới 1 triệu tính bằng nghìn
  const vnd = (v) => (v >= 1e6 ? `${(v / 1e6).toLocaleString("vi-VN", { maximumFractionDigits: 1 })} triệu` : `${Math.round(v / 1e3)} nghìn`);
  for (const j of jobs) {
    const card = el("article", "as-job");
    const title = el("a", "as-job-title", j.title);
    title.href = j.url;
    const lo = j.salary_min_vnd, hi = j.salary_max_vnd;
    const salary = j.salary_text || (lo && hi ? `${vnd(lo)} – ${vnd(hi)}` : lo ? `Từ ${vnd(lo)}` : hi ? `Đến ${vnd(hi)}` : "Thoả thuận");
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
  const list = wrap.querySelector(".as-sources") || wrap.insertBefore(el("ul", "as-sources"), wrap.querySelector(".as-bubble"));
  for (const s of sources) {
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
  $("#bunny-say")?.classList.remove("on"); // chat mới: thỏ im, chờ câu hỏi mới
};

// Mở 1 ngăn (id) hoặc đóng hết (null). Mở: focus vào ngăn, nhớ nút đã mở; đóng: trả focus về nút đó.
// Truyền trigger vì Safari không focus nút khi bấm chuột (activeElement lúc đó là body).
const toggleDrawer = (id, trigger) => {
  let shown = null;
  for (const d of document.querySelectorAll(".as-drawer")) {
    d.hidden = d.id !== id || !d.hidden;
    if (!d.hidden) shown = d;
  }
  for (const b of document.querySelectorAll("[aria-controls^='drawer-']")) {
    b.setAttribute("aria-expanded", String(b.getAttribute("aria-controls") === shown?.id));
  }
  if (shown) {
    opener = trigger || opener || document.activeElement;
    shown.focus();
  } else if (opener) {
    opener.focus();
    opener = null;
  }
};

const loadHistory = async () => {
  const r = await fetch("/api/assistant/conversations").catch(() => null);
  if (!r?.ok) return;
  const list = $("#history-list");
  list.replaceChildren();
  $("#history-status").textContent = "";
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
      const res = await fetch(`/api/assistant/conversations/${c.id}`).catch(() => null);
      if (!res) {
        $("#history-status").textContent = "Em chưa mở được hội thoại, công chúa thử lại giúp em nhé";
        return;
      }
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
      const res = await fetch(`/api/assistant/conversations/${c.id}`, { method: "DELETE" }).catch(() => null);
      if (!res?.ok && res?.status !== 404) { // 404: đã hết hạn/bị xoá từ trước -> coi như xoá xong
        $("#history-status").textContent = "Em chưa xoá được hội thoại, công chúa thử lại giúp em nhé";
        return;
      }
      if (convId === c.id) resetChat();
      await loadHistory();
      $("#drawer-history").focus(); // nút vừa bấm đã bị xoá khỏi danh sách: giữ focus trong ngăn
    });
    li.append(open, del);
    list.append(li);
  }
};

const loadProfile = async () => {
  const r = await fetch("/api/assistant/profile").catch(() => null);
  if (!r?.ok) return;
  const p = await r.json();
  $("#about").value = p.about || "";
  // khách không upload được CV (server trả 403) -> nhắc đăng nhập thay vì "Thêm CV"
  $("#cv-status").textContent = p.cv_filename ? `CV: ${p.cv_filename} (${p.cv_chars} ký tự)`
    : guest ? "Đăng nhập để tải CV (PDF/DOCX) cho em tư vấn kỹ hơn nhé." : "Chưa có CV.";
  $("#profile-chip").textContent = p.cv_filename ? "Hồ sơ: Đã có CV" : guest ? "Hồ sơ" : "+ Thêm CV";
};

const send = async (text) => {
  text = text.trim();
  if (!text || controller) return;
  const ask = addMessage("user", text);
  const wrap = addMessage("assistant");
  const bubble = wrap.querySelector(".as-bubble");
  const steps = el("ul", "as-steps");
  wrap.prepend(steps);
  // Lượt không trọn (lỗi/Dừng/rớt mạng) bị server bỏ khỏi lịch sử, kèm cả hội thoại nếu lượt này vừa tạo nó
  // -> hội thoại mới thì phải quên id để lần gửi sau mở hội thoại mới.
  const fresh = convId === null;
  let raw = "", frame = 0, ended = false, think = null, thinkSince = 0; // think: dòng "Em đang suy nghĩ…" ở đáy tin nhắn
  controller = new AbortController();
  stick = true;
  thread.setAttribute("aria-busy", "true");
  sendBtn.classList.add("stop");
  sendBtn.lastElementChild.textContent = "Dừng";
  setOrb("thinking");
  bunnySay("Em đang suy nghĩ giúp công chúa nè…");
  try {
    const r = await fetch("/api/assistant/chat", {
      method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
      body: JSON.stringify({ conversation_id: convId, message: text, deep_think: deepBtn.getAttribute("aria-pressed") === "true" }),
    });
    if (!r.ok) {
      if (r.status === 404) convId = null; // hội thoại đã hết hạn/bị xoá: "Thử lại" phải mở hội thoại mới, không gửi lại id cũ mãi
      throw new ApiError(apiText((await r.json().catch(() => ({}))).detail, ERR_GENERIC));
    }
    const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf = (buf + value).replace(/\r\n/g, "\n"); // gộp rồi mới đổi CRLF để không sót khi \r và \n nằm ở 2 chunk khác nhau
      let cut;
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, cut);
        buf = buf.slice(cut + 2);
        const name = /^event: (.+)$/m.exec(block)?.[1]; // khối không có dòng event: (comment `: ping` giữ kết nối) không khớp nhánh nào -> bỏ qua
        const data = JSON.parse(/^data: ?([^\n]*)/m.exec(block)?.[1] || "{}"); // [^\n]: server gửi thẳng U+2028/2029, "." sẽ dừng ở đó
        if (think && (name === "token" || name === "tool_start")) {
          // model thôi suy luận (bắt đầu trả lời / gọi tool): dòng "đang suy nghĩ" chỉ là tạm, nhường chỗ cho bước kế
          // (giữ lại thì mỗi vòng tool thêm 1 dòng "đã suy nghĩ" -> danh sách bước rối)
          think.remove();
          think = null;
        }
        if (name === "conversation") convId = data.id;
        else if (name === "thinking") {
          // GLM suy luận cả phút trước khi gọi tool/trả lời; server báo vài giây 1 lần để UI không đứng im
          setOrb("thinking");
          if (!think) { // ở đáy tin nhắn (không nằm trong .as-steps phía trên thẻ job) để luôn thấy khi đang bám đáy
            think = wrap.appendChild(el("p", "as-thinking"));
            thinkSince = Date.now();
          }
          const s = Math.round((Date.now() - thinkSince) / 1000);
          think.textContent = s ? `Em đang suy nghĩ… ${s} giây` : "Em đang suy nghĩ…";
        } else if (name === "tool_start") {
          setOrb("thinking");
          // nhãn tool đã kết thúc bằng "…" ("Em đang tìm việc…") -> bỏ trước khi thêm " nè…"
          bunnySay(data.label ? `${data.label.replace(/…$/, "")} nè…` : "Em đang làm giúp công chúa nè…");
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
          stage?.speak(); // 1 token về -> thỏ gật 1 nhịp như đang nói
          raw += data.text;
          // render markdown tối đa 1 lần mỗi frame cho mượt khi token về nhanh
          frame ||= requestAnimationFrame(() => {
            frame = 0;
            bubble.innerHTML = md(raw);
            follow();
          });
        } else if (name === "error") throw new ApiError(apiText(data.message, ERR_GENERIC));
        else if (name === "done") {
          ended = true;
          loadHistory();
          // trả lời xong: thỏ chào công chúa đúng ritual em đã hứa
          bunnySay("Thưa Công Chúa, em đã làm xong yêu cầu của Công Chúa rồi ạ 🌸");
        }
        follow();
      }
    }
    // stream đóng mà không có done/error (proxy cắt, server restart, rớt mạng): coi là lỗi để hiện thông báo + "Thử lại"
    if (!ended) throw new ApiError(ERR_GENERIC);
  } catch (e) {
    if (e.name !== "AbortError") {
      const p = el("p", "as-error", e instanceof ApiError ? e.message : ERR_GENERIC);
      const retry = el("button", "as-btn ghost", "Thử lại");
      retry.type = "button";
      // server đã bỏ câu hỏi hỏng khỏi lịch sử -> gỡ cả cặp tin nhắn này rồi gửi lại, không thành 2 câu hỏi trùng
      retry.addEventListener("click", () => {
        if (controller) return; // đang trả lời câu khác: send() sẽ bỏ qua, đừng gỡ tin nhắn
        ask.remove();
        wrap.remove();
        send(text);
      });
      p.append(" ", retry);
      wrap.append(p);
    }
  } finally {
    cancelAnimationFrame(frame);
    // wrap đã rời thread = resetChat (chat mới / mở hội thoại khác) đã đặt convId mới, đừng ghi đè
    if (!ended && fresh && wrap.isConnected) convId = null;
    // lỗi / Dừng / stream đứt giữa chừng: các bước còn "◌" nháy thì đánh dấu hỏng, bỏ dòng "đang suy nghĩ"
    for (const li of steps.querySelectorAll(".run")) li.className = "fail";
    think?.remove();
    bubble.innerHTML = md(raw || (controller?.signal.aborted ? "_(công chúa đã dừng)_" : ""));
    controller = null;
    thread.removeAttribute("aria-busy");
    sendBtn.classList.remove("stop");
    sendBtn.lastElementChild.textContent = "Gửi";
    setOrb("idle");
    // lỗi / Dừng / stream đứt: tắt bong bóng "đang nghĩ…" của thỏ (lỗi đã hiện ngay trong khung chat)
    if (!ended) $("#bunny-say")?.classList.remove("on");
    follow();
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
    if (!controller) $("#composer").requestSubmit(); // đang stream: Enter không được làm gì (submit lúc này = Dừng); chỉ nút Dừng mới ngắt
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
  } else toggleDrawer(`drawer-${b.dataset.action}`, b);
}));
document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => toggleDrawer(null)));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !e.isComposing && document.querySelector(".as-drawer:not([hidden])")) toggleDrawer(null);
});
document.querySelectorAll("[data-upload]").forEach((b) => b.addEventListener("click", () => $("#cv-file").click()));
$("#cv-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  // mở ngăn hồ sơ (đang mở thì giữ nguyên, không toggle) để thấy tiến trình và cả lỗi, dù bấm từ ô nhập hay từ chính ngăn này
  if ($("#drawer-profile").hidden) toggleDrawer("drawer-profile");
  $("#cv-status").textContent = "Em đang đọc CV…";
  let r, data;
  try {
    r = await fetch("/api/assistant/profile/cv", { method: "POST", body: fd });
    data = await r.json().catch(() => ({}));
  } catch {
    $("#cv-status").textContent = "Em không tải được CV lúc này, công chúa thử lại giúp em nhé";
    return;
  }
  if (!r.ok) {
    // 413 do Nginx (client_max_body_size) trả HTML, không có detail JSON
    $("#cv-status").textContent = r.status === 413 ? "File CV tối đa 5MB thôi ạ" : apiText(data.detail, "Em không đọc được file này ạ.");
    return;
  }
  await loadProfile();
  addMessage("assistant", `Em đọc xong CV **${data.cv_filename}** rồi ạ 💐 Công chúa hỏi em việc nào hợp nhé!`);
});
$("#profile-save").addEventListener("click", async () => {
  const r = await fetch("/api/assistant/profile", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ about: $("#about").value }),
  }).catch(() => null); // rớt mạng: báo lỗi như lỗi server
  $("#cv-status").textContent = r?.ok ? "Đã lưu hồ sơ ✓" : apiText((await r?.json().catch(() => ({})))?.detail, "Lưu lỗi, thử lại giúp em nhé");
});
$("#profile-delete").addEventListener("click", async () => {
  if (!confirm("Xoá toàn bộ hồ sơ và CV?")) return;
  const r = await fetch("/api/assistant/profile", { method: "DELETE" }).catch(() => null);
  if (!r?.ok) {
    $("#cv-status").textContent = "Em chưa xoá được hồ sơ, công chúa thử lại giúp em nhé";
    return;
  }
  loadProfile();
});

loadHistory();
loadProfile();
// Thỏ chào công chúa khi mở trang (1 lần, tự tắt) — người dùng biết ngay nhân vật đang ở đây
bunnySay("Em chào công chúa ạ 🌸 Hôm nay công chúa cần em tìm việc gì nè?");
