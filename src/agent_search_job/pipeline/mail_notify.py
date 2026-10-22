"""Gửi mail thông báo job mới sau mỗi lượt quét (scheduled scan).

Chỉ dùng stdlib (smtplib + email.message.EmailMessage) — không thêm dependency.
Cấu hình qua .env (xem .env.example mục "Mail thông báo job mới"):

    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_USER=tencuaban@gmail.com
    SMTP_PASSWORD=app-password   # Gmail: App Password (2FA), không phải mật khẩu thường
    SMTP_SSL=0                   # 1 = SMTPS (cổng 465), 0 = STARTTLS (cổng 587/25)
    MAIL_FROM=tencuaban@gmail.com
    MAIL_TO=ban@gmail.com,nguoib@gmail.com
    NEW_JOB_MAIL_MAX=30          # tối đa số job trong 1 mail

Chưa đặt MAIL_TO (hoặc SMTP_HOST) -> tắt gửi mail, chỉ ghi log.
Gửi lỗi không làm hỏng lượt quét — log cảnh báo rồi thôi (scan vẫn có kết quả).
"""
from __future__ import annotations

import logging
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Optional

from ..domain.models import ROLE_CLUSTER_LABELS_VI, JOB_LEVEL_LABELS_VI

log = logging.getLogger("pipeline.mail")
# Thỏ 2D vẫy chào ở đầu mail job mới: cắt từ tranh gốc của mascot (cùng nhân vật với thỏ 3D trên web),
# nhúng vào mail dạng ảnh inline (cid:) nên hiện được cả khi chưa đặt SITE_URL / server không mở công khai.
BUNNY_PNG = Path(__file__).resolve().parents[1] / "web" / "static" / "mail" / "bunny-wave.png"
BUNNY_CID = "bunny-wave"


def mail_enabled() -> bool:
    """True khi cấu hình đủ để gửi mail (MAIL_TO + SMTP_HOST đều đã đặt)."""
    return bool(_env_list("MAIL_TO") and os.environ.get("SMTP_HOST", "").strip())


def _env_list(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").replace(";", ",").split(",")
            if v.strip()]


def render_new_jobs_email(jobs: list[dict], scan_summary: dict,
                          site_url: str = "", max_jobs: int = 30) -> tuple[str, str, str]:
    """Render mail "job mới" -> (subject, html, text). jobs phải có job_id/title/company_name.

    Template cùng tông hồng pastel với website (token :root trong static/app.css): nền giấy hồng #fff5f8,
    dải đầu gradient #ffe6ee -> #f7b7cb, chữ nâu mận #3a2230, link hồng mâm xôi #c2366b, mỗi việc 1 thẻ.
    Viết cho trình đọc mail: bảng + style inline (Gmail/Outlook bỏ <style>, không có flex/grid), phông
    Be Vietnam Pro nếu máy có, không thì Arial. Text từ nguồn tuyển dụng (tiêu đề, công ty...) luôn escape.
    """
    n = len(jobs)
    shown = jobs[:max_jobs]
    # giờ Việt Nam (UTC+7, không đổi giờ mùa hè) thay cho chuỗi ISO UTC thô trong tiêu đề/đầu mail
    try:
        at_vn = datetime.fromisoformat(scan_summary.get("at") or "").astimezone(timezone(timedelta(hours=7)))
        when = at_vn.strftime("%H:%M · %d/%m/%Y")
    except (TypeError, ValueError):
        when = ""
    subject = f"[Agent Search Job] {n} việc làm mới" + (f" · {when}" if when else "")
    font = "'Be Vietnam Pro',-apple-system,'Segoe UI',Roboto,Arial,sans-serif"
    display = "'Bricolage Grotesque','Be Vietnam Pro',Arial,sans-serif"

    # chip tóm tắt lượt quét: chỉ hiện mục có số khác 0 (màu như badge trên web: mint / lilac / xám / cam)
    chip_specs = (
        ("inserted", f"+{scan_summary.get('inserted', n)} việc mới", "#e2f5ec", "#23795a"),
        ("updated", f"{scan_summary.get('updated', 0)} cập nhật", "#f1ecfb", "#6a55a8"),
        ("hidden", f"{scan_summary.get('hidden', 0)} bị ẩn (hết hạn/ngừng tuyển)", "#f8eff3", "#7d6070"),
        ("deleted", f"{scan_summary.get('deleted', 0)} đã xoá", "#f8eff3", "#7d6070"),
        ("errors", f"{scan_summary.get('errors', 0)} lỗi nguồn", "#fff4e6", "#9a5414"),
    )
    chips = "".join(
        f'<span style="display:inline-block;margin:0 6px 6px 0;padding:4px 11px;border-radius:999px;'
        f'background:{bg};color:{fg};font-size:12.5px;font-weight:700;white-space:nowrap">{escape(label)}</span>'
        for key, label, bg, fg in chip_specs if scan_summary.get(key, n if key == "inserted" else 0))

    cards, text_lines = [], []
    for j in shown:
        title, company = j.get("title") or "?", j.get("company_name") or "?"
        salary = j.get("salary_text") or ""
        if not salary and j.get("salary_min_vnd"):
            lo = j["salary_min_vnd"] / 1_000_000
            hi = (j["salary_max_vnd"] / 1_000_000) if j.get("salary_max_vnd") else None
            salary = f"{lo:g} triệu" + (f" - {hi:g} triệu" if hi else "+")
        link = (f"{site_url}/jobs/{j['job_id']}" if site_url
                else (j.get("apply_url") or j.get("source_url") or "").strip())
        tags = [t for t in (
            JOB_LEVEL_LABELS_VI.get(j.get("job_level") or "", ""),
            f"📍 {j['district']}" if j.get("district") else "",
            ROLE_CLUSTER_LABELS_VI.get(j.get("role_cluster") or "", j.get("role_cluster") or ""),
        ) if t]
        tag_html = "".join(
            f'<span style="display:inline-block;margin:6px 6px 0 0;padding:2px 9px;border-radius:999px;'
            f'background:#ffe6ee;color:#7d6070;font-size:12px;font-weight:600">{escape(t)}</span>' for t in tags)
        title_html = (f'<a href="{escape(link)}" style="color:#c2366b;text-decoration:none">{escape(title)}</a>'
                      if link else escape(title))
        # chữ cái avatar: bỏ tiền tố pháp lý ("CÔNG TY CỔ PHẦN ...") kẻo việc nào cũng là "C"
        brand = re.sub(r"(?i)^\s*(?:công\s*ty|cty)\s*(?:tnhh|cổ\s*phần|cp)?\s*(?:một\s*thành\s*viên|mtv)?\s*",
                       "", company).strip() or company
        initial = brand[:1].upper()
        cards.append(f"""
<tr><td style="padding:0 0 12px">
 <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border:1px solid #f4d9e3;border-radius:14px">
  <tr>
   <td width="44" valign="top" style="padding:16px 0 16px 16px">
    <div style="width:40px;height:40px;border-radius:50%;background:#ffe6ee;border:1px solid #f6cbd9;color:#c2366b;
      font:800 17px/40px {display};text-align:center">{escape(initial)}</div>
   </td>
   <td valign="top" style="padding:14px 16px 14px 12px">
    <div style="font:700 16px/1.35 {font}">{title_html}</div>
    <div style="margin-top:2px;color:#3a2230;font:600 13.5px/1.4 {font}">{escape(company)}</div>
    {f'<div style="margin-top:6px;color:#3a2230;font:700 14px/1.4 {font}">💰 {escape(salary)}</div>' if salary else ''}
    {f'<div style="font-family:{font}">{tag_html}</div>' if tag_html else ''}
    {f'<div style="margin-top:10px;font:700 13px {font}"><a href="{escape(link)}" style="color:#c2366b;text-decoration:none">Xem việc →</a></div>' if link else ''}
   </td>
  </tr>
 </table>
</td></tr>""")
        text_lines.append(f"- {title} — {company}" + (f" · {salary}" if salary else "") + (f"\n  {link}" if link else ""))

    more = n - len(shown)
    more_html = (f'<p style="margin:4px 0 0;color:#7d6070;font:500 14px/1.5 {font};text-align:center">'
                 f'… và {more} việc nữa — mở trang tìm việc để xem hết.</p>') if more > 0 else ""
    cta = (f"""<table role="presentation" cellpadding="0" cellspacing="0" align="center" style="margin:18px auto 4px">
  <tr><td bgcolor="#f7b7cb" style="border-radius:10px;box-shadow:inset 0 -2px 0 rgba(194,54,107,.18)">
   <a href="{escape(site_url)}/search" style="display:inline-block;padding:13px 26px;color:#3a2230;
     font:700 15px {font};text-decoration:none">🔎 Mở trang tìm việc</a>
  </td></tr></table>""") if site_url else ""
    preheader = escape(f"Công chúa ơi, nay em tìm được {n} jobs mới nè: " + "; ".join(f"{j.get('title', '')} — {j.get('company_name', '')}" for j in shown[:3]))
    html = f"""<!DOCTYPE html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>{escape(subject)}</title>
<link href="https://fonts.googleapis.com/css2?family=Be+Vietnam+Pro:wght@400;600;700&family=Bricolage+Grotesque:opsz,wght@12..96,800&display=swap" rel="stylesheet">
</head>
<body style="margin:0;padding:0;background:#fff5f8">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:#fff5f8">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#fff5f8" style="background:#fff5f8">
 <tr><td align="center" style="padding:24px 12px">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;background:#ffffff;
    border:1px solid #f4d9e3;border-radius:20px;overflow:hidden;box-shadow:0 6px 20px -10px rgba(194,54,107,.18)">
   <tr><td bgcolor="#ffe6ee" style="padding:22px 24px 18px;background:#ffe6ee;background-image:linear-gradient(135deg,#fff0f5 0%,#ffe6ee 45%,#f7b7cb 100%)">
    <table role="presentation" cellpadding="0" cellspacing="0"><tr>
     <td style="width:32px;height:32px;border-radius:10px;background:#f7b7cb;color:#ffffff;font:700 17px/32px Arial;text-align:center">✦</td>
     <td style="padding-left:10px;color:#3a2230;font:800 15px {display}">Việc làm Content &amp; Marketing
      <span style="display:inline-block;margin-left:4px;padding:1px 8px;border-radius:999px;background:#ffffff;color:#c2366b;font:700 11px {font};vertical-align:2px">TP.HCM</span></td>
    </tr></table>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:14px 0 10px"><tr>
     <td width="76" valign="bottom" style="padding:0 6px 0 0">
      <img src="cid:{BUNNY_CID}" width="72" height="130" alt="Thỏ trợ lý vẫy chào công chúa"
        style="display:block;width:72px;height:130px;border:0">
     </td>
     <td valign="middle">
      <div style="background:#ffffff;border:1px solid #f6cbd9;border-radius:18px 18px 18px 4px;padding:14px 16px;
        box-shadow:0 6px 16px -10px rgba(194,54,107,.35)">
       <h1 style="margin:0;color:#3a2230;font:800 21px/1.3 {display}">Công chúa ơi, nay em tìm được
        <span style="color:#c2366b">{n} jobs mới</span> nè 🌸</h1>
       <p style="margin:6px 0 0;color:#7d6070;font:500 13.5px/1.5 {font}">{escape(f"Lượt quét định kỳ lúc {when}" if when else "Lượt quét định kỳ")}</p>
      </div>
     </td>
    </tr></table>
    <div style="font-family:{font}">{chips}</div>
   </td></tr>
   <tr><td style="padding:20px 20px 8px;background:#fff5f8">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(cards)}</table>
    {more_html}
    {cta}
   </td></tr>
   <tr><td style="padding:16px 24px 22px;background:#fff5f8;border-top:1px solid #f4d9e3;color:#a88a99;font:500 12px/1.6 {font};text-align:center">
    Mail tự động sau mỗi lượt quét định kỳ · Việc làm Content &amp; Marketing TP.HCM<br>
    Người nhận cấu hình trong .env (MAIL_TO).
   </td></tr>
  </table>
 </td></tr>
</table>
</body></html>"""
    summary = " · ".join(label for key, label, _, _ in chip_specs if scan_summary.get(key, n if key == "inserted" else 0))
    text = (f"Công chúa ơi, nay em tìm được {n} jobs mới nè 🌸" + (f"\nLượt quét lúc {when}" if when else "")
            + f"\n{summary}\n\n"
            + "\n".join(text_lines)
            + (f"\n\n… và {more} việc nữa" if more > 0 else "")
            + (f"\n\nMở trang tìm việc: {site_url}/search" if site_url else ""))
    return subject, html, text


def send_mail(subject: str, html: str, text: str,
              images: Optional[dict[str, bytes]] = None) -> tuple[bool, str]:
    """Gửi mail SMTP tới MAIL_TO. Trả (ok, message). Lỗi -> (False, lỗi), không raise.

    images: {cid: PNG} nhúng kèm phần HTML (multipart/related) cho <img src="cid:...">."""
    host = os.environ.get("SMTP_HOST", "").strip()
    to_addrs = _env_list("MAIL_TO")
    if not host or not to_addrs:
        return False, "Chưa cấu hình SMTP_HOST/MAIL_TO — bỏ gửi mail"
    port = int(os.environ.get("SMTP_PORT") or (465 if _smtp_ssl() else 587))
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    sender = os.environ.get("MAIL_FROM", "").strip() or user or to_addrs[0]
    timeout = int(os.environ.get("SMTP_TIMEOUT_SECONDS") or 30)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to_addrs)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(text)          # phần text thuần
    msg.add_alternative(html, subtype="html")
    for cid, data in (images or {}).items():
        msg.get_payload()[-1].add_related(data, maintype="image", subtype="png", cid=f"<{cid}>",
                                          filename=f"{cid}.png", disposition="inline")

    try:
        if _smtp_ssl():
            with smtplib.SMTP_SSL(host, port, timeout=timeout,
                                  context=ssl.create_default_context()) as s:
                if user:
                    s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as s:
                s.ehlo()
                # STARTTLS chỉ khi server hỗ trợ (relay nội bộ có thể không có TLS)
                if s.has_extn("starttls"):
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                if user:
                    s.login(user, password)
                s.send_message(msg)
    except Exception as e:  # noqa: BLE001 - lỗi mail không được làm hỏng lượt quét
        log.warning("Gửi mail thất bại (%s): %s", host, e)
        return False, f"{type(e).__name__}: {e}"
    log.info("Đã gửi mail '%s' tới %s", subject, ", ".join(to_addrs))
    return True, f"Đã gửi tới {', '.join(to_addrs)}"


def _smtp_ssl() -> bool:
    v = os.environ.get("SMTP_SSL", "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off", ""):
        return False
    return False


def notify_new_jobs(jobs: list[dict], scan_summary: dict,
                    site_url: str = "") -> tuple[bool, str]:
    """Gửi mail "job mới" nếu bật cấu hình + có job mới. Trả (ok, message)."""
    if not jobs:
        return True, "Không có job mới — không gửi mail"
    if not mail_enabled():
        log.info("Có %d job mới nhưng chưa cấu hình MAIL_TO/SMTP_HOST — bỏ gửi mail", len(jobs))
        return False, "Chưa cấu hình mail (MAIL_TO/SMTP_HOST)"
    max_jobs = int(os.environ.get("NEW_JOB_MAIL_MAX") or 30)
    subject, html, text = render_new_jobs_email(jobs, scan_summary, site_url, max_jobs)
    return send_mail(subject, html, text,
                     images={BUNNY_CID: BUNNY_PNG.read_bytes()} if BUNNY_PNG.exists() else None)


def notify_admin(subject: str, body: str) -> tuple[bool, str]:
    """Mail cảnh báo vận hành (lỗi lượt quét định kỳ...) — chỉ khi đã cấu hình SMTP."""
    if not mail_enabled():
        return False, "Chưa cấu hình mail"
    return send_mail(subject,
                     f"<pre style='font-family:ui-monospace,monospace'>{body}</pre>", body)
