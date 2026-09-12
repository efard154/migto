import os
from dotenv import load_dotenv


def reload_env():
    """Muat ulang .env ke os.environ (override nilai lama). Dipanggil lagi setelah
    tab Koneksi di web UI menyimpan .env baru, supaya config baru langsung kepakai
    tanpa perlu restart proses."""
    load_dotenv(override=True)


reload_env()


def _build(prefix):
    return {
        "host": os.getenv(f"{prefix}_HOST", "localhost"),
        "port": int(os.getenv(f"{prefix}_PORT", "3306")),
        "user": os.getenv(f"{prefix}_USER", "root"),
        "password": os.getenv(f"{prefix}_PASSWORD", ""),
        "database": os.getenv(f"{prefix}_NAME", ""),
    }


def get_old_db_config():
    return _build("OLD_DB")


def get_new_db_config():
    return _build("NEW_DB")


def get_migration_db_config():
    return _build("MIGRATION_DB")
