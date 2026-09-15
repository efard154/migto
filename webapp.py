"""
Web UI untuk migration tool: dashboard tabel, mapping builder drag-and-drop
(disimpan sebagai JSON di mappings/), serta jalankan & pantau migrasi secara
real-time lewat browser.

Jalankan:  python webapp.py
Buka:      http://127.0.0.1:8765
"""
import glob
import json
import os
import re
import threading
import time
import uuid

from flask import Flask, Response, jsonify, request

import config
import env_config
import migration_service
from db import connect_old, connect_new, connect_migration
from handler import migrator as migrator_module
from handler.migrator import run_migration_process

app = Flask(__name__)

JOBS = {}
JOBS_LOCK = threading.Lock()


@app.after_request
def _no_cache_static(response):
    """Matikan cache browser buat aset statis (index.html/app.js/style.css).
    Tanpa ini, browser bisa terus pakai app.js versi lama walau file di server
    sudah diperbarui -- fitur baru "kelihatan tidak jalan" padahal cuma stale
    cache, bukan bug di kode."""
    if request.path == "/" or request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response

RICH_TAG_RE = re.compile(r"\[/?[a-zA-Z0-9 _]+\]")
LEVEL_MARKERS = [
    ("bold red", "error"),
    ("red", "error"),
    ("bold green", "success"),
    ("green", "success"),
    ("yellow", "warning"),
    ("cyan", "info"),
    ("dim", "muted"),
]

REQUIRED_BLOCK_KEYS = ["source_table", "target_table", "id_mode"]
VALID_ID_MODES = ("preserve", "preserve_secondary", "none")
FILTER_OPS_NO_VALUE = ("IS NULL", "IS NOT NULL")
FILTER_OPS_LIST_VALUE = ("IN", "NOT IN")
VALID_FILTER_OPS = FILTER_OPS_NO_VALUE + FILTER_OPS_LIST_VALUE + ("=", "!=", ">", ">=", "<", "<=", "LIKE", "NOT LIKE")


# ---------------------------------------------------------------------------
# Console adapter: menyalurkan console.print(rich-markup) milik migrator ke log job
# ---------------------------------------------------------------------------

class WebConsole:
    def __init__(self, job):
        self.job = job

    def print(self, msg="", *args, **kwargs):
        text = str(msg)
        level = "info"
        for tag, lvl in LEVEL_MARKERS:
            if f"[{tag}]" in text:
                level = lvl
                break
        clean = RICH_TAG_RE.sub("", text).strip()
        if clean:
            # "detail" = baris minor (progres per-batch, dll) yang disembunyikan
            # secara default di UI -- dipisah dari `level` supaya warna & filter
            # detail bisa berubah independen satu sama lain.
            self.job["logs"].append({
                "level": level,
                "text": clean,
                "ts": time.time(),
                "detail": level == "muted",
            })


# ---------------------------------------------------------------------------
# Mapping validation & storage helpers
# ---------------------------------------------------------------------------

def _mapping_path(name):
    safe = re.sub(r"[^A-Za-z0-9_-]", "", name)
    if not safe:
        raise ValueError("Nama mapping tidak valid")
    return os.path.join(migrator_module.MAPPINGS_DIR, f"{safe}.json")


def validate_mapping(mapping):
    if not isinstance(mapping, dict):
        raise ValueError("Mapping harus berupa object JSON")
    if not mapping.get("blocks"):
        raise ValueError("Mapping harus punya minimal 1 blok")

    for i, b in enumerate(mapping["blocks"], 1):
        for key in REQUIRED_BLOCK_KEYS:
            if not b.get(key):
                raise ValueError(f"Blok #{i}: field '{key}' wajib diisi")

        id_mode = b["id_mode"]
        if id_mode not in VALID_ID_MODES:
            raise ValueError(f"Blok #{i}: id_mode harus salah satu dari {', '.join(VALID_ID_MODES)}")

        if id_mode in ("preserve", "preserve_secondary"):
            if not b.get("id_source") or not b.get("id_target"):
                raise ValueError(f"Blok #{i}: id_source & id_target wajib diisi untuk id_mode '{id_mode}'")
        elif b.get("id_source") or b.get("id_target"):
            raise ValueError(f"Blok #{i}: id_source/id_target tidak boleh diisi kalau id_mode 'none' (tabel tanpa kolom kunci)")

        columns = b.get("columns") or {}
        unpivot = b.get("unpivot")
        if not isinstance(columns, dict):
            raise ValueError(f"Blok #{i}: 'columns' harus berupa object")
        if not columns and not unpivot:
            raise ValueError(f"Blok #{i}: minimal harus ada 1 pasangan kolom, atau isi blok unpivot")

        # Nilai tiap pasangan kolom boleh string (1 kolom sumber -> 1 kolom tujuan)
        # atau list (1 kolom sumber -> beberapa kolom tujuan sekaligus).
        all_target_cols = []
        for old_col, new_col in columns.items():
            if isinstance(new_col, list):
                if not new_col or not all(isinstance(t, str) and t for t in new_col):
                    raise ValueError(
                        f"Blok #{i}: pemetaan kolom `{old_col}` tidak valid "
                        f"(daftar kolom tujuan kosong atau berisi nilai tidak valid)"
                    )
                all_target_cols.extend(new_col)
            elif isinstance(new_col, str) and new_col:
                all_target_cols.append(new_col)
            else:
                raise ValueError(f"Blok #{i}: pemetaan kolom `{old_col}` tidak valid")

        if unpivot:
            if not isinstance(unpivot, dict):
                raise ValueError(f"Blok #{i}: 'unpivot' harus berupa object")
            for key in ["target_label_column", "target_value_column", "items"]:
                if not unpivot.get(key):
                    raise ValueError(f"Blok #{i}: field unpivot '{key}' wajib diisi")
            if unpivot["target_label_column"] == unpivot["target_value_column"]:
                raise ValueError(f"Blok #{i}: kolom label & value unpivot tidak boleh sama")
            if not isinstance(unpivot["items"], list) or not unpivot["items"]:
                raise ValueError(f"Blok #{i}: unpivot.items harus list berisi minimal 1 item")
            for j, item in enumerate(unpivot["items"], 1):
                if not item.get("source_column") or not item.get("label"):
                    raise ValueError(f"Blok #{i}: unpivot.items #{j} wajib punya 'source_column' dan 'label'")

        fk = b.get("fk")
        if fk:
            for key in ["source_column", "target_column", "ref_source_table", "ref_source_column"]:
                if not fk.get(key):
                    raise ValueError(f"Blok #{i}: field FK '{key}' wajib diisi")

        filter_def = b.get("filter")
        if filter_def:
            if not isinstance(filter_def, dict):
                raise ValueError(f"Blok #{i}: 'filter' harus berupa object")
            logic = filter_def.get("logic", "AND")
            if logic not in ("AND", "OR"):
                raise ValueError(f"Blok #{i}: filter.logic harus 'AND' atau 'OR'")
            conditions = filter_def.get("conditions")
            if not isinstance(conditions, list) or not conditions:
                raise ValueError(f"Blok #{i}: filter.conditions harus list berisi minimal 1 kondisi")
            for j, cond in enumerate(conditions, 1):
                if not isinstance(cond, dict) or not cond.get("column") or not isinstance(cond["column"], str):
                    raise ValueError(f"Blok #{i}: filter.conditions #{j} wajib punya 'column'")
                op = cond.get("operator")
                if op not in VALID_FILTER_OPS:
                    raise ValueError(f"Blok #{i}: filter.conditions #{j} operator tidak dikenal: {op}")
                if op not in FILTER_OPS_NO_VALUE:
                    value = cond.get("value")
                    if op in FILTER_OPS_LIST_VALUE:
                        if not isinstance(value, list) or not value or not all(v not in (None, "") for v in value):
                            raise ValueError(
                                f"Blok #{i}: filter.conditions #{j} 'value' untuk operator {op} "
                                f"harus list berisi minimal 1 nilai"
                            )
                    elif value in (None, ""):
                        raise ValueError(f"Blok #{i}: filter.conditions #{j} wajib punya 'value' untuk operator {op}")

        # Satu kolom tujuan cuma boleh diisi sekali per blok (id/fk/unpivot/columns
        # tidak boleh tabrakan menulis ke kolom fisik yang sama).
        reserved_target_cols = [c for c in [b.get("id_target"), fk.get("target_column") if fk else None] if c]
        if unpivot:
            reserved_target_cols += [unpivot.get("target_label_column"), unpivot.get("target_value_column")]
        seen_target_cols = set()
        for t in all_target_cols + reserved_target_cols:
            if t in seen_target_cols:
                raise ValueError(
                    f"Blok #{i}: kolom tujuan `{t}` dipetakan lebih dari sekali "
                    f"(cek pasangan kolom, id_target, fk, dan unpivot supaya tidak tabrakan)"
                )
            seen_target_cols.add(t)


# ---------------------------------------------------------------------------
# Health check koneksi (buat indikator di toolbar)
# ---------------------------------------------------------------------------

@app.get("/api/health")
def api_health():
    def ping(connect_fn):
        try:
            conn = connect_fn()
            conn.close()
            return True
        except Exception:
            return False

    return jsonify({
        "old": ping(connect_old),
        "new": ping(connect_new),
        "migration": ping(connect_migration),
    })


# ---------------------------------------------------------------------------
# Setup koneksi (OLD_DB / NEW_DB / MIGRATION_DB) -- baca/tulis .env
# ---------------------------------------------------------------------------

@app.get("/api/settings/connections")
def get_connection_settings():
    return jsonify(env_config.get_connection_settings())


@app.post("/api/settings/connections/test")
def test_connection_settings():
    data = request.get_json(force=True, silent=True) or {}
    group = data.get("group")
    ok, error = env_config.test_connection(group, data)
    return jsonify({"ok": ok, "error": error})


@app.put("/api/settings/connections/<group>")
def put_connection_settings(group):
    data = request.get_json(force=True, silent=True) or {}
    try:
        env_config.save_connection_settings(group, data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    config.reload_env()

    if group == "migration":
        try:
            migration_service.ensure_migration_logs_table()
        except Exception as err:
            return jsonify({"ok": True, "warning": f"Tersimpan, tapi gagal siapkan tabel migration_logs: {err}"})

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Static data: tabel & kolom
# ---------------------------------------------------------------------------

@app.get("/api/tables")
def api_tables():
    old_tables = migration_service.get_old_tables()
    new_tables = migration_service.get_new_tables()
    old_counts = migration_service.get_old_row_estimates()
    new_counts = migration_service.get_new_row_estimates()
    mapped = migrator_module.MAPPING_REGISTRY

    return jsonify({
        "old": [
            {
                "name": t,
                "row_estimate": old_counts.get(t),
                "mapped": t in mapped,
                "mapping_name": mapped[t]["name"] if t in mapped else None,
            }
            for t in old_tables
        ],
        "new": [{"name": t, "row_estimate": new_counts.get(t)} for t in new_tables],
    })


@app.get("/api/columns/old/<table>")
def api_columns_old(table):
    return jsonify(migration_service.get_old_columns(table))


@app.get("/api/columns/new/<table>")
def api_columns_new(table):
    return jsonify(migration_service.get_new_columns(table))


# ---------------------------------------------------------------------------
# Mapping CRUD
# ---------------------------------------------------------------------------

@app.get("/api/mappings")
def list_mappings():
    result = []
    for path in sorted(glob.glob(os.path.join(migrator_module.MAPPINGS_DIR, "*.json"))):
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
        result.append({
            "name": m["name"],
            "description": m.get("description", ""),
            "blocks": len(m["blocks"]),
            "source_tables": sorted({b["source_table"] for b in m["blocks"]}),
        })
    return jsonify(result)


@app.get("/api/mappings/<name>")
def get_mapping(name):
    path = _mapping_path(name)
    if not os.path.exists(path):
        return jsonify({"error": "Mapping tidak ditemukan"}), 404
    with open(path, encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.put("/api/mappings/<name>")
def put_mapping(name):
    mapping = request.get_json(force=True, silent=True) or {}
    mapping["name"] = name
    mapping.setdefault("description", "")

    try:
        validate_mapping(mapping)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    os.makedirs(migrator_module.MAPPINGS_DIR, exist_ok=True)
    with open(_mapping_path(name), "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2, ensure_ascii=False)

    migrator_module.reload_registry()
    return jsonify({"ok": True})


@app.delete("/api/mappings/<name>")
def delete_mapping(name):
    path = _mapping_path(name)
    if os.path.exists(path):
        os.remove(path)
    migrator_module.reload_registry()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Jalankan & pantau migrasi
# ---------------------------------------------------------------------------

def _run_job(job_id, tables, force, continue_on_error, clear_existing):
    job = JOBS[job_id]
    console = WebConsole(job)
    try:
        migration_service.register_migration_queue(tables)
        summary = run_migration_process(
            tables,
            console=console,
            force=force,
            continue_on_error=continue_on_error,
            clear_existing=clear_existing,
        )
        job["summary"] = summary
        if summary["failed_count"] == 0 and summary["skipped_count"] == 0:
            job["status"] = "SUCCESS"
        elif summary["success_count"] == 0:
            job["status"] = "FAILED"
        else:
            job["status"] = "PARTIAL"
    except Exception as err:
        job["status"] = "FAILED"
        job["logs"].append({"level": "error", "text": f"Migrasi berhenti: {err}", "ts": time.time()})
    finally:
        job["finished_at"] = time.time()


@app.post("/api/run")
def api_run():
    data = request.get_json(force=True, silent=True) or {}
    tables = data.get("tables") or []
    force_migration = bool(data.get("force"))
    continue_on_error = data.get("continue_on_error")
    continue_on_error = True if continue_on_error is None else bool(continue_on_error)
    clear_existing = bool(data.get("clear_existing"))
    if not tables:
        return jsonify({"error": "Pilih minimal satu tabel"}), 400

    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "tables": tables,
        "force": force_migration,
        "continue_on_error": continue_on_error,
        "clear_existing": clear_existing,
        "status": "RUNNING",
        "logs": [],
        "summary": None,
        "started_at": time.time(),
        "finished_at": None,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job

    threading.Thread(
        target=_run_job,
        args=(job_id, tables, force_migration, continue_on_error, clear_existing),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


@app.get("/api/run/<job_id>")
def api_run_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan"}), 404
    return jsonify({
        "id": job["id"],
        "tables": job["tables"],
        "force": job.get("force", False),
        "continue_on_error": job.get("continue_on_error", True),
        "clear_existing": job.get("clear_existing", False),
        "status": job["status"],
        "summary": job.get("summary"),
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "log_count": len(job["logs"]),
    })


@app.get("/api/run/<job_id>/logs")
def api_run_logs(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan"}), 404
    return jsonify(job["logs"])


@app.get("/api/run/<job_id>/stream")
def api_run_stream(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan"}), 404

    def gen():
        idx = 0
        while True:
            logs = job["logs"]
            while idx < len(logs):
                yield f"data: {json.dumps(logs[idx])}\n\n"
                idx += 1
            if job["status"] != "RUNNING":
                yield f"event: done\ndata: {json.dumps({'status': job['status'], 'summary': job.get('summary')})}\n\n"
                break
            time.sleep(0.3)

    return Response(gen(), mimetype="text/event-stream")


@app.get("/api/jobs")
def api_jobs():
    jobs = sorted(
        (
            {
                "id": j["id"],
                "tables": j["tables"],
                "force": j.get("force", False),
                "continue_on_error": j.get("continue_on_error", True),
                "clear_existing": j.get("clear_existing", False),
                "status": j["status"],
                "summary": j.get("summary"),
                "started_at": j["started_at"],
                "finished_at": j["finished_at"],
            }
            for j in JOBS.values()
        ),
        key=lambda j: j["started_at"],
        reverse=True,
    )
    return jsonify(list(jobs))


# ---------------------------------------------------------------------------
# Halaman utama (SPA statis)
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return app.send_static_file("index.html")


if __name__ == "__main__":
    # Port 5000 sering diblok Windows sendiri (masuk excluded port range bawaan
    # Hyper-V/WSL -> bind() gagal dengan "forbidden by access permissions"),
    # jadi default ke port lain. Override lewat env var PORT kalau perlu.
    port = int(os.environ.get("PORT", 8765))
    app.run(host="127.0.0.1", port=port, debug=True, threaded=True)
