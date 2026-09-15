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


def run_migration_process(
    selected_tables,
    console=console,
    force=False,
    continue_on_error=True,
    clear_existing=False,
):
    """Menjalankan migrasi: tabel yang punya mapping JSON dipakai DynamicMigrator,
    sisanya GenericMigrator (copy 1:1). `console` bisa diganti (mis. dari web app)
    selama punya method `.print(...)`.

    `force=True` menyalakan mode force migration: SET FOREIGN_KEY_CHECKS = 0 di
    setiap koneksi NEW_DB selama insert berlangsung (dikembalikan ke 1 setelahnya),
    supaya migrasi tidak berhenti gara-gara urutan tabel/FK yatim.

    `continue_on_error=True` (default) membuat migrasi tetap lanjut ke blok/tabel
    berikutnya walau ada yang gagal -- baik antar blok dalam satu mapping, maupun
    antar tabel/mapping dalam satu run. Set False untuk mode "berhenti di kegagalan
    pertama": begitu satu tabel gagal, sisa tabel yang belum diproses akan dilewati
    (muncul di ringkasan sebagai "skipped", bukan "failed").

    `clear_existing=True` menghapus PERMANEN semua baris yang sudah ada di tabel
    tujuan (DELETE FROM) sebelum insert, untuk setiap tabel tujuan yang terlibat --
    dipakai untuk reseed bersih. Defaultnya False karena ini operasi destruktif.

    Mengembalikan dict ringkasan hasil migrasi: total tabel, serta daftar & jumlah
    yang berhasil / gagal / dilewati (mapping yang dipakai bareng beberapa tabel
    dihitung untuk setiap tabel yang memakainya, walau dieksekusi cuma sekali).
    """
    console.print("\n[bold yellow]=== MEMULAI PROSES MIGRASI MODULAR ===[/bold yellow]\n")
    if force:
        console.print("[yellow]⚠ Force migration aktif: FOREIGN_KEY_CHECKS dimatikan sementara saat insert.[/yellow]")
    if clear_existing:
        console.print("[bold red]⚠ Hapus data sebelumnya AKTIF: data lama di tabel tujuan akan dihapus permanen sebelum insert.[/bold red]")
    if not continue_on_error:
        console.print("[yellow]⚠ Mode lanjutkan-jika-gagal nonaktif: migrasi berhenti begitu ada tabel yang gagal.[/yellow]")
    console.print()

    mapping_results = {}  # mapping_name -> {table: True/False}, dieksekusi sekali lalu di-cache
    results = {}  # table_name -> True/False, hanya terisi untuk tabel yang benar-benar dicoba
    stopped_early = False
    rows_migrated = 0
    rows_failed = 0
    rows_skipped = 0

    for tbl in selected_tables:
        if stopped_early:
            break

        mapping = MAPPING_REGISTRY.get(tbl)
        try:
            if mapping:
                name = mapping["name"]
                if name not in mapping_results:
                    dm = DynamicMigrator(
                        mapping,
                        force=force,
                        continue_on_error=continue_on_error,
                        clear_existing=clear_existing,
                    )
                    mapping_results[name] = dm.execute(console)
                    rows_migrated += dm.rows_migrated
                    rows_failed += dm.rows_failed
                    rows_skipped += dm.rows_skipped
                ok = mapping_results[name].get(tbl, False)
            else:
                gm = GenericMigrator(tbl, force=force, clear_existing=clear_existing)
                ok = gm.execute(console)
                rows_migrated += gm.rows_migrated
                rows_failed += gm.rows_failed
        except Exception as err:
            ok = False
            console.print(f"[bold red]✘ [{tbl.upper()}] Error tak terduga:[/bold red] {err}")

        results[tbl] = ok

        if not ok and not continue_on_error:
            stopped_early = True
            console.print(
                f"[bold red]⏹ Migrasi dihentikan: `{tbl}` gagal dan opsi lanjutkan-jika-gagal nonaktif. "
                f"Tabel yang belum diproses akan dilewati.[/bold red]"
            )

    success = [t for t in selected_tables if results.get(t)]
    failed = [t for t in selected_tables if t in results and not results[t]]
    skipped = [t for t in selected_tables if t not in results]

    console.print("\n[bold yellow]=== RINGKASAN MIGRASI ===[/bold yellow]")
    console.print(f"[green]✔ Berhasil: {len(success)}/{len(selected_tables)} tabel[/green]")
    if failed:
        console.print(f"[bold red]✘ Gagal: {len(failed)}/{len(selected_tables)} tabel -> {', '.join(failed)}[/bold red]")
    if skipped:
        console.print(
            f"[yellow]⏭ Dilewati (tidak sempat dicoba): {len(skipped)}/{len(selected_tables)} tabel -> {', '.join(skipped)}[/yellow]"
        )
    if not failed and not skipped:
        console.print("[dim]Tidak ada tabel yang gagal.[/dim]")

    console.print(
        f"[cyan]📊 Baris data: {rows_migrated} berhasil dimigrasi, {rows_failed} gagal"
        + (f", {rows_skipped} dilewati (duplikat)" if rows_skipped else "")
        + "[/cyan]"
    )

    return {
        "total": len(selected_tables),
        "success": success,
        "failed": failed,
        "skipped": skipped,
        "success_count": len(success),
        "failed_count": len(failed),
        "skipped_count": len(skipped),
        "rows_migrated": rows_migrated,
        "rows_failed": rows_failed,
        "rows_skipped": rows_skipped,
    }
