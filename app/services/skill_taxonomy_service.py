"""Skill taxonomy normalizer and assessment evaluation services."""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from typing import Any

from app.models.quiz import BLOOM_LEVELS

# Bảng ánh xạ ký tự đặc biệt phổ biến trong lập trình
SPECIAL_CHAR_MAP = {
    "+": "_PLUS_",
    "#": "_SHARP_",
    "&": "_AND_",
    ".": "_DOT_",
    "-": "_",
    "/": "_",
    "\\": "_",
}

DEFAULT_THRESHOLDS: dict[str, int] = {
    "NOVICE": 80,
    "BEGINNER": 75,
    "COMPETENT": 70,
    "PROFICIENT": 65,
    "MASTER": 60,
}

DEFAULT_ANOMALY_THRESHOLD = 80
DEFAULT_MIN_SAMPLE_SIZE = 3


def normalize_skill_code(val: str | None) -> str:
    """Normalize skill name into an uppercase snake_case identifier code."""
    if not val or not val.strip():
        raise ValueError("Skill code không được để trống.")

    s = val.strip()
    # 1. Xử lý đ/Đ tiếng Việt mà NFKD không phân rã được
    s = s.replace("đ", "d").replace("Đ", "D")

    # 2. Xử lý trường hợp đặc biệt dấu chấm ở đầu (ví dụ: .NET -> DOT_NET)
    if s.startswith("."):
        s = "DOT_" + s[1:]

    # 3. Ánh xạ các ký tự đặc biệt
    for char, replacement in SPECIAL_CHAR_MAP.items():
        s = s.replace(char, replacement)

    # 4. NFKD Unicode và bỏ dấu
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")

    # 5. Kiểm tra ký tự lạ không thuộc bảng ánh xạ trước khi xóa (cho phép khoảng trắng)
    unsupported_chars = set(re.findall(r"[^a-zA-Z0-9_\s]", s))
    if unsupported_chars:
        raise ValueError(f"Skill name chứa các ký tự không được hỗ trợ: {unsupported_chars}")

    # 6. Thay thế khoảng trắng và ký tự lạ còn lại thành gạch dưới và viết hoa
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").upper()
    s = re.sub(r"_+", "_", s)  # Gộp nhiều gạch dưới liên tiếp

    if not s:
        raise ValueError(f"Skill code '{val}' không hợp lệ sau khi chuẩn hóa.")

    if len(s) > 100:
        raise ValueError(f"Skill code '{s}' vượt quá độ dài tối đa 100 ký tự. Vui lòng rút gọn tên kỹ năng.")

    return s


def is_level_passed(earned_pts: Decimal, max_pts: Decimal, theta_pct: int) -> bool:
    """Exact decimal evaluation without float rounding errors."""
    if max_pts <= Decimal(0):
        return False
    return (earned_pts * Decimal(100)) >= (Decimal(theta_pct) * max_pts)


def evaluate_skill_taxonomy(
    items_by_skill: dict[str, dict[str, Any]],
    thresholds: dict[str, int] | None = None,
    anomaly_threshold: int = DEFAULT_ANOMALY_THRESHOLD,
    min_sample_size: int = DEFAULT_MIN_SAMPLE_SIZE,
) -> dict[str, Any]:
    """Evaluate skill taxonomy for a set of student answers grouped by skill.

    items_by_skill: {
        skill_code: {
            "skill_id": UUID,
            "skill_name": str,
            "answers": [
                {"difficulty": "NOVICE", "earned_pts": Decimal, "max_pts": Decimal},
                ...
            ]
        }
    }
    """
    cfg_thresholds = dict(thresholds or DEFAULT_THRESHOLDS)
    levels = list(BLOOM_LEVELS)  # ["NOVICE", "BEGINNER", "COMPETENT", "PROFICIENT", "MASTER"]
    report_skills: dict[str, Any] = {}

    for skill_code, skill_data in items_by_skill.items():
        skill_id = str(skill_data.get("skill_id", ""))
        skill_name = skill_data.get("skill_name", skill_code)
        answers = skill_data.get("answers", [])

        # 1. Tổng hợp dữ liệu từng bậc
        breakdown: dict[str, dict[str, Any]] = {}
        counts: dict[str, int] = {lvl: 0 for lvl in levels}
        earned_map: dict[str, Decimal] = {lvl: Decimal(0) for lvl in levels}
        max_map: dict[str, Decimal] = {lvl: Decimal(0) for lvl in levels}

        for ans in answers:
            lvl = ans["difficulty"]
            if lvl in levels:
                counts[lvl] += 1
                earned_map[lvl] += Decimal(str(ans.get("earned_pts", 0)))
                max_map[lvl] += Decimal(str(ans.get("max_pts", 0)))

        # Xác định bậc trần đề thi (bậc cao nhất có câu hỏi)
        ceiling_level = None
        for lvl in reversed(levels):
            if counts[lvl] > 0:
                ceiling_level = lvl
                break

        for lvl in levels:
            c = counts[lvl]
            e = earned_map[lvl]
            m = max_map[lvl]
            theta = cfg_thresholds.get(lvl, 70)

            if c == 0:
                status = "NO_DATA"
                passed = False
                rate = None
            elif c < min_sample_size:
                status = "INSUFFICIENT_DATA"
                passed = is_level_passed(e, m, theta)
                rate = float(round((e / m) * Decimal(100), 2)) if m > 0 else 0.0
            else:
                status = "VALID"
                passed = is_level_passed(e, m, theta)
                rate = float(round((e / m) * Decimal(100), 2)) if m > 0 else 0.0

            breakdown[lvl] = {
                "count": c,
                "earned": float(round(e, 2)),
                "max": float(round(m, 2)),
                "rate": rate,
                "passed": passed,
                "threshold": theta,
                "status": status,
            }

        # 2. Duyệt Cổng Năng Lực (Gating Step)
        achieved_level = "NONE"
        reason = None
        blocked_at = None
        provisional = False
        ceiling_reached = False

        # Khởi đầu tại Novice
        novice_stat = breakdown["NOVICE"]["status"]
        if novice_stat == "NO_DATA":
            reason = "NO_DATA"
        elif novice_stat == "INSUFFICIENT_DATA":
            reason = "INSUFFICIENT_DATA"
            provisional = True
        elif not breakdown["NOVICE"]["passed"]:
            reason = "BELOW_NOVICE_THRESHOLD"
            blocked_at = "NOVICE"
        else:
            achieved_level = "NOVICE"
            for i in range(1, len(levels)):
                lvl = levels[i]
                stat = breakdown[lvl]["status"]
                if stat == "NO_DATA":
                    break
                if stat == "INSUFFICIENT_DATA":
                    provisional = True
                    break
                if breakdown[lvl]["passed"]:
                    achieved_level = lvl
                else:
                    blocked_at = lvl
                    break

        # Kiểm tra Chạm Trần Đề Thi
        if ceiling_level and achieved_level == ceiling_level:
            ceiling_reached = True
            provisional = False

        # 3. Phát hiện Bất thường & Lỗ hổng
        has_foundation_gap = False
        is_anomaly = False

        if blocked_at is not None:
            blocked_idx = levels.index(blocked_at)
            for j in range(blocked_idx + 1, len(levels)):
                higher_lvl = levels[j]
                h_b = breakdown[higher_lvl]
                # has_foundation_gap kích hoạt khi đạt ngưỡng thông thường
                if h_b["passed"]:
                    has_foundation_gap = True
                    # is_anomaly đòi hỏi mẫu VALID và đạt ngưỡng anomaly nghiêm ngặt (>= 80%)
                    eff_anomaly_theta = max(h_b["threshold"], anomaly_threshold)
                    if h_b["status"] == "VALID" and is_level_passed(
                        earned_map[higher_lvl], max_map[higher_lvl], eff_anomaly_theta
                    ):
                        is_anomaly = True

        # Invariant check
        assert not is_anomaly or has_foundation_gap, "Invariant violated: is_anomaly must imply has_foundation_gap"

        # 4. Tính toán Chỉ số Radar (Proficiency Index)
        base_score = 0.0 if achieved_level == "NONE" else float((levels.index(achieved_level) + 1) * 20)
        bonus = 0.0

        if achieved_level != "MASTER":
            next_idx = 0 if achieved_level == "NONE" else levels.index(achieved_level) + 1
            if next_idx < len(levels):
                next_lvl = levels[next_idx]
                next_stat = breakdown[next_lvl]["status"]
                if next_stat == "VALID":
                    r_next = breakdown[next_lvl]["rate"] or 0.0
                    theta_next = cfg_thresholds.get(next_lvl, 70)
                    bonus = min(r_next, float(theta_next)) * (20.0 / float(theta_next))

        radar_value = round(min(100.0, base_score + bonus), 2)

        report_skills[skill_code] = {
            "skill_id": skill_id,
            "skill_code": skill_code,
            "skill_name": skill_name,
            "achieved_level": achieved_level,
            "reason": reason,
            "blocked_at": blocked_at,
            "ceiling_level": ceiling_level,
            "ceiling_reached": ceiling_reached,
            "provisional": provisional,
            "radar_value": radar_value,
            "has_foundation_gap": has_foundation_gap,
            "is_anomaly": is_anomaly,
            "level_breakdown": breakdown,
        }

    return {
        "version": "1.4.1",
        "config": {
            "thresholds": cfg_thresholds,
            "anomaly_threshold": anomaly_threshold,
            "min_sample_size": min_sample_size,
        },
        "skills": report_skills,
    }
