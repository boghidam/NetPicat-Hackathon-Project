"""Bundle the static GitHub Pages investor web app into docs/ (zero server needed)."""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DATA = ROOT / "data"
DOCS = ROOT / "docs"


def main() -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    # Copy frontend files
    for item in WEB.iterdir():
        dst = DOCS / item.name
        if item.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(item, dst)
        else:
            shutil.copy2(item, dst)
    # Copy committed Iași dataset + default slot for standalone browser execution
    out_data = DOCS / "data"
    out_slots = out_data / "slots"
    out_slots.mkdir(parents=True, exist_ok=True)
    for rel in ("iasi_core.json", "iasi_routes.json", "iasi_buildings.json", "iasi_realism.json"):
        shutil.copy2(DATA / rel, out_data / rel)
    default_slot = DATA / "slots" / "default.json"
    if default_slot.exists():
        shutil.copy2(default_slot, out_slots / "default.json")
    print(f"Built GitHub Pages static bundle in {DOCS}")


if __name__ == "__main__":
    main()
