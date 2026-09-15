from db import connect_old, connect_new
from handler.base import BaseMigrator

BATCH_SIZE = 2000


class DynamicMigrator(BaseMigrator):
    """Migrator generik yang dikendalikan mapping JSON (lihat mappings/*.json,
    dibuat/diedit lewat mapping_builder.py). Menambah tabel baru = tambah file
    JSON, bukan bikin class handler baru.
    """

    def __init__(
        self,
        mapping: dict,
        force: bool = False,
        continue_on_error: bool = True,
        clear_existing: bool = False,
    ):
        self.mapping = mapping
        self.force = force
        self.continue_on_error = continue_on_error
        self.clear_existing = clear_existing
        # Statistik baris (bukan tabel) -- diisi execute(), dibaca run_migration_process
        # buat ringkasan "berapa baris berhasil / gagal dimigrasi".
        self.rows_migrated = 0
        self.rows_failed = 0
        self.rows_skipped = 0

    def execute(self, console):
        """Jalankan semua blok mapping ini. Mengembalikan dict {source_table: bool}
        berisi status akhir tiap tabel sumber yang terlibat -- tidak pernah raise,
        supaya kegagalan satu mapping tidak menghentikan run_migration_process."""
        name = self.mapping["name"]
        blocks = self.mapping["blocks"]
        source_tables = sorted({b["source_table"] for b in blocks})
        target_tables = sorted({b["target_table"] for b in blocks})

        console.print(f"[cyan]➜ [{name.upper()}] Menjalankan mapping dinamis ({len(blocks)} blok)...[/cyan]")
        for t in source_tables:
            self.update_log(t, "IN_PROGRESS")

        conn_old = connect_old()
        conn_new = connect_new()
        valid_id_cache = {}  # (source_table, id_col) -> set id, buat validasi FK antar blok
        processed_tables = set()
        failed_tables = set()

        try:
            if self.force:
                self._disable_fk_checks(conn_new)

            if self.clear_existing:
                for tt in target_tables:
                    deleted = self._clear_target_table(conn_new, tt)
                    conn_new.commit()
                    console.print(f"[yellow]  🗑 [{tt}] {deleted} baris lama dihapus sebelum migrasi[/yellow]")

            for block in blocks:
                src = block["source_table"]
                stats = self._run_block(block, conn_old, conn_new, console, valid_id_cache)
                self.rows_migrated += stats["inserted"]
                self.rows_skipped += stats["skipped_duplicate"]

                if stats["error"]:
                    conn_new.rollback()
                    failed_tables.add(src)
                    processed_tables.add(src)
                    # Baris yang sempat terbaca dari sumber tapi belum sempat ter-commit
                    # (batch yang lagi berjalan saat error) dihitung gagal.
                    self.rows_failed += max(stats["total"] - stats["inserted"] - stats["skipped_duplicate"], 0)
                    console.print(
                        f"[bold red]  ✘ [{src} -> {block['target_table']}] Blok gagal:[/bold red] {stats['error']}"
                    )
                    if not self.continue_on_error:
                        console.print(
                            f"[yellow]  ⏹ Sisa blok di mapping `{name}` dilewati "
                            f"(opsi lanjutkan-jika-gagal nonaktif).[/yellow]"
                        )
                        break
                else:
                    processed_tables.add(src)

            results = {}
            for t in source_tables:
                if t in failed_tables:
                    self.update_log(t, "FAILED", "Satu atau lebih blok migrasi untuk tabel ini gagal")
                    results[t] = False
                elif t in processed_tables:
                    self.update_log(t, "SUCCESS")
                    results[t] = True
                else:
                    self.update_log(t, "FAILED", "Dilewati karena blok lain gagal (opsi lanjutkan-jika-gagal nonaktif)")
                    results[t] = False

            if failed_tables or len(processed_tables) < len(source_tables):
                console.print(f"[bold yellow]⚠ [{name.upper()}] Mapping selesai dengan masalah.[/bold yellow]")
            else:
                console.print(f"[bold green]✔ [{name.upper()}] Migrasi selesai![/bold green]")

            return results

        except Exception as err:
            # error tak terduga di luar loop blok (mis. gagal set FOREIGN_KEY_CHECKS / koneksi putus)
            conn_new.rollback()
            for t in source_tables:
                self.update_log(t, "FAILED", str(err))
            console.print(f"[bold red]✘ [{name.upper()}] Gagal migrasi:[/bold red] {err}")
            return {t: False for t in source_tables}

        finally:
            if self.force:
                self._restore_fk_checks(conn_new)
            conn_old.close()
            conn_new.close()

    def _normalize_columns(self, columns):
        """`columns` di JSON adalah {kolom_lama: kolom_baru}, tapi kolom_baru boleh
        berupa string tunggal atau list (satu field sumber dipetakan ke beberapa
        kolom tujuan sekaligus, nilainya diduplikasi ke semua kolom tujuan itu).
        Kembalikan list pasangan (old_col, new_col) yang sudah diratakan."""
        pairs = []
        for old_col, new_col in columns.items():
            targets = new_col if isinstance(new_col, list) else [new_col]
            for t in targets:
                pairs.append((old_col, t))
        return pairs

    # Operator yang tidak butuh nilai (dipasang langsung setelah nama kolom)
    _FILTER_OPS_NO_VALUE = {"IS NULL", "IS NOT NULL"}
    # Operator yang nilainya berupa list (dipetakan ke beberapa placeholder %s)
    _FILTER_OPS_LIST_VALUE = {"IN", "NOT IN"}
    _FILTER_OPS_ALLOWED = _FILTER_OPS_NO_VALUE | _FILTER_OPS_LIST_VALUE | {
        "=", "!=", ">", ">=", "<", "<=", "LIKE", "NOT LIKE",
    }

    def _build_filter_clause(self, filter_def):
        """Bangun klausa WHERE dari kondisi filter blok (lihat mapping_builder.py /
        webapp.py buat cara filter ini dibuat). Kembalikan (sql, params); sql kosong
        kalau tidak ada filter. Nilai selalu lewat placeholder %s, tidak pernah
        di-interpolasi langsung, jadi aman dari SQL injection."""
        conditions = (filter_def or {}).get("conditions") or []
        if not conditions:
            return "", []

        logic = (filter_def.get("logic") or "AND").upper()
        if logic not in ("AND", "OR"):
            logic = "AND"

        parts = []
        params = []
        for cond in conditions:
            col = cond["column"]
            op = cond["operator"]
            if op not in self._FILTER_OPS_ALLOWED:
                raise ValueError(f"Operator filter tidak dikenal: {op}")

            if op in self._FILTER_OPS_NO_VALUE:
                parts.append(f"`{col}` {op}")
            elif op in self._FILTER_OPS_LIST_VALUE:
                values = cond["value"] if isinstance(cond["value"], list) else [cond["value"]]
                if not values:
                    raise ValueError(f"Kondisi filter `{col}` {op} butuh minimal 1 nilai")
                placeholders = ", ".join(["%s"] * len(values))
                parts.append(f"`{col}` {op} ({placeholders})")
                params.extend(values)
            else:
                parts.append(f"`{col}` {op} %s")
                params.append(cond["value"])

        return f" WHERE {f' {logic} '.join(parts)}", params

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
        """Jalankan satu blok. Selalu mengembalikan dict statistik baris
        {total, inserted, skipped_duplicate, orphaned_parent, error} -- tidak pernah
        raise, supaya baris yang sempat ter-commit sebelum error tetap terhitung."""
        stats = {"total": 0, "inserted": 0, "skipped_duplicate": 0, "orphaned_parent": 0, "error": None}
        try:
            self._run_block_body(block, conn_old, conn_new, console, valid_id_cache, stats)
        except Exception as err:
            stats["error"] = str(err)
        return stats

    def _run_block_body(self, block, conn_old, conn_new, console, valid_id_cache, stats):
        source_table = block["source_table"]
        target_table = block["target_table"]
        id_source = block.get("id_source")
        id_target = block.get("id_target")
        id_mode = block.get("id_mode", "none")  # "preserve" | "preserve_secondary" | "none"
        upsert = block.get("upsert", False)
        dedup_key = block.get("dedup_key")
        fk = block.get("fk")
        columns = block.get("columns") or {}  # {kolom_lama: kolom_baru | [kolom_baru, ...]}
        unpivot = block.get("unpivot")  # lihat docstring DynamicMigrator soal column-to-row

        column_pairs = self._normalize_columns(columns)  # [(kolom_lama, kolom_baru), ...] sudah diratakan
        new_cols = [new_col for _, new_col in column_pairs]
        label_col = value_col = None
        if unpivot:
            label_col = unpivot["target_label_column"]
            value_col = unpivot["target_value_column"]

        coalesce_cols = self._coalesce_now_columns(
            conn_new, target_table, new_cols + ([label_col, value_col] if unpivot else [])
        )

        def placeholder_for(new_col):
            return "COALESCE(%s, CURRENT_TIMESTAMP)" if new_col in coalesce_cols else "%s"

        old_select_cols = []

        def add_select_col(c):
            if c not in old_select_cols:
                old_select_cols.append(c)

        if id_source:
            add_select_col(id_source)
        for old_col, _ in column_pairs:
            add_select_col(old_col)
        if fk:
            add_select_col(fk["source_column"])
        if unpivot:
            for item in unpivot["items"]:
                add_select_col(item["source_column"])

        already_migrated = set()
        if dedup_key and id_source:
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

        # FK ke induk hanya ditulis kalau target barunya bukan preserve id lama sendiri
        # (preserve_secondary / none) -- konsisten dengan aturan lama: mode "preserve"
        # memang tidak menyimpan kolom FK terpisah karena PK barunya sudah = id lama.
        fk_target_col = fk["target_column"] if fk else None
        fk_applies = bool(fk) and id_mode in ("preserve_secondary", "none")

        if id_mode == "preserve":
            lead_cols = [id_target]
            lead_placeholders = ["%s"]
        elif id_mode == "preserve_secondary":
            lead_cols = ([fk_target_col] if fk else []) + [id_target]
            lead_placeholders = (["%s"] if fk else []) + ["%s"]
        elif id_mode == "none":
            lead_cols = [fk_target_col] if fk else []
            lead_placeholders = ["%s"] if fk else []
        else:
            raise ValueError(f"id_mode tidak dikenal: {id_mode}")

        tail_cols = new_cols + ([label_col, value_col] if unpivot else [])
        tail_placeholders = [placeholder_for(c) for c in new_cols] + (["%s", "%s"] if unpivot else [])

        insert_cols = lead_cols + tail_cols
        values_sql = ", ".join(lead_placeholders + tail_placeholders)

        if upsert and id_mode == "preserve":
            update_sql = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in tail_cols)
            insert_sql = (
                f"INSERT INTO `{target_table}` ({', '.join(f'`{c}`' for c in insert_cols)}) "
                f"VALUES ({values_sql}) ON DUPLICATE KEY UPDATE {update_sql}"
            )
        else:
            insert_sql = (
                f"INSERT INTO `{target_table}` ({', '.join(f'`{c}`' for c in insert_cols)}) VALUES ({values_sql})"
            )

        where_sql, where_params = self._build_filter_clause(block.get("filter"))
        select_sql = f"SELECT {', '.join(f'`{c}`' for c in old_select_cols)} FROM `{source_table}`{where_sql}"

        cur_old = conn_old.cursor(dictionary=True)
        cur_new = conn_new.cursor()
        cur_old.execute(select_sql, where_params or None)

        while True:
            rows = cur_old.fetchmany(BATCH_SIZE)
            if not rows:
                break

            # Dihitung begitu baris terbaca (bukan setelah insert sukses), supaya baris
            # dalam batch yang gagal di-insert tetap terhitung "gagal", bukan hilang.
            stats["total"] += len(rows)

            batch = []
            for r in rows:
                if dedup_key and id_source and r[id_source] in already_migrated:
                    stats["skipped_duplicate"] += 1
                    continue

                lead_values = []
                if fk_applies:
                    fk_val = r[fk["source_column"]]
                    resolved = fk_val if fk_val in valid_parent_ids else None
                    if resolved is None:
                        stats["orphaned_parent"] += 1
                    lead_values.append(resolved)
                if id_mode in ("preserve", "preserve_secondary"):
                    lead_values.append(r[id_source])

                tail_values = [r[old_col] for old_col, _ in column_pairs]

                if unpivot:
                    skip_null = unpivot.get("skip_null", True)
                    for item in unpivot["items"]:
                        raw_val = r[item["source_column"]]
                        if skip_null and raw_val is None:
                            continue
                        batch.append(lead_values + tail_values + [item["label"], raw_val])
                else:
                    batch.append(lead_values + tail_values)

            if batch:
                cur_new.executemany(insert_sql, batch)
                conn_new.commit()
                stats["inserted"] += len(batch)

            console.print(f"[dim]  ...[{source_table} -> {target_table}] {stats['total']} baris sumber diproses[/dim]")

        cur_old.close()
        cur_new.close()

        console.print(
            f"[green]  ✔ [{source_table} -> {target_table}] {stats['inserted']} baris berhasil dipindahkan[/green]"
        )
        if stats["skipped_duplicate"]:
            console.print(
                f"[yellow]  ⚠ {stats['skipped_duplicate']} baris dilewati (sudah pernah dimigrasi sebelumnya)[/yellow]"
            )
        if stats["orphaned_parent"]:
            console.print(
                f"[yellow]  ⚠ {stats['orphaned_parent']} baris tidak punya induk valid "
                f"(`{fk['source_column']}` tidak ditemukan di `{fk['ref_source_table']}`.`{fk['ref_source_column']}`) "
                f"-> `{fk['target_column']}` diisi NULL[/yellow]"
            )
