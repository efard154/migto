import glob
import json
import os

from rich.console import Console

from handler.dynamic import DynamicMigrator
from handler.generic import GenericMigrator

console = Console()

MAPPINGS_DIR = os.path.join(os.path.dirname(__file__), "..", "mappings")


def _load_mappings():
    """Baca semua file JSON di mappings/ dan bentuk lookup tabel lama -> mapping.

    Menambah tabel dengan aturan migrasi khusus = tambah file JSON lewat
    mapping_builder.py, bukan bikin handler .py baru.
    """
    registry = {}
    for path in sorted(glob.glob(os.path.join(MAPPINGS_DIR, "*.json"))):
        with open(path, "r", encoding="utf-8") as f:
            mapping = json.load(f)

        for block in mapping["blocks"]:
            registry[block["source_table"]] = mapping

    return registry


MAPPING_REGISTRY = _load_mappings()


def reload_registry():
    """Muat ulang MAPPING_REGISTRY dari disk (dipanggil web app setelah mapping
    dibuat/diedit/dihapus lewat UI, supaya tidak perlu restart proses)."""
    global MAPPING_REGISTRY
    MAPPING_REGISTRY = _load_mappings()
    return MAPPING_REGISTRY


def run_migration_process(selected_tables, console=console):
    """Menjalankan migrasi: tabel yang punya mapping JSON dipakai DynamicMigrator,
    sisanya GenericMigrator (copy 1:1). `console` bisa diganti (mis. dari web app)
    selama punya method `.print(...)`."""
    console.print("\n[bold yellow]=== MEMULAI PROSES MIGRASI MODULAR ===[/bold yellow]\n")

    executed_mappings = set()

    for tbl in selected_tables:
        mapping = MAPPING_REGISTRY.get(tbl)

        if mapping:
            if mapping["name"] in executed_mappings:
                continue
            DynamicMigrator(mapping).execute(console)
            executed_mappings.add(mapping["name"])
        else:
            GenericMigrator(tbl).execute(console)
