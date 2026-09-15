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
    """Kembalikan list dict {name, type, nullable, key, default, extra} untuk satu tabel."""
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT COLUMN_NAME AS name, COLUMN_TYPE AS type, IS_NULLABLE AS nullable,
               COLUMN_KEY AS `key`, COLUMN_DEFAULT AS `default`, EXTRA AS extra
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """,
        (table,),
    )
    cols = cur.fetchall()
    cur.close()
    return cols


def column_requires_value(c):
    """True kalau kolom ini NOT NULL, tidak punya default, dan bukan auto_increment --
    artinya WAJIB diisi eksplisit saat insert, atau migrasi akan gagal kalau
    kolom ini tidak dipetakan/diisi."""
    extra = (c.get("extra") or "").lower()
    return c["nullable"] == "NO" and c.get("default") is None and "auto_increment" not in extra


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
    listing.add_column("Default", justify="center")

    if allow_none:
        listing.add_row("0", "(skip / tidak dipetakan)", "", "", "", "")
    for idx, c in enumerate(columns, 1):
        if column_requires_value(c):
            # NOT NULL + tidak ada default + bukan auto_increment -> wajib dipetakan,
            # kalau tidak insert bakal gagal. Ditandai merah biar kelihatan jelas.
            listing.add_row(
                f"[bold red]{idx}[/bold red]",
                f"[bold red]{c['name']}[/bold red]",
                f"[bold red]{c['type']}[/bold red]",
                f"[bold red]{c['nullable']}[/bold red]",
                f"[bold red]{c['key']}[/bold red]",
                "[bold red]⚠ wajib diisi[/bold red]",
            )
        else:
            default_display = c["default"] if c["default"] is not None else "[dim]-[/dim]"
            listing.add_row(str(idx), c["name"], c["type"], c["nullable"], c["key"], str(default_display))

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
# Filter / kondisi (WHERE) per blok
# ---------------------------------------------------------------------------

FILTER_OPS_NO_VALUE = ["IS NULL", "IS NOT NULL"]
FILTER_OPS_LIST_VALUE = ["IN", "NOT IN"]
FILTER_OPERATORS = ["=", "!=", ">", ">=", "<", "<="] + ["LIKE", "NOT LIKE"] + FILTER_OPS_LIST_VALUE + FILTER_OPS_NO_VALUE


def build_filter(old_cols):
    """Wizard interaktif buat susun kondisi WHERE yang melekat pada satu blok --
    hanya baris sumber yang cocok kondisi ini yang dimigrasi."""
    if not Confirm.ask(
        "\nApakah blok ini perlu filter/kondisi untuk menyaring baris sumber (WHERE)? "
        "Contoh: hanya migrasi baris dengan status='aktif'.",
        default=False,
    ):
        return None

    conditions = []
    while True:
        col = choose_column(old_cols, "Kolom untuk kondisi filter")
        op = choose_from_list(FILTER_OPERATORS, f"Operator untuk `{col}`")

        if op in FILTER_OPS_NO_VALUE:
            value = None
        elif op in FILTER_OPS_LIST_VALUE:
            raw = Prompt.ask(f"Daftar nilai untuk `{col}` {op} (pisahkan dengan koma)")
            value = [v.strip() for v in raw.split(",") if v.strip()]
            if not value:
                console.print("[red]Minimal harus ada 1 nilai, kondisi ini dilewati.[/red]")
                continue
        else:
            value = Prompt.ask(f"Nilai untuk `{col}` {op}")

        conditions.append({"column": col, "operator": op, "value": value})
        value_display = "" if value is None else (", ".join(value) if isinstance(value, list) else value)
        console.print(f"[dim]  -> kondisi ditambahkan: `{col}` {op} {value_display}[/dim]")

        if not Confirm.ask("Tambah kondisi filter lain?", default=False):
            break

    logic = "AND"
    if len(conditions) > 1:
        logic = Prompt.ask(
            "Gabungkan semua kondisi filter di atas dengan", choices=["AND", "OR"], default="AND"
        )

    return {"logic": logic, "conditions": conditions}


# ---------------------------------------------------------------------------
# Wizard pembuatan 1 blok mapping
# ---------------------------------------------------------------------------

def build_block(conn_old, conn_new, old_tables, new_tables):
    console.print(Panel.fit("[bold]Blok Mapping Baru[/bold]", border_style="cyan"))

    source_table = choose_from_list(old_tables, "Pilih Tabel Sumber (OLD_DB)")
    target_table = choose_from_list(new_tables, "Pilih Tabel Tujuan (NEW_DB)")

    old_cols = get_columns(conn_old, source_table)
    new_cols = get_columns(conn_new, target_table)

    has_key = Confirm.ask(
        "\nApakah tabel sumber ini punya kolom kunci/ID yang mau dipertahankan atau dijadikan referensi? "
        "(jawab tidak kalau tabelnya tidak punya kolom kunci sama sekali, misal tabel lookup/pivot)",
        default=True,
    )

    id_source = id_target = dedup_key = None
    upsert = False

    if not has_key:
        id_mode = "none"
        console.print(
            "[dim]Tidak ada kolom kunci -> baris akan di-insert langsung ke tujuan tanpa mempertahankan id "
            "apa pun. Idempotensi (aman dijalankan ulang) tidak berlaku untuk blok ini.[/dim]"
        )
    else:
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
            upsert = Confirm.ask(
                "Pakai ON DUPLICATE KEY UPDATE supaya aman dijalankan ulang (upsert)?", default=True
            )
        else:
            console.print("[dim]Kolom ini akan menyimpan id lama sebagai referensi (bukan primary key baru).[/dim]")
            id_target = choose_column(new_cols, f"Kolom penyimpan id lama di `{target_table}`")
            dedup_key = id_target
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
        if id_mode == "preserve":
            console.print(
                "[yellow]  ⚠ Mode ID 'preserve' tidak menyimpan kolom FK terpisah (PK baru = id lama). "
                "FK di atas hanya efektif untuk mode 'preserve_secondary' atau tabel tanpa kolom kunci.[/yellow]"
            )

    used_old = {id_source} | ({fk["source_column"]} if fk else set())
    used_new = {id_target} | ({fk["target_column"]} if fk else set())

    unpivot = None
    if Confirm.ask(
        "\nApakah blok ini perlu transformasi column-to-row (unpivot)? Contoh: banyak kolom sumber "
        "(jan, feb, mar, ...) mau dipecah jadi banyak baris di tujuan (mis. kolom periode + nilai).",
        default=False,
    ):
        console.print("[dim]Pilih kolom tujuan yang menampung LABEL (nama periode/kategori) dan VALUE (nilainya).[/dim]")
        label_col = choose_column(
            [c for c in new_cols if c["name"] not in used_new],
            f"Kolom tujuan untuk LABEL di `{target_table}`",
        )
        used_new.add(label_col)
        value_col = choose_column(
            [c for c in new_cols if c["name"] not in used_new],
            f"Kolom tujuan untuk VALUE di `{target_table}`",
        )
        used_new.add(value_col)

        skip_null = Confirm.ask("Lewati kolom sumber yang nilainya NULL (tidak insert baris kosong)?", default=True)

        items = []
        console.print("\n[bold yellow]Tambahkan kolom sumber satu per satu untuk di-unpivot.[/bold yellow] Pilih 0 kalau sudah selesai.\n")
        while True:
            remaining = [c for c in old_cols if c["name"] not in used_old]
            if not remaining:
                break
            col = choose_column(remaining, "Kolom sumber berikutnya untuk di-unpivot", allow_none=True)
            if not col:
                break
            label = Prompt.ask(f"Label untuk `{col}` (nilai yang diisi ke kolom LABEL di atas)", default=col)
            items.append({"source_column": col, "label": label})
            used_old.add(col)

        if not items:
            console.print("[yellow]Tidak ada kolom yang ditambahkan, unpivot dibatalkan.[/yellow]")
        else:
            unpivot = {
                "target_label_column": label_col,
                "target_value_column": value_col,
                "items": items,
                "skip_null": skip_null,
            }

    filter_def = build_filter(old_cols)

    console.print(
        "\n[bold yellow]Pemetaan kolom satu per satu.[/bold yellow] Pilih 0 untuk skip (tidak dipetakan). "
        "Satu kolom sumber juga bisa dipetakan ke lebih dari satu kolom tujuan sekaligus "
        "(nilainya diduplikasi ke semua kolom tujuan itu).\n"
    )

    columns = {}
    for c in old_cols:
        if c["name"] in used_old:
            continue

        mappable_new_cols = [nc for nc in new_cols if nc["name"] not in used_new]
        if not mappable_new_cols:
            console.print(f"[dim]  -> `{c['name']}` dilewati (tidak ada kolom tujuan tersisa)[/dim]")
            continue

        console.print(f"[bold cyan]Kolom sumber:[/bold cyan] {c['name']} ({c['type']})")
        target_col = choose_column(mappable_new_cols, f"Petakan `{c['name']}` ke kolom tujuan mana?", allow_none=True)
        if not target_col:
            console.print(f"[dim]  -> `{c['name']}` dilewati (tidak dipetakan)[/dim]")
            continue

        targets = [target_col]
        used_new.add(target_col)

        while True:
            mappable_new_cols = [nc for nc in new_cols if nc["name"] not in used_new]
            if not mappable_new_cols:
                break
            if not Confirm.ask(
                f"Petakan `{c['name']}` juga ke kolom tujuan lain (nilai yang sama diduplikasi)?", default=False
            ):
                break
            extra = choose_column(mappable_new_cols, f"Kolom tujuan tambahan untuk `{c['name']}`", allow_none=True)
            if not extra:
                break
            targets.append(extra)
            used_new.add(extra)

        columns[c["name"]] = targets[0] if len(targets) == 1 else targets

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
        "unpivot": unpivot,
        "filter": filter_def,
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
