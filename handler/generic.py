from db import connect_old, connect_new
from handler.base import BaseMigrator


class GenericMigrator(BaseMigrator):
    def __init__(self, table_name: str, force: bool = False, clear_existing: bool = False):
        self.table_name = table_name
        self.force = force
        self.clear_existing = clear_existing
        # Statistik baris -- diisi execute(), dibaca run_migration_process buat
        # ringkasan "berapa baris berhasil / gagal dimigrasi".
        self.rows_migrated = 0
        self.rows_failed = 0

    def execute(self, console):
        console.print(f"[cyan]➜ [{self.table_name.upper()}] Memindahkan data 1:1...[/cyan]")
        self.update_log(self.table_name, "IN_PROGRESS")

        conn_old = connect_old()
        conn_new = connect_new()
        total_rows = 0

        try:
            if self.force:
                self._disable_fk_checks(conn_new)

            if self.clear_existing:
                deleted = self._clear_target_table(conn_new, self.table_name)
                conn_new.commit()
                console.print(
                    f"[yellow]  🗑 [{self.table_name.upper()}] {deleted} baris lama dihapus sebelum migrasi[/yellow]"
                )

            cur_old = conn_old.cursor(dictionary=True)
            cur_new = conn_new.cursor()

            cur_old.execute(f"SELECT * FROM `{self.table_name}`")
            rows = cur_old.fetchall()
            total_rows = len(rows)

            if rows:
                cols = list(rows[0].keys())
                col_names = ", ".join([f"`{c}`" for c in cols])
                placeholders = ", ".join(["%s"] * len(cols))

                query = f"INSERT INTO `{self.table_name}` ({col_names}) VALUES ({placeholders})"
                for r in rows:
                    cur_new.execute(query, list(r.values()))

            conn_new.commit()
            self.rows_migrated = total_rows

            self.update_log(self.table_name, "SUCCESS")
            console.print(f"[bold green]✔ [{self.table_name.upper()}] Migrasi 1:1 berhasil ({total_rows} baris)![/bold green]")
            return True

        except Exception as err:
            conn_new.rollback()
            # Insert 1:1 cuma commit sekali di akhir, jadi kalau gagal semua baris
            # yang terbaca dari sumber dianggap belum ter-migrasi (rollback total).
            self.rows_failed = total_rows
            self.update_log(self.table_name, "FAILED", str(err))
            console.print(f"[bold red]✘ [{self.table_name.upper()}] Gagal migrasi:[/bold red] {err}")
            return False

        finally:
            if self.force:
                self._restore_fk_checks(conn_new)
            conn_old.close()
            conn_new.close()