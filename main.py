import sys
import mysql.connector
from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt, Confirm
from rich.panel import Panel
from rich.box import ROUNDED

import migration_service
from handler.migrator import run_migration_process, MAPPING_REGISTRY

console = Console()


def get_old_tables():
    """Mengambil daftar seluruh tabel yang ada di OLD_DB."""
    try:
        return migration_service.get_old_tables()
    except mysql.connector.Error as err:
        console.print(f"[bold red]Gagal terhubung ke OLD_DB:[/bold red] {err}")
        sys.exit(1)


def register_migration_queue(selected_tables):
    """Mencatat antrean tabel yang dipilih ke MIGRATION_DB dengan status 'PENDING'."""
    try:
        migration_service.register_migration_queue(selected_tables)
        console.print("[dim green]✔ Antrean migrasi berhasil dicatat ke MIGRATION_DB.[/dim green]")
    except mysql.connector.Error as err:
        console.print(f"[yellow]Peringatan: Gagal mencatat antrean ke MIGRATION_DB:[/yellow] {err}")


def display_tables_menu(available_tables):
    """Menampilkan tabel dalam bentuk UI Rich Table yang rapi."""
    table_ui = Table(
        title="Daftar Tabel Tersedia di Database Lama (OLD_DB)",
        box=ROUNDED,
        header_style="bold magenta",
        title_style="bold cyan"
    )

    table_ui.add_column("No", justify="center", style="dim", width=6)
    table_ui.add_column("Nama Tabel", style="bold white")
    table_ui.add_column("Catatan Modul", style="yellow")

    for idx, table in enumerate(available_tables, 1):
        mapping = MAPPING_REGISTRY.get(table)
        note = f"Custom Mapping ({mapping['name']}.json)" if mapping else "-"

        table_ui.add_row(str(idx), table, note)

    console.print(table_ui)


def parse_user_selection(input_str, max_index, tables):
    """
    Memproses input dari user (misal: 'all', '1,2,5', atau '1-4').
    """
    input_str = input_str.strip().lower()

    if input_str == "all":
        return tables

    selected_indices = set()
    parts = input_str.split(",")

    for part in parts:
        part = part.strip()
        if "-" in part:
            try:
                start, end = map(int, part.split("-"))
                for i in range(start, end + 1):
                    if 1 <= i <= max_index:
                        selected_indices.add(i - 1)
            except ValueError:
                continue
        else:
            try:
                val = int(part)
                if 1 <= val <= max_index:
                    selected_indices.add(val - 1)
            except ValueError:
                continue

    return [tables[i] for i in sorted(selected_indices)]


def main():
    console.clear()
    console.print(Panel.fit(
        "[bold green]DATABASE MIGRATION TOOL[/bold green]\n"
        "[dim]Sistem Migrasi Data Modular & Terpantau[/dim]",
        box=ROUNDED,
        border_style="cyan"
    ))

    # 1. Fetch daftar tabel dari database lama
    console.print("[cyan]Mengambil daftar tabel dari OLD_DB...[/cyan]")
    tables = get_old_tables()

    if not tables:
        console.print("[bold red]Tidak ada tabel yang ditemukan di OLD_DB.[/bold red]")
        sys.exit(0)

    # 2. Tampilkan Menu Tabel
    display_tables_menu(tables)

    # 3. Minta Input Pilihan dari User
    console.print("\n[bold yellow]Petunjuk Pilihan:[/bold yellow]")
    console.print(" • Ketik [bold white]'all'[/bold white] untuk memilih semua tabel.")
    console.print(" • Ketik nomor dipisah koma (misal: [bold white]1,3,5[/bold white]).")
    console.print(" • Gunakan rentang nomor (misal: [bold white]1-4[/bold white]).\n")

    user_input = Prompt.ask("[bold cyan]Pilih tabel yang ingin dimigrasi[/bold cyan]")
    selected_tables = parse_user_selection(user_input, len(tables), tables)

    if not selected_tables:
        console.print("\n[bold red]Pilihan tidak valid atau kosong. Migrasi dibatalkan.[/bold red]\n")
        sys.exit(0)

    # 4. Tampilkan Ringkasan Pilihan
    summary_table = Table(box=ROUNDED, header_style="bold blue")
    summary_table.add_column("No", justify="center", width=4)
    summary_table.add_column("Tabel Terpilih", style="green")

    for idx, tbl in enumerate(selected_tables, 1):
        summary_table.add_row(str(idx), tbl)

    console.print("\n", summary_table)

    # 5. Konfirmasi Eksekusi
    confirm = Confirm.ask(
        f"[bold yellow]Apakah Anda yakin ingin memulai migrasi untuk {len(selected_tables)} tabel di atas?[/bold yellow]",
        default=True
    )

    if not confirm:
        console.print("\n[yellow]Proses migrasi dibatalkan oleh pengguna.[/yellow]\n")
        sys.exit(0)

    # 6. Catat antrean & Jalankan Proses Migrasi Modular
    register_migration_queue(selected_tables)
    run_migration_process(selected_tables)

    console.print("\n[bold dim green]✔ Seluruh alur migrasi selesai dieksekusi.[/bold dim green]\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n\n[yellow]Dibatalkan oleh pengguna (Ctrl+C). Proses migrasi dihentikan.[/yellow]\n")
        sys.exit(1)