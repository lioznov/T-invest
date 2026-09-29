"""Whole-lot position arithmetic for a user-specified hypothetical trade."""
from decimal import Decimal, InvalidOperation, ROUND_FLOOR


def position_size(capital, risk_pct, allocation_pct, entry, stop, target, lot,
                  fee_bps=5, slippage_bps=5):
    values = (capital, risk_pct, allocation_pct, entry, stop, target, lot, fee_bps, slippage_bps)
    try:
        capital, risk_pct, allocation_pct, entry, stop, target, lot, fee, slip = (
            Decimal(str(value)) for value in values)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Заполни поля расчёта числами.") from None
    if not all(value.is_finite() for value in (capital, risk_pct, allocation_pct, entry, stop, target, lot, fee, slip)):
        raise ValueError("В расчёте допустимы только конечные числа.")
    if not Decimal('1') <= capital <= Decimal('10000000000'):
        raise ValueError("Капитал должен быть от 1 до 10 миллиардов рублей.")
    if not Decimal('0.01') <= risk_pct <= 5 or not 1 <= allocation_pct <= 100:
        raise ValueError("Риск: от 0,01 до 5% капитала; доля позиции: от 1 до 100%.")
    if not Decimal('0.000000001') <= stop < entry < target <= Decimal('1000000000'):
        raise ValueError("Для покупки нужны положительные цены: стоп ниже входа, цель выше входа.")
    if any(price != price.quantize(Decimal('0.000000001')) for price in (entry, stop, target)):
        raise ValueError("Укажи цены с точностью до 9 знаков после запятой.")
    if lot != lot.to_integral_value() or not 1 <= lot <= 1000000:
        raise ValueError("В лоте должно быть целое число акций от 1 до 1 000 000.")
    if not 0 <= fee <= 100 or not 0 <= slip <= 100:
        raise ValueError("Комиссия и проскальзывание: от 0 до 1% за каждую сторону.")

    fee, slip = fee / 10000, slip / 10000
    entry_cost = entry * (1 + slip) * (1 + fee)
    stop_value = stop * (1 - slip) * (1 - fee)
    target_value = target * (1 - slip) * (1 - fee)
    per_share_loss = entry_cost - stop_value
    risk_budget = capital * risk_pct / 100
    allocation_budget = capital * allocation_pct / 100
    lots = int(min(risk_budget / (lot * per_share_loss),
                   allocation_budget / (lot * entry_cost)).to_integral_value(rounding=ROUND_FLOOR))
    shares = lots * int(lot)
    loss, cost, profit = shares * per_share_loss, shares * entry_cost, shares * (target_value - entry_cost)
    message = ("Бюджета риска или доли капитала не хватает даже на один лот." if not lots else
               "Цель после расходов не покрывает стоимость сделки." if profit <= 0 else
               "Размер округлён вниз до целых лотов и ограничен двумя заданными лимитами.")
    def money(value):
        return float(value.quantize(Decimal('.01')))
    return {
        "status": "ok", "lots": lots, "shares": shares,
        "position_cost": money(cost), "planned_loss": money(loss),
        "risk_budget": money(risk_budget), "allocation_budget": money(allocation_budget),
        "expected_target_profit": money(profit),
        "reward_risk": float(profit / loss) if loss else None,
        "effective_risk_pct": float(loss / capital * 100),
        "message": message,
        "assumptions": "Комиссия и проскальзывание учтены на входе и выходе. Это расчёт по заданным ценам: при скачке цены выход ниже стопа может увеличить убыток. Налоги не учтены.",
    }
