# 折扣计算规格 v1.2

## SPEC-DISCOUNT-001 基础折扣

普通会员订单按原价结算，不接受任何折扣参数。

## SPEC-DISCOUNT-002 VIP 折扣范围

VIP 会员订单享受 `discount_rate` 折扣，取值范围为 **[0, 0.30]**（含 0 与 0.30）。

- `discount_rate = 0` 表示不打折，按原价结算。
- `discount_rate = 0.30` 表示最高档折扣，超出该值即违反本规格。

## SPEC-DISCOUNT-003 超限必须拒绝

当调用方传入的 `discount_rate` 大于 0.30 时，实现必须抛出 `ValueError`，
且异常信息需要包含 `discount rate exceeds limit`。不允许静默截断为 0.30，
也不允许按 0.30 结算后返回。

## SPEC-DISCOUNT-004 负值必须拒绝

当 `discount_rate` 小于 0 时，实现必须抛出 `ValueError`。

## SPEC-DISCOUNT-005 金额精度

折扣后金额保留两位小数，采用四舍五入（ROUND_HALF_UP）。

## SPEC-DISCOUNT-006 金额边界

订单原始金额必须为正数（> 0），否则抛出 `ValueError`。
原始金额为 0 或负数时不得进入折扣计算分支。
