"""Unit tests for skill taxonomy normalizer and evaluation engine."""

from decimal import Decimal

import pytest

from app.services.skill_taxonomy_service import (
    evaluate_skill_taxonomy,
    normalize_skill_code,
)


def test_normalizer_collision():
    # Kiểm tra các chuỗi đặc thù
    assert normalize_skill_code("C++") == "C_PLUS_PLUS"
    assert normalize_skill_code("C#") == "C_SHARP"
    assert normalize_skill_code("C") == "C"
    assert normalize_skill_code(".NET") == "DOT_NET"
    assert normalize_skill_code("NET") == "NET"
    assert normalize_skill_code("Node.js") == "NODE_DOT_JS"
    assert normalize_skill_code("HTML & CSS") == "HTML_AND_CSS"
    assert normalize_skill_code("  Điều kiện rẽ nhánh  ") == "DIEU_KIEN_RE_NHANH"

    # Kiểm tra ký tự lạ bị từ chối
    with pytest.raises(ValueError, match="không được hỗ trợ"):
        normalize_skill_code("C!")


def test_gated_evaluation_emax_table():
    # Test k = 3 với các ngưỡng: Novice 80% (3/3), Proficient 65% (2/3), Master 60% (2/3)
    items = {
        "PY_BASIC": {
            "skill_name": "Python Basic",
            "answers": [
                # Novice 3/3 -> pass
                {"difficulty": "NOVICE", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
                {"difficulty": "NOVICE", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
                {"difficulty": "NOVICE", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
                # Beginner 3/3 -> pass
                {"difficulty": "BEGINNER", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
                {"difficulty": "BEGINNER", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
                {"difficulty": "BEGINNER", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
                # Competent 2/3 (66.7% < 70%) -> fail
                {"difficulty": "COMPETENT", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
                {"difficulty": "COMPETENT", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
                {"difficulty": "COMPETENT", "earned_pts": Decimal("0.0"), "max_pts": Decimal("1.0")},
                # Master 2/3 (66.7% >= 60%) -> pass
                {"difficulty": "MASTER", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
                {"difficulty": "MASTER", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
                {"difficulty": "MASTER", "earned_pts": Decimal("0.0"), "max_pts": Decimal("1.0")},
            ],
        }
    }

    report = evaluate_skill_taxonomy(items)
    res = report["skills"]["PY_BASIC"]

    # Đạt Novice, Beginner -> chặn tại Competent
    assert res["achieved_level"] == "BEGINNER"
    assert res["blocked_at"] == "COMPETENT"
    # Đúng Master 2/3 (66.7% >= 60%) -> có gap
    assert res["has_foundation_gap"] is True
    # Nhưng 2/3 = 66.7% < 80% (ngưỡng anomaly) -> không bị gắn anomaly
    assert res["is_anomaly"] is False


def test_gated_evaluation_anomaly_trigger():
    # Học viên rớt Novice 50% nhưng làm đúng 100% (3/3) câu Master -> kích hoạt anomaly!
    items = {
        "PY_ADVANCED": {
            "skill_name": "Python Advanced",
            "answers": [
                # Novice 2/4 (50% < 80%)
                {"difficulty": "NOVICE", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
                {"difficulty": "NOVICE", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
                {"difficulty": "NOVICE", "earned_pts": Decimal("0.0"), "max_pts": Decimal("1.0")},
                {"difficulty": "NOVICE", "earned_pts": Decimal("0.0"), "max_pts": Decimal("1.0")},
                # Master 3/3 (100% >= 80%)
                {"difficulty": "MASTER", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
                {"difficulty": "MASTER", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
                {"difficulty": "MASTER", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
            ],
        }
    }

    report = evaluate_skill_taxonomy(items)
    res = report["skills"]["PY_ADVANCED"]

    assert res["achieved_level"] == "NONE"
    assert res["reason"] == "BELOW_NOVICE_THRESHOLD"
    assert res["blocked_at"] == "NOVICE"
    assert res["has_foundation_gap"] is True
    assert res["is_anomaly"] is True

    # Radar: Base = 0, Bonus Novice: 50% * 20 / 80% = 12.5
    assert res["radar_value"] == 12.5


def test_ceiling_reached():
    # Đề thi chỉ đo Novice và Beginner với k=3 (mẫu VALID), học viên làm đúng cả 2
    items = {
        "SHORT_QUIZ": {
            "answers": [
                {"difficulty": "NOVICE", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
                {"difficulty": "NOVICE", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
                {"difficulty": "NOVICE", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
                {"difficulty": "BEGINNER", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
                {"difficulty": "BEGINNER", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
                {"difficulty": "BEGINNER", "earned_pts": Decimal("1.0"), "max_pts": Decimal("1.0")},
            ]
        }
    }

    report = evaluate_skill_taxonomy(items)
    res = report["skills"]["SHORT_QUIZ"]

    assert res["achieved_level"] == "BEGINNER"
    assert res["ceiling_level"] == "BEGINNER"
    assert res["ceiling_reached"] is True
    assert res["provisional"] is False
    assert res["radar_value"] == 40.0
