// Phần chat dùng chung cho trang Trợ lý (/assistant, chat.js) và widget thỏ hỏi nhanh ở mọi trang (widget.js):
// dựng phần tử, render markdown an toàn, lỗi tiếng Việt, đọc khung SSE của POST /api/assistant/chat, thẻ job + nguồn.
import { marked } from "/static/vendor/marked/marked.esm.js";
import DOMPurify from "/static/vendor/dompurify/purify.es.mjs";

export const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

// Chỉ hiện thông báo lỗi khi nó là chuỗi do API của mình trả về (detail / SSE error.message); còn lại (mạng, JSON hỏng, detail dạng mảng 422) dùng câu tiếng Việt chung.
export const ERR_GENERIC = "Em gặp lỗi rồi, công chúa thử lại giúp em nha 🥺";
export class ApiError extends Error {}
export const apiText = (v, fallback) => (typeof v === "string" && v ? v : fallback);

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
export const md = (text) => DOMPurify.sanitize(marked.parse(text), {
  ALLOWED_TAGS: ["p", "br", "strong", "em", "b", "i", "del", "code", "pre", "blockquote", "ul", "ol", "li", "a",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "table", "thead", "tbody", "tr", "th", "td"],
  ALLOWED_ATTR: ["href", "title", "start", "align"],
  ADD_URI_SAFE_ATTR: ["start", "align"],
  ALLOWED_URI_REGEXP: /^(?:https?:|\/(?![\/\\]))/i,
});

// Đọc stream SSE (`event: <tên>\ndata: <json>\n\n`) tới hết, gọi onEvent(tên, data) cho từng khung có tên.
// Khung không có dòng event: (comment `: ping` giữ kết nối qua Nginx) bị bỏ qua. onEvent ném lỗi -> dừng đọc và ném tiếp.
export const readSSE = async (response, onEvent) => {
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buf = (buf + value).replace(/\r\n/g, "\n"); // gộp rồi mới đổi CRLF để không sót khi \r và \n nằm ở 2 chunk khác nhau
    let cut;
    while ((cut = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, cut);
      buf = buf.slice(cut + 2);
      const name = /^event: (.+)$/m.exec(block)?.[1];
      if (name) onEvent(name, JSON.parse(/^data: ?([^\n]*)/m.exec(block)?.[1] || "{}")); // [^\n]: server gửi thẳng U+2028/2029, "." sẽ dừng ở đó
    }
  }
};

// Thẻ job/nguồn đã được server bỏ trùng và giới hạn số lượng (tool_end và meta lưu giống nhau) -> vẽ thẳng.
// Thẻ, nguồn nằm TRÊN bong bóng chữ: chữ đang stream luôn ở đáy tin nhắn, chỗ mắt (và cuộn bám đáy) đang nhìn.
// guest: khách không lưu việc được (API trả 401) -> không vẽ nút "Lưu việc".
export const addJobs = (wrap, jobs, guest) => {
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

export const addSources = (wrap, sources) => {
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
