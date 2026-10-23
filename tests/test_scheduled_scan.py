"""Tests quét định kỳ (scheduled_scan) + mail thông báo job mới — chạy offline.

run_scheduled_scan được mock adapter (không gọi nguồn thật), SMTP được mock
bằng server socket cục bộ bắt nội dung SEND — kiểm tra đủ: quét -> ẩn hết hạn
-> xoá job chết -> render + gửi mail job mới.
"""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import AsyncIterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_search_job.domain.models import NormalizedJob, utcnow
from agent_search_job.domain.normalize import normalize_job
from agent_search_job.pipeline.ingest import DEFAULT_QUERIES
from agent_search_job.pipeline.scheduled_scan import (
    ScheduledScanManager, interval_hours, last_scans, run_scheduled_scan,
    scheduled_enabled,
)
from agent_search_job.storage.store import JobStore

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_all import make_job


# ---------------------------------------------------------------------------
# Mock adapter: discover trả job từ danh sách cho trước, không gọi nguồn
# ---------------------------------------------------------------------------

class FakeAdapter:
    def __init__(self, jobs: list[NormalizedJob], name: str = "vietnamworks",
                 error: Exception | None = None):
        self.name = name
        self.jobs = jobs
        self.error = error

    async def health_check(self):
        from agent_search_job.sources.base import SourceHealth
        return SourceHealth(source=self.name, ok=self.error is None,
                            error=str(self.error) if self.error else "")

    async def discover(self, input) -> AsyncIterator[NormalizedJob]:
        if self.error:
            raise self.error
        for j in self.jobs:
            yield j


def _install_adapters(monkeypatch, jobs: list[NormalizedJob], error=None):
    """_adapters() của scheduled_scan trả FakeAdapter thay vì nguồn thật."""
    from agent_search_job.pipeline import scheduled_scan as mod

    def fake(with_topcv: bool = False):
        return [FakeAdapter(list(jobs), error=error)]

    monkeypatch.setattr(mod, "_adapters", fake)


# ---------------------------------------------------------------------------
# SMTP server giả: bắt nội dung mail gửi qua socket
# ---------------------------------------------------------------------------

class FakeSMTP(threading.Thread):
    """Server SMTP tối giản: trả 220/250/235/334/354/221, ghi DATA vào self.sent."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sent: list[str] = []
        self.port = 0
        self._ready = threading.Event()
        self._stop = threading.Event()

    def run(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.port = srv.getsockname()[1]
        self._ready.set()
        srv.settimeout(1.0)
        try:
            while not self._stop.is_set():
                try:
                    client, _ = srv.accept()
                except socket.timeout:
                    continue
                with client:
                    client.settimeout(10.0)
                    client.sendall(b"220 fake ESMTP\r\n")
                    buf = b""
                    data_lines: list[bytes] = []
                    in_data = False
                    try:
                        while True:
                            chunk = client.recv(4096)
                            if not chunk:
                                break
                            buf += chunk
                            # xử lý theo từng dòng (smtplib gửi cả mail 1 lần)
                            while b"\r\n" in buf:
                                line, buf = buf.split(b"\r\n", 1)
                                if in_data:
                                    if line == b".":
                                        client.sendall(b"250 OK\r\n")
                                        self.sent.append(b"\r\n".join(data_lines).decode("utf-8", "replace"))
                                        in_data = False
                                        data_lines = []
                                    else:
                                        data_lines.append(line)
                                    continue
                                cmd = line.strip().upper()
                                if cmd.startswith(b"EHLO") or cmd.startswith(b"HELO"):
                                    # không quảng cáo STARTTLS -> client gửi plaintext
                                    client.sendall(b"250-fake\r\n250 AUTH PLAIN LOGIN\r\n")
                                elif cmd.startswith(b"AUTH"):
                                    client.sendall(b"235 OK\r\n")
                                elif cmd.startswith(b"MAIL FROM") or cmd.startswith(b"RCPT TO"):
                                    client.sendall(b"250 OK\r\n")
                                elif cmd.startswith(b"DATA"):
                                    client.sendall(b"354 go\r\n")
                                    in_data = True
                                elif cmd.startswith(b"QUIT"):
                                    client.sendall(b"221 bye\r\n")
                                else:
                                    client.sendall(b"250 OK\r\n")
                            if cmd.startswith(b"QUIT"):
                                break
                    except (socket.timeout, ConnectionError):
                        pass
        finally:
            srv.close()

    def start_wait(self):
        self.start()
        self._ready.wait(2.0)
        return self

    def stop(self):
        self._stop.set()


@pytest.fixture
def fake_smtp():
    server = FakeSMTP().start_wait()
    yield server
    server.stop()


def _smtp_env(monkeypatch, smtp: FakeSMTP, to="ban@example.com,nguoiB@example.com"):
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", str(smtp.port))
    monkeypatch.setenv("SMTP_USER", "sender@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setenv("MAIL_FROM", "sender@example.com")
    monkeypatch.setenv("MAIL_TO", to)
    monkeypatch.setenv("SMTP_SSL", "0")
    monkeypatch.setenv("SMTP_TIMEOUT_SECONDS", "5")


def _decoded_mail(raw: str) -> str:
    """Mail thô (RFC 2047 + base64) -> text giải mã để assert nội dung tiếng Việt."""
    import email
    import email.header

    msg = email.message_from_string(raw)
    parts = []
    # headers
    for name in ("Subject", "To", "From"):
        v = str(msg.get(name, ""))
        try:
            v = str(email.header.make_header(email.header.decode_header(v)))
        except Exception:  # noqa: BLE001
            pass
        parts.append(v)
    # payload từng part (base64/quoted-printable -> utf-8)
    for part in msg.walk():
        payload = part.get_payload(decode=True)
        if payload:
            parts.append(payload.decode("utf-8", "replace"))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestScheduledScan:
    async def test_scan_inserts_new_jobs_and_sends_mail(self, monkeypatch, fake_smtp):
        """Lượt quét: insert job mới -> mail gửi tới MAIL_TO có link job."""
        store = JobStore(":memory:")
        _install_adapters(monkeypatch, [
            make_job(title="Content Marketing Specialist", company_name="Công Ty A",
                     source_job_id="s1"),
            make_job(title="Social Media Executive", company_name="Công Ty B",
                     source_job_id="s2"),
        ])
        _smtp_env(monkeypatch, fake_smtp)
        monkeypatch.setenv("SITE_URL", "https://jobs.example.com")

        res = await run_scheduled_scan(store, with_details=False)

        assert res.ok is True
        assert res.inserted == 2
        assert res.new_jobs and len(res.new_jobs) == 2
        assert res.mail["sent"] is True
        # mail thật đi qua fake SMTP server
        assert len(fake_smtp.sent) == 1
        mail_text = _decoded_mail(fake_smtp.sent[0])
        assert "2 việc làm mới" in mail_text
        assert "Content Marketing Specialist" in mail_text
        assert "Social Media Executive" in mail_text
        assert "jobs.example.com/jobs/" in mail_text
        assert "ban@example.com" in mail_text and "nguoiB@example.com" in mail_text

    async def test_scan_no_new_jobs_no_mail(self, monkeypatch, fake_smtp):
        """Không có job mới -> không gửi mail (nhưng scan vẫn ok)."""
        store = JobStore(":memory:")
        _install_adapters(monkeypatch, [])
        _smtp_env(monkeypatch, fake_smtp)

        res = await run_scheduled_scan(store, with_details=False)

        assert res.ok is True
        assert res.inserted == 0
        assert res.new_jobs == []
        assert res.mail["sent"] is False
        assert len(fake_smtp.sent) == 0

    async def test_scan_hides_expired_and_deletes_old_hidden(self, monkeypatch, fake_smtp):
        """Job hết hạn nộp bị ẩn trong lượt; job đã ẩn quá SCAN_DELETE_AFTER_DAYS
        bị XOÁ HẲN (data chết) trừ job có người lưu."""
        store = JobStore(":memory:")
        # job cũ đã ẩn quá 3 ngày (data chết) -> lượt scan phải xoá
        dead = normalize_job(make_job(title="Old Content Editor", company_name="Cty Cũ",
                                      source_job_id="d1"))
        dead.last_verified_at = utcnow() - timedelta(days=10)
        dead.deadline = utcnow() - timedelta(days=9)  # hạn nộp đã qua
        dead.is_active = False
        dead_id = store.insert_job(dead)
        store.conn.execute("UPDATE jobs SET is_active=0 WHERE job_id=?", (dead_id,))
        # job cũ đã ẩn nhưng CÓ người lưu -> giữ
        saved = normalize_job(make_job(title="Saved Copywriter", company_name="Cty Giữ",
                                       source_job_id="d2"))
        saved.last_verified_at = utcnow() - timedelta(days=10)
        saved.deadline = utcnow() - timedelta(days=9)
        saved.is_active = False
        saved_id = store.insert_job(saved)
        store.conn.execute("UPDATE jobs SET is_active=0 WHERE job_id=?", (saved_id,))
        store.conn.execute(
            "INSERT INTO users(email, token, created_at) VALUES ('x@x.com','tok',?)",
            (utcnow().isoformat(),))
        uid = store.conn.execute("SELECT user_id FROM users WHERE email='x@x.com'").fetchone()[0]
        store.save_job_for_user(uid, saved_id)
        # job hết hạn nộp hôm qua -> bị ẨN trong lượt quét này
        expired = normalize_job(make_job(title="Expired Content Lead", company_name="Cty Hết Hạn",
                                         source_job_id="d3"))
        expired.deadline = utcnow() - timedelta(days=1)
        expired_id = store.insert_job(expired)

        _install_adapters(monkeypatch, [
            make_job(title="Fresh Content Creator", company_name="Cty Mới",
                     source_job_id="n1"),
        ])
        _smtp_env(monkeypatch, fake_smtp)
        monkeypatch.setenv("SCAN_DELETE_AFTER_DAYS", "3")

        res = await run_scheduled_scan(store, with_details=False)

        assert res.ok is True
        assert res.inserted == 1
        assert res.hidden >= 1  # expired job bị ẩn
        # data chết bị xoá hẳn; job có người lưu + job hết hạn mới ẩn vẫn còn
        assert res.deleted["jobs"] == 1
        assert store.get_job(dead_id) is None
        assert store.get_job(saved_id) is not None
        assert store.get_job(expired_id) is not None
        assert store.get_job(expired_id)["is_active"] == 0
        # mail chỉ liệt kê job mới (active), không liệt kê job bị ẩn
        assert len(fake_smtp.sent) == 1
        mail_text = _decoded_mail(fake_smtp.sent[0])
        assert "Fresh Content Creator" in mail_text
        assert "Old Content Editor" not in mail_text

    async def test_scan_purge_disabled_when_zero_days(self, monkeypatch, fake_smtp):
        """SCAN_DELETE_AFTER_DAYS=0 -> tắt xoá hẳn (chỉ ẩn)."""
        store = JobStore(":memory:")
        dead = normalize_job(make_job(title="Old Content Editor", company_name="Cty Cũ",
                                      source_job_id="d1"))
        dead.last_verified_at = utcnow() - timedelta(days=10)
        dead.deadline = utcnow() - timedelta(days=9)
        dead_id = store.insert_job(dead)
        store.conn.execute("UPDATE jobs SET is_active=0 WHERE job_id=?", (dead_id,))

        _install_adapters(monkeypatch, [])
        _smtp_env(monkeypatch, fake_smtp)
        monkeypatch.setenv("SCAN_DELETE_AFTER_DAYS", "0")

        res = await run_scheduled_scan(store, with_details=False)

        assert res.ok is True
        assert res.deleted["jobs"] == 0
        assert store.get_job(dead_id) is not None

    async def test_scan_records_history(self, monkeypatch, fake_smtp):
        """Mỗi lượt scan ghi 1 dòng vào scheduled_scans (xem qua --status)."""
        store = JobStore(":memory:")
        _install_adapters(monkeypatch, [])
        _smtp_env(monkeypatch, fake_smtp)

        res1 = await run_scheduled_scan(store, with_details=False)
        res2 = await run_scheduled_scan(store, with_details=False)

        scans = last_scans(store, limit=10)
        assert len(scans) == 2
        assert scans[0]["ok"] == 1 and scans[1]["ok"] == 1
        assert scans[0]["result"]["inserted"] == 0

    async def test_scan_adapter_error_still_ok(self, monkeypatch, fake_smtp):
        """Nguồn lỗi -> lượt scan vẫn ok (errors được ghi), không sập vòng lặp."""
        store = JobStore(":memory:")
        _install_adapters(monkeypatch, [], error=RuntimeError("nguồn sập"))
        _smtp_env(monkeypatch, fake_smtp)

        res = await run_scheduled_scan(store, with_details=False)

        assert res.ok is True  # lỗi nguồn là bình thường, không phải lỗi scan
        assert res.errors and "nguồn sập" in res.errors[0]
        assert res.mail["sent"] is False  # không có job mới

    async def test_scan_mail_disabled_when_no_config(self, monkeypatch):
        """Chưa cấu hình SMTP -> không gửi mail, scan vẫn chạy ok."""
        store = JobStore(":memory:")
        _install_adapters(monkeypatch, [
            make_job(title="Content Specialist", company_name="Cty X", source_job_id="x1"),
        ])
        monkeypatch.delenv("SMTP_HOST", raising=False)
        monkeypatch.delenv("MAIL_TO", raising=False)

        res = await run_scheduled_scan(store, with_details=False)

        assert res.ok is True
        assert res.inserted == 1
        assert res.mail["sent"] is False
        assert "Chưa cấu hình" in res.mail["message"]

    async def test_scan_skips_llm_cache_orphans(self, monkeypatch, fake_smtp):
        """Cache AI của job bị xoá cũng bị xoá kèm (không để lại rác)."""
        store = JobStore(":memory:")
        dead = normalize_job(make_job(title="Dead Job Content", company_name="Cty Chết",
                                      source_job_id="dd1"))
        dead.last_verified_at = utcnow() - timedelta(days=10)
        dead.deadline = utcnow() - timedelta(days=9)
        dead_id = store.insert_job(dead)
        store.conn.execute("UPDATE jobs SET is_active=0 WHERE job_id=?", (dead_id,))
        store.set_llm_cache(dead_id, "m", "v1", "relevance", {"relevance": 5})

        _install_adapters(monkeypatch, [])
        _smtp_env(monkeypatch, fake_smtp)
        monkeypatch.setenv("SCAN_DELETE_AFTER_DAYS", "3")

        res = await run_scheduled_scan(store, with_details=False)

        assert res.deleted["llm_cache"] == 1  # cache của dead job bị xoá kèm
        n = store.conn.execute("SELECT COUNT(*) c FROM llm_enrichment").fetchone()["c"]
        assert n == 0


class TestMailRender:
    def test_render_email_content(self):
        from agent_search_job.pipeline.mail_notify import render_new_jobs_email

        jobs = [
            {"job_id": 7, "title": "Content Marketing Specialist",
             "company_name": "Công Ty A", "salary_text": "15 - 25 triệu",
             "district": "Quận 1", "role_cluster": "content_marketing",
             "job_level": "junior", "apply_url": "https://src.example/7",
             "source_url": "https://src.example/7"},
            {"job_id": 8, "title": "Copywriter", "company_name": "Công Ty B",
             "salary_text": "", "salary_min_vnd": 10_000_000, "salary_max_vnd": None,
             "district": "", "role_cluster": "copywriter_seo",
             "job_level": "", "apply_url": "", "source_url": ""},
        ]
        subject, html, text = render_new_jobs_email(
            jobs, {"at": "2026-10-21T09:00:00+00:00", "inserted": 2,
                   "updated": 3, "hidden": 1, "deleted": 1, "errors": 0},
            site_url="https://jobs.example.com", max_jobs=30)

        assert "2 việc làm mới" in subject
        assert "Content Marketing Specialist" in html and "Copywriter" in html
        assert "https://jobs.example.com/jobs/7" in html
        assert "https://jobs.example.com/jobs/8" in html
        assert "10 triệu+" in html  # salary từ min/max khi thiếu salary_text
        assert "+2 việc mới" in html
        assert "Content Marketing Specialist" in text
        assert "Copywriter" in text

    def test_render_uses_app_theme_vn_time_and_escapes(self):
        """Template tông hồng như website, giờ Việt Nam thay chuỗi UTC, text từ nguồn tuyển dụng được escape."""
        from agent_search_job.pipeline.mail_notify import render_new_jobs_email

        jobs = [{"job_id": 1, "title": 'Content <script>alert(1)</script> & SEO', "company_name": "Cty <b>X</b>",
                 "salary_text": "15 - 20 Triệu VND", "district": "Quận 3", "role_cluster": "content_marketing",
                 "job_level": "junior", "apply_url": 'https://src.example/1?a=1&b="2"', "source_url": ""}]
        subject, html, text = render_new_jobs_email(
            jobs, {"at": "2026-10-21T09:05:00+00:00", "inserted": 1, "updated": 0}, site_url="")

        assert subject == "[Agent Search Job] 1 việc làm mới · 16:05 · 21/10/2026"
        # thỏ 2D vẫy chào + lời chào xưng "em", gọi "công chúa" như trợ lý trên web
        assert '<img src="cid:bunny-wave"' in html
        assert "Công chúa ơi, nay em tìm được" in html and "1 jobs mới</span> nè" in html
        assert text.startswith("Công chúa ơi, nay em tìm được 1 jobs mới nè")
        assert "Lượt quét định kỳ lúc 16:05 · 21/10/2026" in html and "2026-10-21T09" not in html
        assert "<script>" not in html and "&lt;script&gt;" in html and "Cty &lt;b&gt;X&lt;/b&gt;" in html
        assert 'href="https://src.example/1?a=1&amp;b=&quot;2&quot;"' in html  # link nguồn khi không có SITE_URL
        assert "#fff5f8" in html and "#c2366b" in html and "#ffe6ee" in html  # màu app
        assert "Content Marketing" in html and "1-2 năm" in html and "Quận 3" in html
        assert "cập nhật" not in html.split("<h1")[1].split("</td>")[0]  # chip 0 cập nhật không hiện
        assert "Mở trang tìm việc" not in html  # không SITE_URL -> không có nút về site
        assert "Content <script>alert(1)</script> & SEO — Cty <b>X</b> · 15 - 20 Triệu VND" in text

    def test_notify_embeds_bunny_image_inline(self, monkeypatch, fake_smtp):
        """Mail job mới gửi kèm ảnh thỏ 2D dạng inline (multipart/related, Content-ID khớp cid: trong HTML)."""
        import email
        from agent_search_job.pipeline import mail_notify

        _smtp_env(monkeypatch, fake_smtp, to="ban@example.com")
        ok, _ = mail_notify.notify_new_jobs(
            [{"job_id": 1, "title": "Content Executive", "company_name": "Cty A"}], {"inserted": 1})
        assert ok and len(fake_smtp.sent) == 1
        msg = email.message_from_string(fake_smtp.sent[0])
        related = [p for p in msg.walk() if p.get_content_type() == "multipart/related"]
        assert len(related) == 1
        img = [p for p in msg.walk() if p.get_content_type() == "image/png"]
        assert len(img) == 1 and img[0]["Content-ID"] == "<bunny-wave>"
        assert img[0].get_payload(decode=True) == mail_notify.BUNNY_PNG.read_bytes()
        html = [p for p in msg.walk() if p.get_content_type() == "text/html"][0].get_payload(decode=True).decode()
        assert 'src="cid:bunny-wave"' in html

    def test_render_caps_max_jobs(self):
        from agent_search_job.pipeline.mail_notify import render_new_jobs_email

        jobs = [{"job_id": i, "title": f"Job {i}", "company_name": f"Cty {i}",
                 "salary_text": "", "district": "", "role_cluster": "",
                 "job_level": "", "apply_url": "", "source_url": ""} for i in range(40)]
        subject, html, text = render_new_jobs_email(jobs, {"inserted": 40}, "", max_jobs=30)

        assert "40 việc làm mới" in subject
        assert "Job 29" in html          # 30 job đầu có mặt
        assert "Job 39" not in html      # job thứ 31+ không có
        assert "… và 10 việc nữa" in html

    def test_mail_disabled_without_config(self, monkeypatch):
        from agent_search_job.pipeline import mail_notify

        monkeypatch.delenv("SMTP_HOST", raising=False)
        monkeypatch.delenv("MAIL_TO", raising=False)
        assert mail_notify.mail_enabled() is False

        ok, msg = mail_notify.notify_new_jobs(
            [{"job_id": 1, "title": "T", "company_name": "C"}], {"inserted": 1})
        assert ok is False

    def test_send_mail_smtp_ssl_port_default(self, monkeypatch):
        from agent_search_job.pipeline.mail_notify import _smtp_ssl

        monkeypatch.setenv("SMTP_SSL", "1")
        assert _smtp_ssl() is True
        monkeypatch.setenv("SMTP_SSL", "0")
        assert _smtp_ssl() is False
        monkeypatch.delenv("SMTP_SSL")
        assert _smtp_ssl() is False


class TestScheduledScanConfig:
    def test_interval_default_12(self, monkeypatch):
        monkeypatch.delenv("SCAN_INTERVAL_HOURS", raising=False)
        assert interval_hours() == 12

    def test_interval_env(self, monkeypatch):
        monkeypatch.setenv("SCAN_INTERVAL_HOURS", "6")
        assert interval_hours() == 6

    def test_interval_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("SCAN_INTERVAL_HOURS", "abc")
        assert interval_hours() == 12

    def test_interval_minimum_1(self, monkeypatch):
        monkeypatch.setenv("SCAN_INTERVAL_HOURS", "0")
        assert interval_hours() == 1

    def test_enabled_flag(self, monkeypatch):
        monkeypatch.setenv("SCHEDULED_SCAN_ENABLED", "1")
        assert scheduled_enabled() is True
        monkeypatch.setenv("SCHEDULED_SCAN_ENABLED", "0")
        assert scheduled_enabled() is False
        monkeypatch.delenv("SCHEDULED_SCAN_ENABLED")
        assert scheduled_enabled() is False

    def test_next_scan_at_fixed_times_vn(self, monkeypatch):
        """SCAN_TIMES=08:00,20:00: quét đúng 8h sáng / 8h tối giờ Việt Nam, đúng mốc thì lấy mốc sau."""
        from datetime import datetime, timedelta, timezone
        from agent_search_job.pipeline.scheduled_scan import VN_TZ, next_scan_at
        monkeypatch.setenv("SCAN_TIMES", "08:00, 20:00")
        vn = lambda d, h, m=0: datetime(2026, 10, d, h, m, tzinfo=VN_TZ)
        assert next_scan_at(vn(23, 7, 30)) == vn(23, 8)
        assert next_scan_at(vn(23, 8)) == vn(23, 20)          # đúng 8:00 (vừa chạy) -> 20:00
        assert next_scan_at(vn(23, 8, 20)) == vn(23, 20)
        assert next_scan_at(vn(23, 21)) == vn(24, 8)          # sau 20h -> 8h sáng hôm sau
        # server chạy giờ UTC: 01:00Z = 08:00 VN -> mốc kế là 20:00 VN
        assert next_scan_at(datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc)) == vn(23, 20)
        monkeypatch.setenv("SCAN_TIMES", "8:00")
        assert next_scan_at(vn(23, 9)) == vn(24, 8)

    def test_next_scan_at_falls_back_to_interval(self, monkeypatch):
        """Không có / sai định dạng SCAN_TIMES -> mỗi SCAN_INTERVAL_HOURS giờ như cũ."""
        from datetime import datetime, timedelta
        from agent_search_job.pipeline.scheduled_scan import VN_TZ, next_scan_at
        now = datetime(2026, 10, 23, 10, 0, tzinfo=VN_TZ)
        monkeypatch.setenv("SCAN_INTERVAL_HOURS", "12")
        for times in ("", "25:00,abc", "8h"):
            monkeypatch.setenv("SCAN_TIMES", times)
            assert next_scan_at(now) == now + timedelta(hours=12), times

    async def test_loop_sleeps_until_next_slot_without_startup_scan(self, monkeypatch):
        """SCAN_ON_STARTUP=0 + SCAN_TIMES: không quét lúc khởi động, ngủ tới đúng mốc kế tiếp rồi mới quét."""
        import asyncio
        from datetime import datetime
        from agent_search_job.pipeline import scheduled_scan as mod
        monkeypatch.setenv("SCAN_TIMES", "08:00,20:00")
        monkeypatch.setenv("SCAN_ON_STARTUP", "0")
        runs, sleeps = [], []
        async def fake_scan(store, with_details=True):
            runs.append(1)
        async def fake_sleep(sec):
            sleeps.append(sec)
            raise asyncio.CancelledError
        monkeypatch.setattr(mod, "run_scheduled_scan", fake_scan)
        monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
        with pytest.raises(asyncio.CancelledError):
            await ScheduledScanManager._loop(JobStore(":memory:"))
        expect = (mod.next_scan_at() - datetime.now(mod.VN_TZ)).total_seconds()
        assert runs == [] and len(sleeps) == 1 and abs(sleeps[0] - expect) < 5 and 0 < sleeps[0] <= 12 * 3600

    async def test_manager_loop_disabled(self, monkeypatch):
        """SCHEDULED_SCAN_ENABLED!=1 -> start_loop trả None (không tạo task)."""
        monkeypatch.setenv("SCHEDULED_SCAN_ENABLED", "0")
        store = JobStore(":memory:")
        task = await ScheduledScanManager.start_loop(store)
        assert task is None

    async def test_manager_no_overlap(self, monkeypatch):
        """Đang chạy lượt thì lượt kế bị bỏ qua (không chồng chéo crawl)."""
        monkeypatch.setenv("SCHEDULED_SCAN_ENABLED", "1")
        monkeypatch.setattr(ScheduledScanManager, "_running", True)
        store = JobStore(":memory:")
        # _run_once khi đang _running -> không gọi run_scheduled_scan
        called = []
        from agent_search_job.pipeline import scheduled_scan as mod
        async def fake_scan(s):
            called.append(1)
            return None
        monkeypatch.setattr(mod, "run_scheduled_scan", fake_scan)
        await ScheduledScanManager._run_once(store)
        assert called == []  # bị bỏ qua
        monkeypatch.setattr(ScheduledScanManager, "_running", False)

    async def test_manual_rescan_busy_while_scheduled_running(self, monkeypatch):
        """Đang quét định kỳ thì nút "Quét ngay" báo bận, không mở lượt quét thứ 2."""
        from agent_search_job.pipeline.rescan import RescanManager
        monkeypatch.setattr(ScheduledScanManager, "_running", True)
        monkeypatch.setattr(RescanManager, "_task", None)
        monkeypatch.setattr(RescanManager, "_current", None)
        st = await RescanManager.start(JobStore(":memory:"), "all", "content", 5)
        assert "định kỳ đang chạy" in st.message and RescanManager._task is None

    async def test_scheduled_waits_for_manual_rescan(self, monkeypatch):
        """Đang "Quét ngay" thì lượt định kỳ chờ nó xong rồi mới chạy (không bỏ lượt, không chạy song song)."""
        import asyncio
        from agent_search_job.pipeline import scheduled_scan as mod
        from agent_search_job.pipeline.rescan import RescanManager
        order = []

        async def manual():
            await asyncio.sleep(0.05)
            order.append("manual")
        async def fake_scan(store, with_details=True):
            order.append("scheduled")
        monkeypatch.setattr(RescanManager, "_task", asyncio.create_task(manual()))
        monkeypatch.setattr(mod, "run_scheduled_scan", fake_scan)
        monkeypatch.setattr(ScheduledScanManager, "_running", False)
        await ScheduledScanManager._run_once(JobStore(":memory:"))
        assert order == ["manual", "scheduled"] and ScheduledScanManager._running is False


class TestScheduledScanAPI:
    def test_api_status_endpoint(self, monkeypatch):
        """/api/scheduled-scan/status trả cấu hình + lịch sử."""
        from fastapi.testclient import TestClient
        import agent_search_job.web.app as appmod
        from agent_search_job.web.app import app

        monkeypatch.setenv("SCHEDULED_SCAN_ENABLED", "1")
        monkeypatch.setenv("SCAN_INTERVAL_HOURS", "12")
        # không quét thật khi test: vòng lặp chỉ sleep chờ interval
        monkeypatch.setenv("SCAN_ON_STARTUP", "0")
        monkeypatch.setenv("ALLOWLIST_EMAILS", "")
        appmod._store = None  # reset singleton -> dùng JOBS_DB mới
        with TestClient(app) as client:
            r = client.get("/api/scheduled-scan/status")
            assert r.status_code == 200
            body = r.json()
            assert body["enabled"] is True
            assert body["interval_hours"] == 12
            assert body["running"] is False
            assert isinstance(body["history"], list)

    def test_lifespan_starts_scan_loop(self, monkeypatch, tmp_path):
        """Vòng lặp quét định kỳ chạy trong server: lifespan tạo task, quét 1 lượt
        lúc khởi động (SCAN_ON_STARTUP=1) rồi ngủ — không chặn web."""
        from fastapi.testclient import TestClient
        import agent_search_job.web.app as appmod
        from agent_search_job.web.app import app

        monkeypatch.setenv("SCHEDULED_SCAN_ENABLED", "1")
        monkeypatch.setenv("SCAN_ON_STARTUP", "1")
        monkeypatch.setenv("ALLOWLIST_EMAILS", "")
        monkeypatch.setenv("SCAN_JD_DETAILS", "0")  # test không fetch JD ngoài mạng
        monkeypatch.delenv("SMTP_HOST", raising=False)
        # DB test riêng trong tmp_path — KHÔNG đụng data/jobs.db thật
        monkeypatch.setenv("JOBS_DB", str(tmp_path / "scan-test.db"))
        appmod._store = None
        _install_adapters(monkeypatch, [
            make_job(title="Loop Content Writer", company_name="Cty Loop",
                     source_job_id="lp1"),
        ])

        with TestClient(app) as client:
            # lượt quét chạy nền; poll tới khi lượt đầu HOÀN TẤT (finished_at)
            from agent_search_job.pipeline.scheduled_scan import last_scans
            store = appmod.get_store()
            scans = []
            for _ in range(100):
                scans = last_scans(store, limit=5)
                if scans and scans[0].get("finished_at"):
                    break
                time.sleep(0.1)
            assert scans and scans[0].get("finished_at"), \
                "vòng lặp quét không hoàn tất lượt nào trong 10s"
            assert scans[0]["ok"] == 1
            assert scans[0]["result"]["inserted"] == 1
            # web vẫn phục vụ bình thường trong lúc quét
            assert client.get("/health").status_code == 200


class TestIngestHiddenCounts:
    async def test_pipeline_reports_hidden_counts(self, monkeypatch):
        """CrawlRunResult.hidden_expired/hidden_stale được pipeline điền đúng."""
        from agent_search_job.pipeline.ingest import IngestPipeline

        store = JobStore(":memory:")
        # job hết hạn nộp vẫn active -> lượt quét phải ẩn + báo hidden_expired
        expired = normalize_job(make_job(title="Expired Social Lead",
                                         company_name="Cty Hết Hạn",
                                         source_job_id="e9"))
        expired.deadline = utcnow() - timedelta(days=1)
        store.insert_job(expired)

        pipeline = IngestPipeline(store, [FakeAdapter([
            make_job(title="New Content Exec", company_name="Cty N", source_job_id="n9"),
        ])])
        res = await pipeline.run(queries=["content"], max_results_per_query=10)

        assert res.hidden_expired == 1
        assert res.hidden_stale >= 0
        assert "hidden_expired" in res.to_dict()
        assert res.to_dict()["hidden_expired"] == 1


class TestExpiredPurge:
    """Job quá hạn nộp bị xoá hẳn mỗi EXPIRED_PURGE_HOURS giờ; lượt quét không nạp lại job quá hạn."""

    @staticmethod
    def _add(store, sid, deadline, active=True):
        job = normalize_job(make_job(title=f"Content Executive {sid}", company_name=f"Cty {sid}",
                                     source_job_id=sid, source_url=f"https://example.com/job/{sid}"))
        job.deadline = deadline
        jid = store.insert_job(job)
        if not active:
            with store.conn:
                store.conn.execute("UPDATE jobs SET is_active=0 WHERE job_id=?", (jid,))
        return jid

    def test_purge_deletes_expired_including_saved_and_cache(self):
        from datetime import datetime
        from agent_search_job.pipeline.purge_store import purge_expired_jobs
        from agent_search_job.pipeline.scheduled_scan import VN_TZ
        store = JobStore(":memory:")
        now_vn = datetime.now(VN_TZ)
        expired_shown = self._add(store, "a", now_vn - timedelta(hours=3))  # hạn ghi +07:00, đã qua 3 tiếng
        self._add(store, "b", now_vn - timedelta(days=2), active=False)
        expired_saved = self._add(store, "c", now_vn - timedelta(days=1))
        future = self._add(store, "d", now_vn + timedelta(hours=3))
        no_deadline = self._add(store, "e", None)
        with store.conn:
            store.conn.execute("INSERT INTO users(email, token, created_at) VALUES ('u@x.com', 'tok-u', 'x')")
            uid = store.conn.execute("SELECT user_id FROM users WHERE email='u@x.com'").fetchone()[0]
            store.conn.execute("INSERT INTO saved_jobs(user_id, job_id, saved_at) VALUES (?,?,'x')", (uid, expired_saved))
            store.conn.execute("INSERT INTO llm_enrichment(job_id, model, prompt_version, kind, payload_json, created_at) "
                               "VALUES (?, 'm', 'v1', 'jd', '{}', 'x')", (expired_shown,))

        assert purge_expired_jobs(store) == {"jobs": 3, "saved_removed": 1, "llm_cache": 1}
        assert {r[0] for r in store.conn.execute("SELECT job_id FROM jobs")} == {future, no_deadline}
        assert store.conn.execute("SELECT COUNT(*) FROM saved_jobs").fetchone()[0] == 0
        assert purge_expired_jobs(store) == {"jobs": 0, "saved_removed": 0, "llm_cache": 0}

    def test_hide_expired_compares_real_time_across_timezones(self):
        """Hạn "…T(giờ VN − 3h)+07:00" đã qua thật; so chuỗi ISO với giờ UTC sẽ tưởng còn hạn 4 tiếng nữa."""
        from datetime import datetime
        from agent_search_job.pipeline.scheduled_scan import VN_TZ
        store = JobStore(":memory:")
        now_vn = datetime.now(VN_TZ)
        expired = self._add(store, "x", now_vn - timedelta(hours=3))
        future = self._add(store, "y", now_vn + timedelta(hours=3))
        assert store.mark_expired_inactive()["expired"] == 1
        active = {r[0] for r in store.conn.execute("SELECT job_id FROM jobs WHERE is_active=1")}
        assert expired not in active and future in active

    async def test_ingest_skips_expired_jobs(self):
        from datetime import datetime
        from agent_search_job.pipeline.ingest import IngestPipeline
        old_aware = make_job(title="Social Media Executive", company_name="Cty Cũ", source_job_id="o1")
        old_aware.deadline = utcnow() - timedelta(days=1)
        old_naive = make_job(title="Copywriter Senior", company_name="Cty Cũ 2", source_job_id="o2")
        old_naive.deadline = datetime.now() - timedelta(days=2)  # không múi giờ -> coi là giờ VN
        fresh = make_job(title="Content Executive Mới", company_name="Cty Mới", source_job_id="n1")
        fresh.deadline = utcnow() + timedelta(days=10)
        store = JobStore(":memory:")
        res = await IngestPipeline(store, [FakeAdapter([old_aware, old_naive, fresh])]).run(
            queries=["content"], max_results_per_query=10)
        assert (res.inserted, res.expired_skipped) == (1, 2) and res.to_dict()["expired_skipped"] == 2
        assert [r[0] for r in store.conn.execute("SELECT title FROM jobs")] == ["Content Executive Mới"]

    async def test_purge_loop_runs_and_skips_while_scanning(self, monkeypatch):
        from agent_search_job.pipeline import purge_store
        from agent_search_job.pipeline import scheduled_scan as mod
        from agent_search_job.pipeline.rescan import RescanManager
        calls = []
        monkeypatch.setattr(purge_store, "purge_expired_jobs",
                            lambda s: calls.append(1) or {"jobs": 2, "saved_removed": 0, "llm_cache": 0})
        async def stop(sec):
            assert sec == 6 * 3600
            raise asyncio.CancelledError
        monkeypatch.setattr(mod.asyncio, "sleep", stop)
        monkeypatch.setattr(RescanManager, "_task", None)
        monkeypatch.setattr(RescanManager, "_current", None)
        monkeypatch.setattr(ScheduledScanManager, "last_expired_purge", None)

        monkeypatch.setattr(ScheduledScanManager, "_running", True)  # đang quét định kỳ -> bỏ lượt
        with pytest.raises(asyncio.CancelledError):
            await ScheduledScanManager.purge_expired_loop(JobStore(":memory:"), 6)
        assert calls == [] and ScheduledScanManager.last_expired_purge is None

        monkeypatch.setattr(ScheduledScanManager, "_running", False)
        with pytest.raises(asyncio.CancelledError):
            await ScheduledScanManager.purge_expired_loop(JobStore(":memory:"), 6)
        assert calls == [1] and ScheduledScanManager.last_expired_purge["jobs"] == 2
