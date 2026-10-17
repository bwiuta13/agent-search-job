"""Chuẩn hóa salary, kinh nghiệm, location và phân loại role cluster.

Rule-based theo docs/research-website.md mục 6-7:
- role cluster dùng từ khóa vi/en, LLM không cần ở MVP;
- location TP.HCM giữ cả text nguồn lẫn bản chuẩn hóa;
- salary VND/tháng với cả min/max.
"""
from __future__ import annotations

import html
import re
from typing import Optional

from .models import (
    ROLE_CLUSTER_KEYWORDS,
    RoleCluster,
    ROLE_CLUSTER_LABELS_VI,
    WorkMode,
    NormalizedJob,
)

# ---------------------------------------------------------------------------
# Salary normalization
# ---------------------------------------------------------------------------

_MILLION = 1_000_000
# "10 - 15 triệu", "10-15TR", "Từ 8 triệu", "Trên 20 triệu", "12tr-20tr"
_SAL_RANGE_RE = re.compile(
    r"(?:từ\s*)?(\d+(?:[.,]\d+)?)\s*(?:-|–|—|to|\+)\s*(\d+(?:[.,]\d+)?)\s*(triệu|tr|trieu|m|k)\b",
    re.I,
)
_SAL_FROM_RE = re.compile(r"(?:từ|from)\s*(\d+(?:[.,]\d+)?)\s*(triệu|tr|trieu)\b", re.I)
_SAL_ABOVE_RE = re.compile(r"(?:trên|tối thiểu|above|upto|up to|до)\s*(\d+(?:[.,]\d+)?)\s*(triệu|tr|trieu)\b", re.I)
# VND thô: "15000000" hoặc "15,000,000 VND"
_SAL_VND_RE = re.compile(r"(\d{2,3}(?:[.,]\d{3})+|\d{8,12})\s*(?:vnd|đ|d)?\b", re.I)
_NEGOTIABLE = ("thoả thuận", "thỏa thuận", "thương lượng", "negotiable", "cạnh tranh", "competitive")


def _to_float(s: str) -> float:
    return float(s.replace(",", "."))


def parse_salary(text: str) -> tuple[Optional[int], Optional[int], str]:
    """Trả về (min_vnd, max_vnd, salary_text sạch).

    Thiếu max thì max = min. Không parse được -> (None, None, text gốc).
    """
    if not text:
        return None, None, ""
    t = text.strip()
    low = t.lower()
    if any(k in low for k in _NEGOTIABLE):
        return None, None, t

    m = _SAL_RANGE_RE.search(low)
    if m:
        lo, hi, unit = _to_float(m.group(1)), _to_float(m.group(2)), m.group(3).lower()
        if unit == "k":  # hiếm: 15k-20k USD? bỏ qua vì hiếm trong VN listings
            return None, None, t
        return int(lo * _MILLION), int(hi * _MILLION), t
    m = _SAL_FROM_RE.search(low)
    if m:
        v = int(_to_float(m.group(1)) * _MILLION)
        return v, None, t
    m = _SAL_ABOVE_RE.search(low)
    if m:
        v = int(_to_float(m.group(1)) * _MILLION)
        return v, None, t
    # VND thô
    for m in _SAL_VND_RE.finditer(t):
        raw = re.sub(r"[.,]", "", m.group(1))
        v = int(raw)
        if 2_000_000 <= v <= 300_000_000:  # hợp lý cho lương tháng VND
            return v, None, t
    return None, None, t


# ---------------------------------------------------------------------------
# Experience normalization
# ---------------------------------------------------------------------------

_EXP_RANGE_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:-|–|to)\s*(\d+(?:[.,]\d+)?)\s*(?:năm|nam|year|yr)s?", re.I
)
_EXP_SINGLE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*\+?\s*(?:năm|nam|year|yr)s?", re.I)
_EXP_PLUS_RE = re.compile(r"(?:trên|above|at least)\s*(\d+(?:[.,]\d+)?)\s*(?:năm|nam|year)s?", re.I)
_EXP_NONE = ("chưa yêu cầu", "không yêu cầu kinh nghiệm", "fresher", "intern", "thực tập", "sinh viên")


def parse_experience(text: str) -> tuple[Optional[float], Optional[float]]:
    """Trả về (min_years, max_years); None nếu không có."""
    if not text:
        return None, None
    low = text.lower()
    if any(k in low for k in _EXP_NONE):
        return 0.0, None
    m = _EXP_PLUS_RE.search(low)
    if m:
        return float(m.group(1).replace(",", ".")), None
    m = _EXP_RANGE_RE.search(low)
    if m:
        lo = float(m.group(1).replace(",", "."))
        hi = float(m.group(2).replace(",", "."))
        return lo, hi
    m = _EXP_SINGLE_RE.search(low)
    if m:
        v = float(m.group(1).replace(",", "."))
        # "2 năm kinh nghiệm" -> tối thiểu 2 năm
        return v, None
    return None, None


# ---------------------------------------------------------------------------
# Location normalization (TP.HCM focus)
# ---------------------------------------------------------------------------

HCM_NAMES = {"hồ chí minh", "ho chi minh", "hcm", "tp.hcm", "tp hcm", "tp. hcm", "sài gòn", "saigon", "saigon"}
# Quận/huyện cũ + phường/xã mới sau sáp nhập 2025 (nhóm chính)
HCM_DISTRICTS = [
    # quận cũ
    "quận 1", "quận 2", "quận 3", "quận 4", "quận 5", "quận 6", "quận 7", "quận 8",
    "quận 9", "quận 10", "quận 11", "quận 12", "bình thạnh", "gò vấp", "phú nhuận",
    "tân bình", "tân phú", "thủ đức", "binh thanh", "go vap", "phu nhuan",
    "tan binh", "tan phu", "thu duc",
    # huyện cũ
    "củ chi", "củ chi", "hóc môn", "bình chánh", "nhà bè", "cần giờ",
    # phường mới 2025
    "phường bến nghé", "phường nữa", "phường trường thọ", "phường an khánh",
    "phường an phú", "phường sài gòn",
]
# Map biến thể tên quận (k nằm trong chuỗi address/district text)
_DISTRICT_CANON: dict[str, str] = {
    "q1": "Quận 1", "q.1": "Quận 1", "quận 1": "Quận 1",
    "q3": "Quận 3", "quận 3": "Quận 3", "q7": "Quận 7", "quận 7": "Quận 7",
    "q10": "Quận 10", "quận 10": "Quận 10",
    "binh thanh": "Bình Thạnh", "bình thạnh": "Bình Thạnh",
    "go vap": "Gò Vấp", "gò vấp": "Gò Vấp",
    "phu nhuan": "Phú Nhuận", "phú nhuận": "Phú Nhuận",
    "tan binh": "Tân Bình", "tân bình": "Tân Bình",
    "tan phu": "Tân Phú", "tân phú": "Tân Phú",
    "thu duc": "Thủ Đức", "thủ đức": "Thủ Đức",
    "cu chi": "Củ Chi", "củ chi": "Củ Chi",
    "hoc mon": "Hóc Môn", "hóc môn": "Hóc Môn",
    "binh chanh": "Bình Chánh", "bình chánh": "Bình Chánh",
    "nha be": "Nhà Bè", "nhà bè": "Nhà Bè",
    "can gio": "Cần Giờ", "cần giờ": "Cần Giờ",
}


def is_hcm(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(n in low for n in HCM_NAMES)


def parse_district(text: str) -> str:
    """Trích quận/huyện (hoặc phường mới) từ text địa chỉ tự do."""
    if not text:
        return ""
    low = text.lower()
    for key, canon in _DISTRICT_CANON.items():
        if key in low:
            return canon
    for d in HCM_DISTRICTS:
        if d in low:
            return d.title()
    return ""


def parse_work_mode(text: str, title: str = "") -> str:
    low = f"{text} {title}".lower()
    if "remote" in low or "từ xa" in low or "làm từ xa" in low:
        return WorkMode.REMOTE.value
    if "hybrid" in low or "kết hợp" in low or "linh hoạt" in low:
        return WorkMode.HYBRID.value
    return WorkMode.ONSITE.value


# ---------------------------------------------------------------------------
# Role cluster classification (rule-based)
# ---------------------------------------------------------------------------

def classify_role(title: str, description: str = "") -> tuple[str, float]:
    """Trả về (role_cluster, confidence 0-1). Match title nhiều hơn description."""
    if not title:
        return RoleCluster.CONTENT_MARKETING.value, 0.0
    t = title.lower()
    d = (description or "").lower()[:500]
    scores: dict[str, float] = {}
    for cluster, kws in ROLE_CLUSTER_KEYWORDS.items():
        s = 0.0
        for kw in kws:
            # keyword càng dài (nhiều từ) càng đặc trưng cho role -> trọng số cao
            w = 1.0 + 0.5 * (len(kw.split()) - 1)
            if kw in t:
                s += 2.0 * w
            elif kw in d:
                s += 0.5 * w
        if s > 0:
            scores[cluster] = s
    if not scores:
        # fallback: keyword generic "content"/"marketing" -> content marketing
        if "content" in t or "nội dung" in t or "marketing" in t:
            return RoleCluster.CONTENT_MARKETING.value, 0.4
        return RoleCluster.CONTENT_MARKETING.value, 0.0
    best = max(scores, key=scores.get)
    # confidence: chuẩn hóa theo điểm, title-match thuần -> cao
    conf = min(0.95, 0.5 + 0.15 * scores[best])
    return best, round(conf, 2)


def extract_platforms(text: str) -> list[str]:
    from .models import PLATFORMS
    low = text.lower()
    return [p for p in PLATFORMS if p in low]


def extract_tools(text: str) -> list[str]:
    from .models import CREATIVE_TOOLS
    low = text.lower()
    return [t for t in CREATIVE_TOOLS if t in low]


def detect_portfolio_required(text: str) -> Optional[bool]:
    low = text.lower()
    has = "portfolio" in low or "hồ sơ năng lực" in low or "demo" in low or "showreel" in low
    if not has:
        return None
    return True


# Cấp bậc theo text nguồn (vi) - hard-code mapping từng nguồn
# Lưu ý THỨ TỰ: từ khóa cấp cao/specific đứng TRƯỚC, từ khóa role chung
# (executive/officer/nhân viên...) đứng CUỐI để "Senior Executive" vẫn ra senior,
# "Content Lead" vẫn ra lead.
# keyword role-generic: chỉ dùng khi KHÔNG có keyword cấp cao nào trong title
# ("Senior Executive" -> senior, không thêm junior từ 'executive')
_GENERIC_ROLE_KEYWORDS: list[tuple[str, str]] = [
    ("executive", "junior"), ("officer", "junior"), ("collaborator", "junior"),
    ("nhân viên", "junior"), ("creator", "junior"), ("writer", "junior"),
    ("editor", "junior"), ("biên tập", "junior"), ("biên kịch", "junior"),
    ("copywriter", "junior"), ("thực thi", "junior"),
    ("specialist", "middle"), ("chuyên viên", "middle"),
    ("strategist", "middle"), ("planner", "middle"),
]

_LEVEL_KEYWORDS: list[tuple[str, str]] = [
    # intern
    ("thực tập sinh", "intern"), ("intern", "intern"), ("thực tập", "intern"),
    # senior/lead/manager - cụ thể trước
    ("senior", "senior"),
    ("trưởng nhóm", "lead"), ("team lead", "lead"), ("leader", "lead"),
    ("supervisor", "lead"), ("giám sát", "lead"), ("lead", "lead"),
    ("trưởng phòng", "manager"), ("giám đốc", "manager"),
    ("manager", "manager"), ("head of", "manager"), ("director", "manager"),
    ("quản lý", "manager"), ("quản trị", "manager"),
    # middle
    ("middle", "middle"), ("specialist", "middle"), ("chuyên viên", "middle"),
    ("strategist", "middle"), ("planner", "middle"),
    # junior/fresher cụ thể
    ("junior", "junior"), ("fresher", "fresher"),
    ("chưa có kinh nghiệm", "fresher"), ("không yêu cầu kinh nghiệm", "fresher"),
]


def classify_job_levels(title: str, extra_text: str = "",
                        exp_min: Optional[float] = None,
                        exp_max: Optional[float] = None) -> list[str]:
    """Cấp bậc NHIỀU giá trị: 1 job có thể match nhiều cấp (search cả 2 loại).

    Nguồn tín hiệu (union kết quả):
    1. keyword trong title ("Social & Content Lead" -> lead)
    2. số năm KN từ JD (2 năm -> middle)
    -> "Content Executive" 2 năm = [junior, middle] - search level nào cũng ra.

    Thứ tự trả về: cao -> thấp (phần tử đầu là cấp chính để hiển thị).
    """
    levels: list[str] = []
    t = (title or "").lower()
    for kw, level in _LEVEL_KEYWORDS:
        if kw in t and level not in levels:
            levels.append(level)
    if not levels:
        # role-generic (executive/specialist/collaborator...) chỉ khi không có
        # keyword cấp nào trong title - "Senior Executive" vẫn chỉ là senior
        for kw, level in _GENERIC_ROLE_KEYWORDS:
            if kw in t and level not in levels:
                levels.append(level)
    if not levels:
        x = (extra_text or "").lower()[:800]
        for kw, level in _LEVEL_KEYWORDS:
            if kw in x and level not in levels:
                levels.append(level)
    # tín hiệu từ số năm KN (luôn xét thêm - thêm level nếu chưa có)
    if exp_min is not None or exp_max is not None:
        mid = ((exp_min + exp_max) / 2 if (exp_min is not None and exp_max is not None)
               else (exp_min if exp_min is not None else exp_max))
        # mid=0 (JD parse "0 năm" mơ hồ) không thêm fresher cho job senior/manager
        if mid is not None and mid > 0:
            if mid < 1:
                exp_level = "fresher"
            elif mid < 2:
                exp_level = "junior"
            elif mid < 4:
                exp_level = "middle"
            else:
                exp_level = "senior"
            if exp_level not in levels:
                levels.append(exp_level)
    # giữ thứ tự tín hiệu: title match trước (cấp chính để hiển thị),
    # level từ số năm KN nối sau (search vẫn match cả hai)
    return levels


def classify_job_level(title: str, extra_text: str = "",
                       exp_min: Optional[float] = None,
                       exp_max: Optional[float] = None) -> str:
    """Cấp bậc chính (phần tử đầu của classify_job_levels) - tương thích ngược."""
    return (classify_job_levels(title, extra_text, exp_min, exp_max) or [""])[0]


def normalize_job(job: NormalizedJob) -> NormalizedJob:
    """Chạy mọi normalize step lên một job đã parse thô."""
    # Nguồn (attr HTML TopCV, JSON VietnamWorks) trả text còn entity "&amp;" -> unescape 1 lần
    # trước mọi bước khác để dedupe_key và text hiển thị dùng "&" thật.
    for f in ("title", "company_name", "salary_text", "address", "city", "district",
              "description", "requirements"):
        setattr(job, f, html.unescape(getattr(job, f) or ""))
    job.benefits = [html.unescape(b) for b in job.benefits]
    lo, hi, text = parse_salary(job.salary_text or "")
    job.salary_min_vnd, job.salary_max_vnd, job.salary_text = lo, hi, text
    if not job.experience_years_min and not job.experience_years_max:
        exp_src = f"{job.requirements} {job.description}"
        job.experience_years_min, job.experience_years_max = parse_experience(exp_src)
    job.is_hcm = is_hcm(f"{job.address} {job.city} {job.district}")
    if not job.district:
        job.district = parse_district(job.address or job.city)
    job.work_mode = parse_work_mode(f"{job.description} {job.requirements}", job.title)
    job.job_level = ",".join(classify_job_levels(
        job.title, f"{job.requirements} {job.description}",
        job.experience_years_min, job.experience_years_max,
    ))
    cluster, conf = classify_role(job.title, job.description)
    job.role_cluster, job.role_cluster_confidence = cluster, conf
    blob = f"{job.title} {job.description} {job.requirements}"
    job.platforms = extract_platforms(blob)
    job.creative_tools = extract_tools(blob)
    job.portfolio_required = detect_portfolio_required(blob)
    if not job.deadline:  # mặc định 30 ngày nếu không rõ
        from datetime import timedelta
        from .models import utcnow
        job.deadline = utcnow() + timedelta(days=30)
    return job


def dedupe_key(title: str, company: str) -> str:
    """Fingerprint chống trùng: title+company đã chuẩn (lower, bỏ dấu, bỏ khoảng trắng)."""
    import unicodedata
    def clean(s: str) -> str:
        s = unicodedata.normalize("NFD", s.lower())
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        s = re.sub(r"[^a-z0-9]+", " ", s).strip()
        return re.sub(r"\s+", " ", s)
    # bỏ một số từ noise
    noise = ["tai", "ho chi minh", "ha noi", "hcm", "muc luong", "thu nhap", "urgent", "hot"]
    words = [w for w in clean(f"{title}|{company}").split() if w not in noise]
    return " ".join(words)


def label_role_cluster(cluster: str) -> str:
    return ROLE_CLUSTER_LABELS_VI.get(cluster, cluster)
