// Trợ lý AI (/assistant): gửi câu hỏi, đọc SSE, vẽ tin nhắn + thẻ job + nguồn, lịch sử, hồ sơ/CV.
// Phần dùng chung với widget thỏ hỏi nhanh ở các trang khác nằm trong chat-core.js.
import { el, md, ERR_GENERIC, ApiError, apiText, readSSE, addJobs, addSources } from "./chat-core.js";

const $ = (s) => document.querySelector(s);
const thread = $("#thread"), input = $("#input"), sendBtn = $("#send"), deepBtn = $("#deep");
const guest = $(".as-shell").dataset.guest === "true";
let convId = null, controller = null, stage = null, opener = null;
// Bám đáy khi đang stream: chỉ cuộn theo khi công chúa đang ở gần cuối hội thoại, đang kéo lên đọc thì để yên.
let stick = true;
thread.addEventListener("scroll", () => { stick = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 60; });
const follow = () => { if (stick) thread.scrollTop = thread.scrollHeight; };
const setOrb = (state) => {
  $(".as-orb-css").dataset.state = state;
  stage?.setState(state);
};
// Thỏ mascot 3D chỉ hiện từ 1280px (ô nhân vật ẩn dưới mức này, kể cả mobile) -> chỉ tải GLB 2.8MB khi màn đủ rộng;
// màn hẹp mở rộng ra sau đó thì tải lúc đó. Tải lỗi -> bỏ qua (ô nhân vật trống), lần đổi kích thước sau thử lại.
const wideForCharacter = matchMedia("(min-width: 1280px)");
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
    stage?.setState($(".as-orb-css").dataset.state); // stage nạp sau: theo trạng thái hiện tại (vd đang chào)
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

// Em chào trước: lời chào chỉ có ở giao diện (không lưu, không gửi LLM, không tốn lượt). Màn hình trống thì gõ dần
// dưới orb; tin nhắn đầu tiên kéo nó vào đầu thread để hội thoại đọc liền mạch.
const hour = new Date().getHours();
const GREETING = `Em chào công chúa ạ ${hour < 11 ? "☀️" : hour < 18 ? "🌸" : "🌙"} Em tìm việc mới, so sánh JD, xem lương `
  + "thị trường và review công ty giúp công chúa được nè. Hôm nay công chúa muốn em tìm việc gì ạ?";
const greet = el("div", "as-msg assistant as-greet");
const greetBubble = greet.appendChild(el("div", "as-bubble"));
// phần chưa gõ vẫn nằm trong bong bóng nhưng trong suốt: bong bóng đủ cao ngay từ đầu, trang không xô lệch khi gõ
const greetShown = el("span"), greetRest = el("span", "as-greet-rest");
let greetTimer = 0;

const addMessage = (role, text = "") => {
  if (greet.isConnected && greet.parentElement !== thread) {
    // tin nhắn đầu tiên: lời chào (đang gõ dở thì hiện đủ) chuyển từ dưới orb vào đầu thread
    clearInterval(greetTimer);
    greetShown.textContent = GREETING;
    greetRest.textContent = "";
    if (!controller) setOrb("idle");
    thread.append(greet);
  }
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

// Về màn hình trống. greetAgain=false khi sắp mở hội thoại cũ: không chào lại (lời chào không có trong lịch sử).
const resetChat = (greetAgain = true) => {
  controller?.abort();
  convId = null;
  clearInterval(greetTimer);
  thread.replaceChildren();
  document.body.classList.remove("has-chat");
  $("#bunny-say")?.classList.remove("on"); // chat mới: thỏ im, chờ câu hỏi mới
  if (!greetAgain) {
    greet.remove();
    setOrb("idle");
    return;
  }
  $(".as-hero").append(greet);
  const words = GREETING.split(" ");
  let n = matchMedia("(prefers-reduced-motion: reduce)").matches ? words.length - 1 : 0;
  const type = () => {
    n++;
    greetShown.textContent = words.slice(0, n).join(" ");
    greetRest.textContent = words.slice(n).map((w) => ` ${w}`).join("");
    if (n < words.length) return;
    clearInterval(greetTimer);
    if (!controller) setOrb("idle");
  };
  greetBubble.replaceChildren(greetShown, greetRest);
  setOrb("speaking");
  type();
  if (n < words.length) greetTimer = setInterval(type, 45);
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

// Mở 1 hội thoại đã lưu: từ ngăn lịch sử, hoặc ?c=<id> khi bấm "Mở rộng" ở widget thỏ trang khác.
// Trả Response (null = rớt mạng) để nơi gọi tự báo lỗi theo ngữ cảnh của mình.
const openConversation = async (id) => {
  const res = await fetch(`/api/assistant/conversations/${id}`).catch(() => null);
  if (!res?.ok) return res;
  const conv = await res.json();
  resetChat(false);
  convId = conv.id;
  for (const m of conv.messages) {
    const w = addMessage(m.role, m.content);
    addJobs(w, m.meta?.jobs, guest);
    addSources(w, m.meta?.sources);
  }
  return res;
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
      const res = await openConversation(c.id);
      if (!res) {
        $("#history-status").textContent = "Em chưa mở được hội thoại, công chúa thử lại giúp em nhé";
        return;
      }
      if (!res.ok) return loadHistory();
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
    await readSSE(r, (name, data) => {
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
        addJobs(wrap, data.jobs, guest);
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
        stage?.wave();
      }
      follow();
    });
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

resetChat(); // màn hình trống + em chào trước
// "Mở rộng" từ widget thỏ: /assistant?c=<id> -> mở tiếp đúng hội thoại đó (hết hạn/không phải của mình thì giữ màn hình trống)
const fromWidget = new URLSearchParams(location.search).get("c");
if (/^\d+$/.test(fromWidget || "")) openConversation(fromWidget).finally(() => history.replaceState(null, "", "/assistant"));
loadHistory();
loadProfile();
// Thỏ chào công chúa khi mở trang (1 lần, tự tắt) — người dùng biết ngay nhân vật đang ở đây
bunnySay("Em chào công chúa ạ 🌸 Hôm nay công chúa cần em tìm việc gì nè?");
