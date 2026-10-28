"""金额计算测试：包含目标测试（当前应失败）与回归测试（必须保持通过）。"""

from decimal import Decimal

import pytest

from pricing import apply_discount


# ---------------- 目标测试：SPEC-DISCOUNT-003 超限必须拒绝 ----------------
# 修改前应 FAILED（当前实现会把 0.5 当成 50% 折扣算下去）


def test_vip_discount_over_limit_rejected():
    with pytest.raises(ValueError) as exc:
        apply_discount(Decimal("100.00"), Decimal("0.50"), is_vip=True)
    assert "discount rate exceeds limit" in str(exc.value)


def test_vip_discount_slightly_over_limit_rejected():
    with pytest.raises(ValueError) as exc:
        apply_discount(Decimal("100.00"), Decimal("0.3001"), is_vip=True)
    assert "discount rate exceeds limit" in str(exc.value)


# ---------------- 回归测试：以下行为修改后必须仍然通过 ----------------


def test_vip_boundary_discount_allowed():
    """SPEC-DISCOUNT-002：0.30 是合法边界值。"""
    result = apply_discount(Decimal("100.00"), Decimal("0.30"), is_vip=True)
    assert result == Decimal("70.00")


def test_vip_zero_discount():
    """SPEC-DISCOUNT-002：0 表示不打折。"""
    result = apply_discount(Decimal("100.00"), Decimal("0"), is_vip=True)
    assert result == Decimal("100.00")


def test_vip_rounding_half_up():
    """SPEC-DISCOUNT-005：两位小数、四舍五入。"""
    result = apply_discount(Decimal("10.05"), Decimal("0.10"), is_vip=True)
    assert result == Decimal("9.05")


def test_normal_member_no_discount():
    """SPEC-DISCOUNT-001：普通会员按原价。"""
    assert apply_discount(Decimal("88.80"), Decimal("0"), is_vip=False) == Decimal("88.80")


def test_normal_member_with_discount_rejected():
    with pytest.raises(ValueError):
        apply_discount(Decimal("100.00"), Decimal("0.10"), is_vip=False)


def test_negative_discount_rejected():
    """SPEC-DISCOUNT-004：负值必须拒绝。"""
    with pytest.raises(ValueError):
        apply_discount(Decimal("100.00"), Decimal("-0.1"), is_vip=True)


def test_amount_must_be_positive():
    """SPEC-DISCOUNT-006：金额必须为正。"""
    with pytest.raises(ValueError):
        apply_discount(Decimal("0"), Decimal("0.1"), is_vip=True)
    with pytest.raises(ValueError):
        apply_discount(Decimal("-5.00"), Decimal("0.1"), is_vip=True)
