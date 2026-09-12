from db import connect_old, connect_new
from handler.base import BaseMigrator

BATCH_SIZE = 2000


class DynamicMigrator(BaseMigrator):
    """Migrator generik yang dikendalikan mapping JSON (lihat mappings/*.json,
    dibuat/diedit lewat mapping_builder.py). Menambah tabel baru = tambah file
    JSON, bukan bikin class handler baru.
    """

    def __init__(self, mapping: dict):
        self.mapping = mapping

    def execute(self, console):
        name = self.mapping["name"]
        blocks = self.mapping["blocks"]
        source_tables = sorted({b["source_table"] for b in blocks})

        console.print(f"[cyan]➜ [{name.upper()}] Menjalankan mapping dinamis ({len(blocks)} blok)...[/cyan]")
        for t in source_tables:
            self.update_log(t, "IN_PROGRESS")

        conn_old = connect_old()
        conn_new = connect_new()
        valid_id_cache = {}  # (source_table, id_col) -> set id, buat validasi FK antar blok

        try:
            for block in blocks:
                self._run_block(block, conn_old, conn_new, console, valid_id_cache)

            for t in source_tables:
                self.update_log(t, "SUCCESS")
            console.print(f"[bold green]✔ [{name.upper()}] Migrasi selesai![/bold green]")
            return True

        except Exception as err:
            conn_new.rollback()
            for t in source_tables:
                self.update_log(t, "FAILED", str(err))
            console.print(f"[bold red]✘ [{name.upper()}] Gagal migrasi:[/bold red] {err}")
            return False

        finally:
            conn_old.close()
            conn_new.close()

    def _get_valid_ids(self, conn_old, table, id_col, cache):
        key = (table, id_col)
        if key not in cache:
            cur = conn_old.cursor()
            cur.execute(f"SELECT `{id_col}` FROM `{table}`")
            cache[key] = {row[0] for row in cur.fetchall()}
            cur.close()
        return cache[key]

    def _coalesce_now_columns(self, conn_new, table, columns):
        """Kolom target timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP dianggap
        auto-filled: nilai lama dipakai kalau ada, NULL jatuh ke CURRENT_TIMESTAMP,
        supaya insert eksplisit tidak menabrak constraint NOT NULL itu."""
        if not columns:
            return set()

        cur = conn_new.cursor()
        placeholders = ", ".join(["%s"] * len(columns))
        cur.execute(
            f"""
            SELECT COLUMN_NAME FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
              AND COLUMN_NAME IN ({placeholders})
              AND IS_NULLABLE = 'NO'
              AND COLUMN_DEFAULT LIKE '%%CURRENT_TIMESTAMP%%'
            """,
            [table] + list(columns),
        )
        result = {row[0] for row in cur.fetchall()}
        cur.close()
        return result

    def _run_block(self, block, conn_old, conn_new, console, valid_id_cache):
        source_table = block["source_table"]
        target_table = block["target_table"]
        id_source = block["id_source"]
        id_target = block["id_target"]
        id_mode = block["id_mode"]  # "preserve" | "preserve_secondary"
        upsert = block.get("upsert", False)
        dedup_key = block.get("dedup_key")
        fk = block.get("fk")
        columns = block["columns"]  # {kolom_lama: kolom_baru}

        new_cols = list(columns.values())
        coalesce_cols = self._coalesce_now_columns(conn_new, target_table, new_cols)

        def placeholder_for(new_col):
            return "COALESCE(%s, CURRENT_TIMESTAMP)" if new_col in coalesce_cols else "%s"

        old_select_cols = [id_source] + list(columns.keys())
        if fk:
            old_select_cols.append(fk["source_column"])

        already_migrated = set()
        if dedup_key:
            cur_dedup = conn_new.cursor()
            cur_dedup.execute(
                f"SELECT `{dedup_key}` FROM `{target_table}` WHERE `{dedup_key}` IS NOT NULL AND `{dedup_key}` != 0"
            )
            already_migrated = {row[0] for row in cur_dedup.fetchall()}
            cur_dedup.close()

        valid_parent_ids = None
        if fk:
            valid_parent_ids = self._get_valid_ids(
                conn_old, fk["ref_source_table"], fk["ref_source_column"], valid_id_cache
            )

        if id_mode == "preserve":
            insert_cols = [id_target] + new_cols
            values_sql = "%s, " + ", ".join(placeholder_for(c) for c in new_cols)
            if upsert:
                update_sql = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in new_cols)
                insert_sql = (
                    f"INSERT INTO `{target_table}` ({', '.join(f'`{c}`' for c in insert_cols)}) "
                    f"VALUES ({values_sql}) ON DUPLICATE KEY UPDATE {update_sql}"
                )
            else:
                insert_sql = (
                    f"INSERT INTO `{target_table}` ({', '.join(f'`{c}`' for c in insert_cols)}) VALUES ({values_sql})"
                )
        elif id_mode == "preserve_secondary":
            fk_target_col = fk["target_column"] if fk else None
            insert_cols = ([fk_target_col] if fk else []) + [id_target] + new_cols
            leading_placeholders = (["%s"] if fk else []) + ["%s"]
            values_sql = ", ".join(leading_placeholders) + ", " + ", ".join(placeholder_for(c) for c in new_cols)
            insert_sql = (
                f"INSERT INTO `{target_table}` ({', '.join(f'`{c}`' for c in insert_cols)}) VALUES ({values_sql})"
            )
        else:
            raise ValueError(f"id_mode tidak dikenal: {id_mode}")

        select_sql = f"SELECT {', '.join(f'`{c}`' for c in old_select_cols)} FROM `{source_table}`"

        cur_old = conn_old.cursor(dictionary=True)
        cur_new = conn_new.cursor()
        cur_old.execute(select_sql)

        total = 0
        skipped_duplicate = 0
        orphaned_parent = 0

        while True:
            rows = cur_old.fetchmany(BATCH_SIZE)
            if not rows:
                break

            batch = []
            for r in rows:
                if dedup_key and r[id_source] in already_migrated:
                    skipped_duplicate += 1
                    continue

                row_values = []
                if id_mode == "preserve_secondary" and fk:
                    fk_val = r[fk["source_column"]]
                    resolved = fk_val if fk_val in valid_parent_ids else None
                    if resolved is None:
                        orphaned_parent += 1
                    row_values.append(resolved)

                row_values.append(r[id_source])
                row_values += [r[c] for c in columns]
                batch.append(row_values)

            if batch:
                cur_new.executemany(insert_sql, batch)
                conn_new.commit()

            total += len(rows)
            console.print(f"[dim]  ...[{source_table} -> {target_table}] {total} baris diproses[/dim]")

        cur_old.close()
        cur_new.close()

        migrated = total - skipped_duplicate
        console.print(f"[green]  ✔ [{source_table} -> {target_table}] {migrated} baris berhasil dipindahkan[/green]")
        if skipped_duplicate:
            console.print(f"[yellow]  ⚠ {skipped_duplicate} baris dilewati (sudah pernah dimigrasi sebelumnya)[/yellow]")
        if orphaned_parent:
            console.print(
                f"[yellow]  ⚠ {orphaned_parent} baris tidak punya induk valid "
                f"(`{fk['source_column']}` tidak ditemukan di `{fk['ref_source_table']}`.`{fk['ref_source_column']}`) "
                f"-> `{fk['target_column']}` diisi NULL[/yellow]"
            )
