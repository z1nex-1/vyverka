import asyncio
import json

from conftest import EXAMPLES
from vyverka.models import Run
from vyverka.net import SnapshotStore
from vyverka.pipeline import Pipeline
from vyverka.replay import replay
from vyverka.tz.parser import parse_docx


def _printer() -> Run:
    tz = EXAMPLES / "tz_3d_printer.docx"
    return asyncio.run(Pipeline(mode="offline").execute(parse_docx(tz.read_bytes(), tz.name)))


def test_saved_run_replays_to_same_price():
    run = _printer()
    again = Run.model_validate_json(run.model_dump_json())
    rep = replay(again)
    assert rep.ok and rep.rules_pinned
    assert rep.replayed_total == run.total == 1183333.33
    assert rep.snapshots_ok >= 3 and not rep.snapshots_bad


def test_edited_price_in_journal_is_caught():
    run = _printer()
    res = run.results[0]
    picked = next(o for o in res.offers if o.id in res.calc.offer_ids)
    picked.price *= 0.9
    rep = replay(run)
    assert not rep.ok
    assert rep.items[0].price_diffs and rep.replayed_total != run.total


def test_edited_snapshot_hash_is_caught():
    run = _printer()
    res = run.results[0]
    picked = next(o for o in res.offers if o.id in res.calc.offer_ids)
    picked.evidence.sha256 = "0" * 64
    rep = replay(run)
    assert not rep.ok and picked.evidence.url in rep.snapshots_bad


def test_rules_from_run_time_are_used():
    run = _printer()
    run.settings["rules"] = json.loads(json.dumps(run.settings["rules"]))
    run.settings["rules"]["method"]["cv_limit"] = 10
    rep = replay(run)
    assert rep.items[0].replayed["status"] == "heterogeneous" and not rep.ok


def test_refetched_page_keeps_old_version(tmp_path):
    store = SnapshotStore(root=tmp_path / "s", seed=tmp_path / "seed")
    first = store.put("https://example.test/p", 200, "цена 100")
    second = store.put("https://example.test/p", 200, "цена 120")
    key = first.snapshot
    assert store.version_path(key, first.sha256) != store.version_path(key, second.sha256)
    assert store.version_path(key, first.sha256) is not None
    assert [v["sha256"] for v in store.versions(key)] == [first.sha256, second.sha256]


def test_snapshot_marks_price_and_values_only_in_text():
    from vyverka.web.highlight import render

    page = (b'<html><body><div data-price="1450000">1&nbsp;450&nbsp;000 &#8381;</div>'
            b'<script>var p = "1 450 000"</script><p>Max. build volume 600 x 600 x 600 mm</p></body></html>')
    out = render(page, {"url": "https://example.test", "fetched_at": "2026-11-13T10:00", "sha256": "ab" * 32},
                 1450000.0, ["600 x 600 x 600 mm"])
    assert out.count('class="vv-hl"') == 2
    assert 'data-price="1450000"' in out and 'var p = "1 450 000"' in out
    assert 'id="vv-1">1&nbsp;450&nbsp;000</mark>' in out


def test_eis_unit_price_with_long_fraction_is_found():
    import re
    from vyverka.web.highlight import price_pattern

    assert re.search(price_pattern(343.33333333333), "Цена за единицу: 343,3333333 ₽")
    assert not re.search(price_pattern(343.0), "Цена 1343,00")
