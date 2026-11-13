"""Собирает в seed/snapshots только те сохранённые страницы, которые нужны примерам из examples/.
С этим набором демо считается в офлайн-режиме без сети и даёт те же числа."""

import asyncio
import shutil
import sys

from vyverka.config import ROOT, data_dir
from vyverka.net import SnapshotStore
from vyverka.pipeline import Pipeline
from vyverka.tz.parser import parse_docx

EXAMPLES = ["tz_paper.docx", "tz_3d_printer.docx", "tz_cytokines.docx", "tz_mouse.docx"]


def main() -> int:
    used: set[str] = set()
    original = SnapshotStore.get

    def spy(self, url):
        page = original(self, url)
        if page:
            used.add(self.key(url))
        return page

    SnapshotStore.get = spy
    for name in EXAMPLES:
        spec = parse_docx((ROOT / "examples" / name).read_bytes(), name)
        run = asyncio.run(Pipeline(mode="offline").execute(spec))
        print(f"{name}: {run.total}", file=sys.stderr)
    src, dst = data_dir() / "snapshots", ROOT / "seed" / "snapshots"
    dst.mkdir(parents=True, exist_ok=True)
    size = 0
    for key in sorted(used):
        for ext in (".json", ".gz"):
            p = src / f"{key}{ext}"
            if p.exists():
                shutil.copy2(p, dst / p.name)
                size += p.stat().st_size
    print(f"{len(used)} снимков, {size / 1e6:.1f} МБ → {dst}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
