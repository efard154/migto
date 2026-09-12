"""Kelola pengaturan koneksi (OLD_DB/NEW_DB/MIGRATION_DB) yang disimpan di .env,
supaya bisa diubah lewat tab Koneksi di web UI tanpa edit file manual."""
import os

import mysql.connector

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

GROUP_PREFIX = {
    "old": "OLD_DB",
    "new": "NEW_DB",
    "migration": "MIGRATION_DB",
}


def _sanitize(value):
    """Buang newline/carriage-return supaya satu field tidak bisa menyuntik baris
    .env baru (mis. lewat password yang mengandung newline)."""
    return str(value).replace("\r", "").replace("\n", "").strip()


def read_env_values():
    """Baca .env apa adanya jadi dict key->value. Kosong kalau file belum ada."""
    values = {}
    if os.path.exists(ENV_FILE):
        # newline="" -> jangan normalisasi akhir baris (biar LF/CRLF asli file tidak berubah)
        with open(ENV_FILE, "r", encoding="utf-8", newline="") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip()
    return values


def get_connection_settings():
    """Nilai saat ini per grup. Password TIDAK pernah dikirim balik ke browser --
    hanya ditandai sudah diisi atau belum lewat `has_password`."""
    values = read_env_values()
    result = {}
    for group, prefix in GROUP_PREFIX.items():
        result[group] = {
            "host": values.get(f"{prefix}_HOST", ""),
            "port": values.get(f"{prefix}_PORT", "3306"),
            "user": values.get(f"{prefix}_USER", ""),
            "database": values.get(f"{prefix}_NAME", ""),
            "has_password": bool(values.get(f"{prefix}_PASSWORD")),
        }
    return result


def _resolve_password(group, submitted_password):
    """Kalau field password dikosongkan di form, pakai password yang sudah
    tersimpan (supaya tidak ketiban kosong tiap kali user cuma ganti host/port)."""
    if submitted_password:
        return _sanitize(submitted_password)
    values = read_env_values()
    return values.get(f"{GROUP_PREFIX[group]}_PASSWORD", "")


def save_connection_settings(group, data):
    if group not in GROUP_PREFIX:
        raise ValueError(f"Grup koneksi tidak dikenal: {group}")
    prefix = GROUP_PREFIX[group]

    port = str(data.get("port") or "3306").strip()
    if not port.isdigit():
        raise ValueError("Port harus berupa angka")

    updates = {
        f"{prefix}_HOST": _sanitize(data.get("host") or "localhost"),
        f"{prefix}_PORT": port,
        f"{prefix}_USER": _sanitize(data.get("user") or "root"),
        f"{prefix}_NAME": _sanitize(data.get("database") or ""),
    }
    if data.get("password"):
        updates[f"{prefix}_PASSWORD"] = _sanitize(data["password"])

    _update_env_file(updates)


def _update_env_file(updates):
    lines = []
    # newline="" di kedua sisi (baca & tulis) -> akhir baris asli file (LF/CRLF)
    # tidak diubah jadi konvensi OS yang menjalankan skrip ini.
    newline = "\n"
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8", newline="") as f:
            lines = f.readlines()
        if lines and lines[0].endswith("\r\n"):
            newline = "\r\n"

    seen = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            lines[i] = f"{key}={updates[key]}{newline}"
            seen.add(key)

    for key, val in updates.items():
        if key not in seen:
            if lines and not lines[-1].endswith(("\n", "\r\n")):
                lines[-1] += newline
            lines.append(f"{key}={val}{newline}")

    with open(ENV_FILE, "w", encoding="utf-8", newline="") as f:
        f.writelines(lines)


def test_connection(group, data):
    """Coba konek pakai parameter yang dikirim form (belum tentu tersimpan).
    Password kosong -> pakai password tersimpan (lihat _resolve_password)."""
    if group not in GROUP_PREFIX:
        return False, f"Grup koneksi tidak dikenal: {group}"

    try:
        port = int(data.get("port") or 3306)
    except (TypeError, ValueError):
        return False, "Port harus berupa angka"

    try:
        conn = mysql.connector.connect(
            host=data.get("host") or "localhost",
            port=port,
            user=data.get("user") or "root",
            password=_resolve_password(group, data.get("password")),
            database=data.get("database") or "",
            connection_timeout=5,
        )
        conn.close()
        return True, None
    except Exception as err:
        return False, str(err)
