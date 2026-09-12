from abc import ABC, abstractmethod
from db import connect_migration


class BaseMigrator(ABC):
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