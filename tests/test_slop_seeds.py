from __future__ import annotations

from pathlib import Path

from virtual_self.eval.slop_seeds import all_anti_pattern_seeds, persona_eval_slop_flag_seeds


def test_persona_eval_slop_flag_seeds_from_fixture():
    report = Path(__file__).resolve().parents[1] / ".state" / "persona_eval" / "20261015T113715Z-33f94701.json"
    if not report.is_file():
        return
    seeds = persona_eval_slop_flag_seeds(report)
    assert seeds
    assert any(origin == "eval_finding" for _p, _k, _s, origin in seeds)


def test_all_anti_pattern_seeds_dedupes():
    seeds = all_anti_pattern_seeds()
    keys = {(p, k) for p, k, _s, _o in seeds}
    assert len(keys) == len(seeds)
    assert any(k == "regex" for _p, k, _s, _o in seeds)
