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
    # Copy committed Iași dataset + bundled ML slots (default=0ep, apprentice=50ep, trained=500ep)
    out_data = DOCS / "data"
    out_slots = out_data / "slots"
    if out_slots.exists():
        shutil.rmtree(out_slots)
    out_slots.mkdir(parents=True, exist_ok=True)
    for rel in ("iasi_core.json", "iasi_routes.json", "iasi_buildings.json", "iasi_realism.json"):
        shutil.copy2(DATA / rel, out_data / rel)
    for slot_name in ("default", "apprentice", "trained"):
        slot_file = DATA / "slots" / f"{slot_name}.json"
        if slot_file.exists():
            shutil.copy2(slot_file, out_slots / f"{slot_name}.json")
    print(f"Built GitHub Pages static bundle in {DOCS}")


if __name__ == "__main__":
    main()
