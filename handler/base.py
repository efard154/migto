from abc import ABC, abstractmethod
from db import connect_migration


class BaseMigrator(ABC):
    def _disable_fk_checks(self, conn):
        """Matikan FOREIGN_KEY_CHECKS di session koneksi ini (mode force migration),
        supaya insert tidak digagalkan urutan tabel/FK yatim."""
        cur = conn.cursor()
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        cur.close()

    def _restore_fk_checks(self, conn):
        try:
            cur = conn.cursor()
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
            cur.close()
        except Exception:
            pass  # koneksi mungkin sudah bermasalah/tertutup, aman diabaikan

    def _clear_target_table(self, conn, table):
        """Hapus semua baris di tabel tujuan sebelum insert (opsi 'hapus data
        sebelumnya' pada force migration, buat reseed bersih). Pakai DELETE FROM,
        bukan TRUNCATE, supaya tetap ikut transaksi & FOREIGN_KEY_CHECKS session
        yang sedang aktif alih-alih implicit-commit ala DDL."""
        cur = conn.cursor()
        cur.execute(f"DELETE FROM `{table}`")
        deleted = cur.rowcount
        cur.close()
        return deleted

    def update_log(self, table_name: str, status: str, error_msg: str = None):
        """Memperbarui status transaksi migrasi ke MIGRATION_DB."""
        try:
            conn = connect_migration()
            with conn.cursor() as cursor:
                if status == "SUCCESS":
                    cursor.execute("""
                        UPDATE migration_logs 
                        SET status = %s, finished_at = CURRENT_TIMESTAMP 
                        WHERE table_name = %s AND status IN ('PENDING', 'IN_PROGRESS')
                    """, (status, table_name))
                elif status == "FAILED":
                    cursor.execute("""
                        UPDATE migration_logs 
                        SET status = %s, error_message = %s, finished_at = CURRENT_TIMESTAMP 
                        WHERE table_name = %s AND status IN ('PENDING', 'IN_PROGRESS')
                    """, (status, error_msg, table_name))
                else:
                    cursor.execute("""
                        UPDATE migration_logs 
                        SET status = %s WHERE table_name = %s
                    """, (status, table_name))
            conn.commit()
            conn.close()
        except Exception as err:
            print(f"[Log Error] {err}")

    @abstractmethod
    def execute(self, console):
        """Metode eksekusi wajib untuk setiap handler."""
        pass