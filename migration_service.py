"""Helper bersama untuk daftar tabel/kolom & antrean migrasi, dipakai baik oleh
CLI (main.py) maupun web app (webapp.py) supaya logikanya tidak dobel/ketinggalan
sinkron di dua tempat."""
import mysql.connector

from db import connect_old, connect_new, connect_migration


def _show_tables(conn_factory):
    conn = conn_factory()
    cur = conn.cursor()
    cur.execute("SHOW TABLES")
    tables = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return tables


def get_old_tables():
    return _show_tables(connect_old)


def get_new_tables():
    return _show_tables(connect_new)


def _get_columns(conn_factory, table):
    conn = conn_factory()
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
    conn.close()
    return cols


def get_old_columns(table):
    return _get_columns(connect_old, table)


def get_new_columns(table):
    return _get_columns(connect_new, table)


def _get_row_estimates(conn_factory):
    """Perkiraan jumlah baris via information_schema (cepat), bukan COUNT(*) yang
    full-scan -- cukup buat tampilan dashboard, bukan angka yang harus presisi."""
    conn = conn_factory()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT TABLE_NAME AS name, TABLE_ROWS AS row_estimate "
        "FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
    )
    rows = {r["name"]: r["row_estimate"] for r in cur.fetchall()}
    cur.close()
    conn.close()
    return rows


def get_old_row_estimates():
    return _get_row_estimates(connect_old)


def get_new_row_estimates():
    return _get_row_estimates(connect_new)


def ensure_migration_logs_table():
    """Buat tabel migration_logs kalau belum ada -- supaya MIGRATION_DB yang baru
    ditunjuk (mis. lewat tab Koneksi) langsung siap dipakai tanpa setup manual.
    UNIQUE KEY di table_name dibutuhkan oleh ON DUPLICATE KEY UPDATE di
    register_migration_queue."""
    conn = connect_migration()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS migration_logs (
            id INT NOT NULL AUTO_INCREMENT,
            table_name VARCHAR(191) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
            error_message TEXT NULL,
            started_at DATETIME NULL,
            finished_at DATETIME NULL,
            PRIMARY KEY (id),
            UNIQUE KEY uniq_table_name (table_name)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    conn.commit()
    cursor.close()
    conn.close()


def register_migration_queue(selected_tables):
    """Mencatat antrean tabel yang dipilih ke MIGRATION_DB dengan status 'PENDING'."""
    ensure_migration_logs_table()
    conn = connect_migration()
    cursor = conn.cursor()

    for tbl in selected_tables:
        cursor.execute("""
            INSERT INTO migration_logs (table_name, status, started_at)
            VALUES (%s, 'PENDING', CURRENT_TIMESTAMP)
            ON DUPLICATE KEY UPDATE
                status = 'PENDING',
                error_message = NULL,
                started_at = CURRENT_TIMESTAMP,
                finished_at = NULL
        """, (tbl,))

    conn.commit()
    cursor.close()
    conn.close()
