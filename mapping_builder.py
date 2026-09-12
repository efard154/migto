"""
Tool interaktif untuk membuat/mengedit mapping migrasi kolom (OLD_DB -> NEW_DB)
dan menyimpannya sebagai JSON di folder mappings/.

Ini menggantikan cara lama (bikin file handler/<tabel>.py baru tiap ada aturan
migrasi khusus): `handler/dynamic.py` (DynamicMigrator) membaca JSON hasil tool
ini secara generik, jadi tabel baru cukup dipetakan lewat wizard ini.

Jalankan:  python mapping_builder.py
"""
import json
import os
import sys

from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt, Confirm
from rich.panel import Panel
from rich.box import ROUNDED

from db import connect_old, connect_new

console = Console()
MAPPINGS_DIR = os.path.join(os.path.dirname(__file__), "mappings")


# ---------------------------------------------------------------------------
# Introspeksi skema
# ---------------------------------------------------------------------------

def list_tables(conn):
    cur = conn.cursor()
    cur.execute("SHOW TABLES")
    tables = [row[0] for row in cur.fetchall()]
    cur.close()
    return tables


def get_columns(conn, table):
    """Kembalikan list dict {name, type, nullable, key, default} untuk satu tabel."""
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT COLUMN_NAME AS name, COLUMN_TYPE AS type, IS_NULLABLE AS nullable,
               COLUMN_KEY AS `key`, COLUMN_DEFAULT AS `default`
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """,
        (table,),
    )
    cols = cur.fetchall()
    cur.close()
    return cols


def get_primary_key(conn, table):
    cols = get_columns(conn, table)
    for c in cols:
        if c["key"] == "PRI":
            return c["name"]
    return None


# ---------------------------------------------------------------------------
# Helper UI
# ---------------------------------------------------------------------------

def choose_from_list(items, title, allow_none=False, none_label="(tidak ada)"):
    """Tampilkan daftar bernomor, minta user pilih satu index, kembalikan itemnya."""
    listing = Table(box=ROUNDED, header_style="bold magenta", title=title, title_style="bold cyan")
    listing.add_column("No", justify="center", style="dim", width=5)
    listing.add_column("Nama")

    if allow_none:
        listing.add_row("0", f"[dim]{none_label}[/dim]")
    for idx, item in enumerate(items, 1):
        listing.add_row(str(idx), str(item))

    console.print(listing)

    while True:
        raw = Prompt.ask("Pilih nomor")
        try:
            val = int(raw.strip())
        except ValueError:
            console.print("[red]Masukkan angka yang valid.[/red]")
            continue

        if allow_none and val == 0:
            return None
        if 1 <= val <= len(items):
            return items[val - 1]

        console.print("[red]Nomor di luar jangkauan.[/red]")


def choose_column(columns, title, allow_none=False):
    names = [c["name"] for c in columns]
    listing = Table(box=ROUNDED, header_style="bold magenta", title=title, title_style="bold cyan")
    listing.add_column("No", justify="center", style="dim", width=5)
    listing.add_column("Kolom", style="bold white")
    listing.add_column("Tipe", style="yellow")
    listing.add_column("Null?", justify="center")
    listing.add_column("Key", justify="center")

    if allow_none:
        listing.add_row("0", "(skip / tidak dipetakan)", "", "", "")
    for idx, c in enumerate(columns, 1):
        listing.add_row(str(idx), c["name"], c["type"], c["nullable"], c["key"])

    console.print(listing)

    while True:
        raw = Prompt.ask("Pilih nomor kolom")
        try:
            val = int(raw.strip())
        except ValueError:
            console.print("[red]Masukkan angka yang valid.[/red]")
            continue

        if allow_none and val == 0:
            return None
        if 1 <= val <= len(names):
            return names[val - 1]

        console.print("[red]Nomor di luar jangkauan.[/red]")


# ---------------------------------------------------------------------------
# Wizard pembuatan 1 blok mapping
# ---------------------------------------------------------------------------

def build_block(conn_old, conn_new, old_tables, new_tables):
    console.print(Panel.fit("[bold]Blok Mapping Baru[/bold]", border_style="cyan"))

    source_table = choose_from_list(old_tables, "Pilih Tabel Sumber (OLD_DB)")
    target_table = choose_from_list(new_tables, "Pilih Tabel Tujuan (NEW_DB)")

    old_cols = get_columns(conn_old, source_table)
    new_cols = get_columns(conn_new, target_table)

    default_id_source = get_primary_key(conn_old, source_table) or old_cols[0]["name"]
    console.print(f"[dim]Primary key sumber terdeteksi: {default_id_source}[/dim]")
    id_source = choose_column(old_cols, f"Kolom ID di `{source_table}` (sumber)")

    default_id_target = get_primary_key(conn_new, target_table)
    console.print(f"[dim]Primary key tujuan terdeteksi: {default_id_target}[/dim]")

    console.print(
        "\n[bold yellow]Mode ID:[/bold yellow]\n"
        " 1. preserve            -> nilai id lama langsung jadi primary key baru\n"
        " 2. preserve_secondary  -> id baru auto-increment, id lama disimpan di kolom lain\n"
    )
    id_mode_choice = Prompt.ask("Pilih mode", choices=["1", "2"], default="1")
    id_mode = "preserve" if id_mode_choice == "1" else "preserve_secondary"

    if id_mode == "preserve":
        id_target = choose_column(new_cols, f"Kolom ID di `{target_table}` (tujuan, primary key)")
        dedup_key = None
        upsert = Confirm.ask(
            "Pakai ON DUPLICATE KEY UPDATE supaya aman dijalankan ulang (upsert)?", default=True
        )
    else:
        console.print("[dim]Kolom ini akan menyimpan id lama sebagai referensi (bukan primary key baru).[/dim]")
        id_target = choose_column(new_cols, f"Kolom penyimpan id lama di `{target_table}`")
        dedup_key = id_target
        upsert = False
        console.print(
            f"[dim]Idempoten: baris dengan `{dedup_key}` yang sudah ada di `{target_table}` akan dilewati "
            f"kalau mapping ini dijalankan ulang.[/dim]"
        )

    fk = None
    if Confirm.ask(
        "\nApakah blok ini perlu foreign key (tautan ke induk di tabel lain), "
        "misalnya sub-baris yang menunjuk ke baris induknya?",
        default=False,
    ):
        fk_source_col = choose_column(
            [c for c in old_cols if c["name"] != id_source],
            f"Kolom FK di `{source_table}` (sumber, menunjuk id induk)",
        )
        fk_target_col = choose_column(
            [c for c in new_cols if c["name"] != id_target],
            f"Kolom FK di `{target_table}` (tujuan, menyimpan id induk)",
        )
        ref_table = choose_from_list(old_tables, "Tabel induk acuan (OLD_DB, tempat id induk berasal)")
        ref_cols = get_columns(conn_old, ref_table)
        ref_col = choose_column(ref_cols, f"Kolom id induk di `{ref_table}`")

        fk = {
            "source_column": fk_source_col,
            "target_column": fk_target_col,
            "ref_source_table": ref_table,
            "ref_source_column": ref_col,
        }

    used_old = {id_source} | ({fk["source_column"]} if fk else set())
    used_new = {id_target} | ({fk["target_column"]} if fk else set())

    console.print("\n[bold yellow]Pemetaan kolom satu per satu.[/bold yellow] Pilih 0 untuk skip (tidak dipetakan).\n")

    mappable_new_cols = [c for c in new_cols if c["name"] not in used_new]
    columns = {}
    for c in old_cols:
        if c["name"] in used_old:
            continue

        console.print(f"[bold cyan]Kolom sumber:[/bold cyan] {c['name']} ({c['type']})")
        target_col = choose_column(mappable_new_cols, f"Petakan `{c['name']}` ke kolom tujuan mana?", allow_none=True)
        if target_col:
            columns[c["name"]] = target_col
        else:
            console.print(f"[dim]  -> `{c['name']}` dilewati (tidak dipetakan)[/dim]")

    return {
        "source_table": source_table,
        "target_table": target_table,
        "id_source": id_source,
        "id_target": id_target,
        "id_mode": id_mode,
        "upsert": upsert,
        "dedup_key": dedup_key,
        "fk": fk,
        "columns": columns,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_existing(name):
    path = os.path.join(MAPPINGS_DIR, f"{name}.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_mapping(mapping):
    os.makedirs(MAPPINGS_DIR, exist_ok=True)
    path = os.path.join(MAPPINGS_DIR, f"{mapping['name']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2, ensure_ascii=False)
    return path


def main():
    console.clear()
    console.print(Panel.fit(
        "[bold green]MAPPING BUILDER[/bold green]\n"
        "[dim]Bikin/edit aturan migrasi kolom secara interaktif, disimpan sebagai JSON[/dim]",
        box=ROUNDED,
        border_style="cyan",
    ))

    console.print("[cyan]Menghubungkan ke OLD_DB & NEW_DB...[/cyan]")
    conn_old = connect_old()
    conn_new = connect_new()
    old_tables = list_tables(conn_old)
    new_tables = list_tables(conn_new)

    name = Prompt.ask("\nNama mapping (jadi nama file, misal 'locaties')").strip()
    mapping = load_existing(name)

    if mapping:
        console.print(f"[yellow]Mapping '{name}' sudah ada ({len(mapping['blocks'])} blok). Menambah blok baru ke situ.[/yellow]")
    else:
        description = Prompt.ask("Deskripsi singkat mapping ini", default="")
        mapping = {"name": name, "description": description, "blocks": []}

    while True:
        block = build_block(conn_old, conn_new, old_tables, new_tables)
        mapping["blocks"].append(block)

        console.print(f"\n[bold green]✔ Blok `{block['source_table']}` -> `{block['target_table']}` ditambahkan.[/bold green]")
        console.print(json.dumps(block, indent=2, ensure_ascii=False))

        if not Confirm.ask("\nTambah blok lagi untuk mapping ini?", default=False):
            break

    path = save_mapping(mapping)
    conn_old.close()
    conn_new.close()

    console.print(f"\n[bold green]✔ Mapping disimpan ke {path}[/bold green]")
    console.print("[dim]Jalankan main.py seperti biasa; tabel yang terdaftar di mapping ini otomatis pakai DynamicMigrator.[/dim]\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n\n[yellow]Dibatalkan oleh pengguna (Ctrl+C).[/yellow]\n")
        sys.exit(1)
