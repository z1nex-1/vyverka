import statistics

import pytest

from vyverka import calc as calc_mod
from vyverka.calc import calculate, stats


@pytest.fixture
def iterative(monkeypatch):
    base = calc_mod.rules()
    monkeypatch.setattr(calc_mod, "rules", lambda: {**base, "method": {**base["method"], "outlier": "iterative"}})


def test_stats_use_sample_deviation():
    mean, std, cv = stats([100, 110, 120])
    assert mean == 110
    assert std == pytest.approx(10.0)
    assert cv == pytest.approx(9.0909, rel=1e-4)


def test_homogeneous_sample_gives_mean_times_qty():
    calc, excluded = calculate({"a": 300.0, "b": 320.0, "c": 340.0, "d": 337.6}, qty=2500)
    assert calc.status == "ok"
    assert calc.unit_price == pytest.approx(324.40)
    assert calc.total == pytest.approx(811000.0)
    assert calc.cv < 33
    assert not excluded
    assert calc.steps[-1].title == "НМЦД"


def test_fewer_than_three_prices_is_not_justified():
    calc, _ = calculate({"a": 100.0, "b": 101.0}, qty=1)
    assert calc.status == "insufficient"
    assert calc.unit_price is None
    assert "не менее 3" in calc.message


def test_extreme_price_is_only_suggested_by_default():
    calc, excluded = calculate({"a": 100.0, "b": 105.0, "c": 98.0, "d": 102.0, "e": 260.0}, qty=1)
    assert calc.status == "heterogeneous" and not excluded
    assert "260,00" in calc.message and "исключите вручную" in calc.message


def test_extreme_price_removed_until_homogeneous(iterative):
    prices = {"a": 100.0, "b": 105.0, "c": 98.0, "d": 102.0, "e": 260.0}
    calc, excluded = calculate(prices, qty=1)
    assert calc.status == "ok"
    assert "e" in excluded
    assert calc.unit_price == pytest.approx(statistics.mean([100, 105, 98, 102]))


def test_mad_prefilter_drops_anomalies_in_large_sample(iterative):
    prices = {str(i): v for i, v in enumerate([100, 101, 99, 102, 98, 100, 5000])}
    calc, excluded = calculate(prices, qty=1)
    assert "6" in excluded and "MAD" in excluded["6"]
    assert calc.status == "ok"


def test_heterogeneous_when_exclusion_budget_spent():
    calc, _ = calculate({"a": 10.0, "b": 30.0, "c": 60.0}, qty=1)
    assert calc.status == "heterogeneous"
    assert calc.unit_price is None


def test_half_kopeck_rounds_up():
    calc, _ = calculate({"a": 100.005, "b": 100.005, "c": 100.005}, qty=1)
    assert calc.unit_price == pytest.approx(100.01)


def test_min_price_rule_takes_lowest_of_homogeneous_sample(monkeypatch):
    base = calc_mod.rules()
    monkeypatch.setattr(calc_mod, "rules", lambda: {**base, "method": {**base["method"], "price": "min"}})
    c, _ = calculate({"a": 100.0, "b": 110.0, "c": 120.0}, 3)
    assert c.status == "ok" and c.unit_price == 100.0 and c.total == 300.0
    assert "min(ц₁…ц₃)" in c.steps[-1].detail and "минимальная" in c.message


def test_min_price_rule_still_requires_homogeneity(monkeypatch):
    base = calc_mod.rules()
    monkeypatch.setattr(calc_mod, "rules", lambda: {**base, "method": {**base["method"], "price": "min"}})
    c, _ = calculate({"a": 100.0, "b": 300.0, "c": 900.0}, 1)
    assert c.status == "heterogeneous" and c.unit_price is None
