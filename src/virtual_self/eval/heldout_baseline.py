from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from virtual_self.eval.heldout_snapshot import heldout_dir
from virtual_self.eval.stylometry import mean_features, stylometry_features


def load_heldout_by_register(root: Path | None = None) -> dict[str, list[str]]:
    base = root or heldout_dir()
    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    by_reg: dict[str, list[str]] = defaultdict(list)
    for entry in manifest.get("entries") or []:
        reg = entry.get("register") or "journal"
        fname = entry.get("filename")
        if not fname:
            continue
        text = (base / fname).read_text(encoding="utf-8")
        by_reg[reg].append(text)
    return dict(by_reg)


def heldout_stylometry_baselines(root: Path | None = None) -> dict[str, Any]:
    by_reg = load_heldout_by_register(root)
    out: dict[str, Any] = {}
    for reg, texts in sorted(by_reg.items()):
        feats = [stylometry_features(t) for t in texts]
        out[reg] = {
            "sample_count": len(texts),
            "mean_features": mean_features(feats),
        }
    return out
