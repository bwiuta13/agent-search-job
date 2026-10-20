// Thỏ hỏi nhanh (_bunny_widget.html) ở mọi trang trừ /assistant: chat mini cùng API + lịch sử (tự xoá sau 24h) với trang Trợ lý.
// Hội thoại đang dở và trạng thái mở card được nhớ trong sessionStorage -> sang trang khác (bấm thẻ job) vẫn hỏi tiếp được;
// "Mở rộng" mở đúng hội thoại đó ở /assistant?c=<id>.
// Thỏ 3D (Three.js + character.glb 2.8MB) chỉ nạp khi trình duyệt rảnh sau khi trang tải xong; màn hẹp thì đợi tới lúc mở chat.
import { el, md, ERR_GENERIC, ApiError, apiText, readSSE, addJobs, addSources } from "./chat-core.js";

const $ = (s) => document.querySelector(s);
const panel = $("#bw-panel"), fab = $("#bw-fab"), hello = $("#bw-hello"), thread = $("#bw-thread");
const form = $("#bw-form"), input = $("#bw-input"), sendBtn = $("#bw-send"), statusEl = $("#bw-status");
const canvas = $("#bw-canvas"), headSlot = $("#bw-avatar-slot");
const guest = $("#bw").dataset.guest === "true";
const IDLE = "Em giúp gì được cho công chúa ạ?";
let convId = null, controller = null, stage = null, started = false, loaded = false, guestCookie = null;

// sessionStorage có thể bị chặn (cửa sổ riêng tư, chặn dữ liệu trang) -> đọc/ghi lỗi thì coi như không có
const session = {
  get: (k) => { try { return sessionStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { if (v == null) sessionStorage.removeItem(k); else sessionStorage.setItem(k, v); } catch { /* bỏ qua */ } },
};
const remember = (id) => {
  convId = id;
  session.set("bw-conv", id == null ? null : String(id));
  $("#bw-expand").href = id == null ? "/assistant" : `/assistant?c=${id}`;
};
remember(/^\d+$/.test(session.get("bw-conv") || "") ? Number(session.get("bw-conv")) : null);

// Khách (public mode) chưa từng mở /assistant thì chưa có cookie phiên khách (API chat trả 401) -> xin 1 lần trước khi gọi
const ensureGuest = () => {
  if (guest) guestCookie ||= fetch("/api/assistant/guest-session", { method: "POST" })
    .then((r) => { if (!r.ok) guestCookie = null; }, () => { guestCookie = null; });
  return guestCookie;
};

// Không có WebGL2 / bật tiết kiệm dữ liệu / nạp lỗi -> giữ thỏ emoji (dispose() ẩn canvas, gỡ class has-character)
const start3D = () => {
  if (started || navigator.connection?.saveData) return;
  started = true;
  import("./stage.js")
    .then((m) => {
      stage = m.createStage(canvas, { avatarSlot: canvas });
      return stage?.setCharacter(canvas.dataset.character);
    })
    .catch((e) => {
      console.warn("Không tải được thỏ 3D", e);
      stage?.dispose();
      stage = null;
    });
};
if (matchMedia("(min-width: 768px)").matches) {
  const later = () => (window.requestIdleCallback ? requestIdleCallback(start3D, { timeout: 4000 }) : setTimeout(start3D, 1500));
  if (document.readyState === "complete") later();
  else addEventListener("load", later, { once: true });
}

// Bám đáy khi đang stream: chỉ cuộn theo khi công chúa đang ở gần cuối, đang kéo lên đọc thì để yên
let stick = true;
thread.addEventListener("scroll", () => { stick = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 60; });
const follow = () => { if (stick) thread.scrollTop = thread.scrollHeight; };

const addMsg = (role, text = "") => {
  panel.classList.add("has-chat");
  const wrap = el("div", `as-msg ${role}`);
  const bubble = wrap.appendChild(el("div", "as-bubble"));
  if (role === "user") bubble.textContent = text;
  else bubble.innerHTML = md(text);
  thread.append(wrap);
  return wrap;
};

const openPanel = async (focus = true) => {
  hello.hidden = true;
  panel.hidden = false;
  fab.hidden = true;
  fab.setAttribute("aria-expanded", "true");
  headSlot.append(canvas); // 1 canvas, 1 context WebGL: thỏ chuyển từ nút lên đầu card
  session.set("bw-open", "1");
  start3D(); // màn hẹp chưa nạp lúc rảnh -> nạp khi công chúa mở chat
  stage?.wave();
  if (focus) input.focus();
  follow();
  // Lần mở đầu tiên của trang: hiện lại hội thoại đang dở (hỏi ở trang trước). Rớt mạng -> lần mở sau thử lại;
  // 404 (hết hạn 24h / đã xoá ở trang Trợ lý) -> quên id, hỏi câu mới là hội thoại mới.
  if (loaded || convId === null) return;
  loaded = true;
  await ensureGuest();
  const r = await fetch(`/api/assistant/conversations/${convId}`).catch(() => null);
  if (!r) {
    loaded = false;
    return;
  }
  if (!r.ok) return remember(null);
  const conv = await r.json();
  if (thread.childElementCount) return; // công chúa đã hỏi tiếp trong lúc chờ: không chen lịch sử vào giữa
  for (const m of conv.messages) {
    const w = addMsg(m.role, m.content);
    addJobs(w, m.meta?.jobs, guest);
    addSources(w, m.meta?.sources);
  }
  stick = true;
  follow();
};

const closePanel = () => {
  panel.hidden = true;
  fab.hidden = false;
  fab.setAttribute("aria-expanded", "false");
  $("#bw-fab-slot").append(canvas);
  session.set("bw-open", null);
  fab.focus();
};

const send = async (text) => {
  text = text.trim();
  if (!text || controller) return;
  const ask = addMsg("user", text);
  const wrap = addMsg("assistant");
  const bubble = wrap.querySelector(".as-bubble");
  wrap.classList.add("pending"); // 3 chấm chờ chữ đầu tiên
  // Lượt không trọn (lỗi/Dừng/rớt mạng) bị server bỏ, kèm cả hội thoại nếu lượt này vừa tạo -> phải quên id
  const fresh = convId === null;
  let raw = "", frame = 0, ended = false, phase = "", thinkSince = 0;
  controller = new AbortController();
  stick = true;
  follow();
  thread.setAttribute("aria-busy", "true");
  sendBtn.classList.add("stop");
  sendBtn.setAttribute("aria-label", "Dừng");
  statusEl.textContent = "Em đang suy nghĩ giúp công chúa nè…";
  stage?.setState("thinking");
  try {
    await ensureGuest();
    const r = await fetch("/api/assistant/chat", {
      method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
      body: JSON.stringify({ conversation_id: convId, message: text, deep_think: false }),
    });
    if (!r.ok) {
      if (r.status === 404) remember(null); // hội thoại đã hết hạn/bị xoá: "Thử lại" mở hội thoại mới
      throw new ApiError(apiText((await r.json().catch(() => ({}))).detail, ERR_GENERIC));
    }
    await readSSE(r, (name, data) => {
      const first = name !== phase; // đổi pha (nghĩ -> gọi tool -> trả lời): dòng trạng thái đổi 1 lần, không đọc lại theo từng token
      phase = name;
      if (name === "conversation") remember(data.id);
      else if (name === "thinking") {
        // GLM suy luận cả phút trước khi gọi tool/trả lời; server báo vài giây 1 lần để card không đứng im
        if (first) thinkSince = Date.now();
        stage?.setState("thinking");
        const s = Math.round((Date.now() - thinkSince) / 1000);
        statusEl.textContent = s ? `Em đang suy nghĩ… ${s} giây` : "Em đang suy nghĩ…";
      } else if (name === "tool_start") {
        stage?.setState("thinking");
        statusEl.textContent = data.label || "Em đang làm giúp công chúa nè…";
      } else if (name === "tool_end") {
        addJobs(wrap, data.jobs, guest);
        addSources(wrap, data.sources);
      } else if (name === "token") {
        if (first) {
          wrap.classList.remove("pending");
          stage?.setState("speaking");
          statusEl.textContent = "Em đang trả lời công chúa nè…";
        }
        stage?.speak(); // 1 token về -> thỏ gật 1 nhịp như đang nói
        raw += data.text;
        frame ||= requestAnimationFrame(() => { // render markdown tối đa 1 lần mỗi frame
          frame = 0;
          bubble.innerHTML = md(raw);
          follow();
        });
      } else if (name === "error") throw new ApiError(apiText(data.message, ERR_GENERIC));
      else if (name === "done") {
        ended = true;
        stage?.wave();
      }
      follow();
    });
    // stream đóng mà không có done/error (proxy cắt, server restart, rớt mạng): coi là lỗi để hiện "Thử lại"
    if (!ended) throw new ApiError(ERR_GENERIC);
  } catch (e) {
    if (e.name !== "AbortError") {
      const p = el("p", "as-error", e instanceof ApiError ? e.message : ERR_GENERIC);
      const retry = el("button", null, "Thử lại");
      retry.type = "button";
      // server đã bỏ câu hỏi hỏng khỏi lịch sử -> gỡ cả cặp tin nhắn rồi gửi lại, không thành 2 câu trùng
      retry.addEventListener("click", () => {
        if (controller) return;
        ask.remove();
        wrap.remove();
        send(text);
      });
      p.append(retry);
      wrap.append(p);
    }
  } finally {
    cancelAnimationFrame(frame);
    // wrap đã rời thread = "Chat mới" đã quên id, đừng ghi đè
    if (!ended && fresh && wrap.isConnected) remember(null);
    wrap.classList.remove("pending");
    bubble.innerHTML = md(raw || (controller?.signal.aborted ? "_(công chúa đã dừng)_" : ""));
    controller = null;
    thread.removeAttribute("aria-busy");
    sendBtn.classList.remove("stop");
    sendBtn.setAttribute("aria-label", "Gửi");
    statusEl.textContent = ended ? "Xong rồi ạ, công chúa hỏi tiếp em nhé 🌸" : IDLE;
    stage?.setState("idle");
    if (ended && panel.hidden) { // công chúa đóng card trong lúc em trả lời -> báo bằng bong bóng cạnh thỏ
      hello.querySelector(".bw-hello-open").textContent = "Em trả lời xong rồi ạ, công chúa bấm vào em để xem nhé 🌸";
      hello.hidden = false;
    }
    follow();
  }
};

fab.addEventListener("click", () => openPanel());
$("#bw-close").addEventListener("click", closePanel);
$("#bw-new").addEventListener("click", () => {
  controller?.abort();
  thread.replaceChildren();
  panel.classList.remove("has-chat");
  remember(null);
  statusEl.textContent = IDLE;
  input.focus();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !e.isComposing && !panel.hidden
    && (panel.contains(document.activeElement) || document.activeElement === document.body)) closePanel();
});
for (const b of document.querySelectorAll(".bw-chip")) b.addEventListener("click", () => send(b.dataset.prompt));
// Đang trả lời mà bấm thẻ job / link nội bộ thì mở tab mới: rời trang lúc này là mất câu trả lời đang viết dở.
// Trả lời xong thì mở ngay tab này, card tự mở lại ở trang mới với đúng hội thoại.
thread.addEventListener("click", (e) => {
  const a = e.target.closest(".as-job-title, .as-bubble a[href^='/']");
  if (a) a.target = controller ? "_blank" : "";
});

form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (controller) return controller.abort();
  const text = input.value;
  input.value = "";
  input.style.height = "";
  send(text);
});
input.addEventListener("keydown", (e) => {
  // isComposing: không gửi khi đang gõ dấu tiếng Việt bằng bộ gõ (Telex/VNI); đang stream thì Enter không làm gì (chỉ nút Dừng mới ngắt)
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    if (!controller) form.requestSubmit();
  }
});
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
});

// Kéo chuột trên thỏ ở đầu card -> xoay; chạm nhẹ (không kéo) -> thỏ nhảy 1 nhịp
let down = false, lastX = 0, moved = 0;
headSlot.addEventListener("pointerdown", (e) => {
  down = true;
  moved = 0;
  lastX = e.clientX;
  headSlot.setPointerCapture(e.pointerId);
});
headSlot.addEventListener("pointermove", (e) => {
  if (!down) return;
  const dx = e.clientX - lastX;
  lastX = e.clientX;
  moved += Math.abs(dx);
  stage?.rotate(dx);
});
headSlot.addEventListener("pointerup", () => {
  down = false;
  if (moved < 4) stage?.poke();
});
headSlot.addEventListener("pointercancel", () => { down = false; });

hello.querySelector(".bw-hello-open").addEventListener("click", () => openPanel());
hello.querySelector(".bw-hello-x").addEventListener("click", () => { hello.hidden = true; });

if (session.get("bw-open") === "1") openPanel(false); // đang hỏi dở ở trang trước -> mở lại card, không cướp focus của trang
else if (!session.get("bw-hello")) {
  // Thỏ chào 1 lần mỗi phiên, tự ẩn sau 8 giây
  session.set("bw-hello", "1");
  setTimeout(() => {
    if (!panel.hidden) return;
    hello.hidden = false;
    stage?.wave();
    setTimeout(() => { hello.hidden = true; }, 8000);
  }, 1500);
}
