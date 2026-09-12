from db import connect_old, connect_new
from handler.base import BaseMigrator


class GenericMigrator(BaseMigrator):
    def __init__(self, table_name: str):
        self.table_name = table_name

    def execute(self, console):
        console.print(f"[cyan]➜ [{self.table_name.upper()}] Memindahkan data 1:1...[/cyan]")
        self.update_log(self.table_name, "IN_PROGRESS")

        conn_old = connect_old()
        conn_new = connect_new()

        try:
            cur_old = conn_old.cursor(dictionary=True)
            cur_new = conn_new.cursor()

            cur_old.execute(f"SELECT * FROM `{self.table_name}`")
            rows = cur_old.fetchall()

            if rows:
                cols = list(rows[0].keys())
                col_names = ", ".join([f"`{c}`" for c in cols])
                placeholders = ", ".join(["%s"] * len(cols))

                query = f"INSERT INTO `{self.table_name}` ({col_names}) VALUES ({placeholders})"
                for r in rows:
                    cur_new.execute(query, list(r.values()))

            conn_new.commit()
            conn_old.close()
            conn_new.close()

            self.update_log(self.table_name, "SUCCESS")
            console.print(f"[bold green]✔ [{self.table_name.upper()}] Migrasi 1:1 berhasil![/bold green]")
            return True

        except Exception as err:
            conn_new.rollback()
            self.update_log(self.table_name, "FAILED", str(err))
            console.print(f"[bold red]✘ [{self.table_name.upper()}] Gagal migrasi:[/bold red] {err}")
            return False