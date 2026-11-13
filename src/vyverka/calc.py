import math
import statistics

from .config import rules
from .models import CalcStep, Calculation
from .words import plural


def _fmt(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ").replace(".", ",")


def _pct(x: float, nd: int) -> str:
    return f"{x:.{nd}f}".replace(".", ",")


def stats(prices: list[float]) -> tuple[float, float, float]:
    mean = sum(prices) / len(prices)
    std = statistics.stdev(prices) if len(prices) > 1 else 0.0
    cv = std / mean * 100 if mean else 0.0
    return mean, std, cv


def calculate(prices: dict[str, float], qty: float) -> tuple[Calculation, dict[str, str]]:
    """Метод сопоставимых рыночных цен: средняя арифметическая однородной совокупности (CV ≤ предела).
    Возвращает расчёт и словарь offer_id -> причина исключения на этапе расчёта."""
    m = rules().get("method", {})
    need, limit = int(m.get("min_offers", 3)), float(m.get("cv_limit", 33))
    k, max_share = float(m.get("outlier_mad_k", 3.0)), float(m.get("max_outlier_share", 0.34))
    nd = int(m.get("rounding", 2))
    steps: list[CalcStep] = []
    excluded: dict[str, str] = {}
    pool = dict(prices)
    start_n = len(pool)

    steps.append(CalcStep(title="Исходная выборка",
                          detail=f"{plural(len(pool), 'сопоставимая цена', 'сопоставимые цены', 'сопоставимых цен')}: " + ", ".join(_fmt(v) for v in sorted(pool.values()))))
    if len(pool) < need:
        return Calculation(status="insufficient", n=len(pool), prices=list(pool.values()), offer_ids=list(pool),
                           steps=steps, message=f"Найдено {plural(len(pool), 'сопоставимая цена', 'сопоставимые цены', 'сопоставимых цен')}, нужно не менее {need}. "
                           "Цена не обоснована: запросите коммерческие предложения или расширьте поиск."), excluded

    if m.get("outlier", "hint") != "iterative":
        return _hint_only(pool, qty, need, limit, nd, steps), excluded

    med = statistics.median(pool.values())
    mad = statistics.median(abs(v - med) for v in pool.values())
    if mad > 0 and len(pool) >= int(m.get("outlier_mad_min_n", 6)):
        far = {i: v for i, v in pool.items() if abs(v - med) > k * 1.4826 * mad}
        if far and len(pool) - len(far) >= need and len(far) <= max_share * start_n:
            for i, v in far.items():
                excluded[i] = f"аномальная цена: {_fmt(v)} отклоняется от медианы {_fmt(med)} более чем на {k:g}·MAD"
                pool.pop(i)
            steps.append(CalcStep(title="Отсев аномалий (медиана ± k·MAD)",
                                  detail=f"медиана {_fmt(med)}, MAD {_fmt(mad)}; исключено {len(far)}: "
                                  + ", ".join(_fmt(v) for v in far.values())))

    mean, std, cv = stats(list(pool.values()))
    steps.append(CalcStep(title="Однородность", detail=f"среднее {_fmt(mean)}, σ {_fmt(std)}, V = σ/среднее = {_pct(cv, 2)} %"))
    while cv > limit and len(pool) > need and (start_n - len(pool) + 1) <= max_share * start_n:
        med = statistics.median(pool.values())
        i, v = max(pool.items(), key=lambda kv: abs(kv[1] - med))
        pool.pop(i)
        excluded[i] = f"исключена для однородности: {_fmt(v)} дальше всех от медианы {_fmt(med)} при V {_pct(cv, 1)} % > {limit:g} %"
        mean, std, cv = stats(list(pool.values()))
        steps.append(CalcStep(title="Исключение крайнего значения",
                              detail=f"убрана цена {_fmt(v)}; новое V = {_pct(cv, 2)} %"))

    return _finish(pool, qty, limit, nd, steps), excluded


def _hint_only(pool: dict[str, float], qty: float, need: int, limit: float, nd: int,
               steps: list[CalcStep]) -> Calculation:
    """Приказ № 567 не разрешает выбрасывать цены автоматически: при V > 33 % нужны дополнительные исследования.
    Поэтому цена, сильнее всех отклоняющаяся от медианы, только подсказывается закупщику, а решение за ним."""
    mean, std, cv = stats(list(pool.values()))
    steps.append(CalcStep(title="Однородность", detail=f"среднее {_fmt(mean)}, σ {_fmt(std)}, V = σ/среднее = {_pct(cv, 2)} %"))
    res = _finish(pool, qty, limit, nd, steps)
    if cv > limit:
        med = statistics.median(pool.values())
        i, v = max(pool.items(), key=lambda kv: abs(kv[1] - med))
        rest = [x for k, x in pool.items() if k != i]
        tip = f"сильнее всех от медианы {_fmt(med)} отклоняется цена {_fmt(v)}; "
        if len(rest) >= need:
            tip += (f"без неё V = {_pct(stats(rest)[2], 2)} %. Проверьте её источник и при обоснованных сомнениях "
                    "исключите вручную с комментарием")
        else:
            tip += (f"без неё останется {plural(len(rest), 'цена', 'цены', 'цен')}, поэтому нужны ещё цены: "
                    "запросите коммерческие предложения")
        res.steps.append(CalcStep(title="Подсказка закупщику", detail=tip))
        res.message += " Подсказка: " + tip + "."
    return res


def _finish(pool: dict[str, float], qty: float, limit: float, nd: int, steps: list[CalcStep]) -> Calculation:
    mean, std, cv = stats(list(pool.values()))
    vals = list(pool.values())
    base = Calculation(status="ok", n=len(vals), prices=vals, offer_ids=list(pool), mean=round(mean, nd),
                       std=round(std, nd), cv=round(cv, 2), minimum=min(vals), median=statistics.median(vals), steps=steps)
    if cv > limit:
        base.status = "heterogeneous"
        base.message = (f"Совокупность цен неоднородна: V = {_pct(cv, 2)} % > {limit:g} %. "
                        "По методике нужно провести дополнительные исследования — цена не обоснована.")
        return base
    unit = math.floor(mean * 10**nd + 0.5) / 10**nd
    base.unit_price = unit
    base.total = round(unit * qty, nd)
    base.steps.append(CalcStep(title="НМЦД",
                          detail=f"цена за единицу = (1/{len(vals)})·Σцᵢ = {_fmt(unit)}; × {qty:g} = {_fmt(base.total)}"))
    base.message = f"Обоснована по {plural(len(vals), 'цене', 'ценам', 'ценам')}, V = {_pct(cv, 2)} % ≤ {limit:g} %."
    return base
