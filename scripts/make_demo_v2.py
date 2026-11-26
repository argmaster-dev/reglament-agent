"""Собрать демонстрационную правку ДБРЦТ: в п. 1.5 «3 рабочих дня» -> «5 рабочих дней».

Правится только word/document.xml внутри копии файла; остальные части архива переносятся как есть.
Запуск: uv run python scripts/make_demo_v2.py
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
DOC_ID = "dbrct-project"
OUTPUT = ROOT / "docx" / "demo" / "ДБРЦТ v2 (демо-правка п. 1.5).docx"
OLD, NEW = "3 рабочих дня", "5 рабочих дней"
POINT = "1.5. "


def source_path(root: Path = ROOT) -> Path:
    cards = tomllib.loads((root / "docx" / "documents.toml").read_text(encoding="utf-8"))["document"]
    return root / "docx" / next(c["file"] for c in cards if c["doc_id"] == DOC_ID)


def edit_xml(xml: str) -> str:
    start = xml.index(f">{POINT}")  # the run that starts with the point number
    end = xml.index("</w:t>", start)
    run = xml[start:end]
    if run.count(OLD) != 1:
        raise ValueError(f"В п. 1.5 ожидалось ровно одно вхождение «{OLD}»")
    return xml[:start] + run.replace(OLD, NEW) + xml[end:]


def make(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(source) as src, ZipFile(target, "w") as dst:
        for item in src.infolist():
            data = src.read(item)
            if item.filename == "word/document.xml":
                data = edit_xml(data.decode("utf-8")).encode("utf-8")
            dst.writestr(item, data, compress_type=item.compress_type)
    return target


if __name__ == "__main__":
    print(make(source_path(), OUTPUT))
