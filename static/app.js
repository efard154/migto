const state = {
  tables: { old: [], new: [] },
  currentMapping: null,
  editingBlockIndex: null,
  pairs: {}, // { newCol: oldCol }
  oldColumns: [],
  newColumns: [],
  selectedRunTables: new Set(),
  currentJobId: null,
  eventSource: null,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function fillSelect(id, values, placeholder) {
  const sel = document.getElementById(id);
  sel.innerHTML = "";
  if (placeholder) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = placeholder;
    sel.appendChild(opt);
  }
  values.forEach((v) => {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = v;
    sel.appendChild(opt);
  });
}

function formatCount(n) {
  if (n === null || n === undefined) return "?";
  return Number(n).toLocaleString("id-ID");
}

function formatTime(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString("id-ID");
}

function toast(msg, type = "info") {
  const container = document.getElementById("toast-container");
  const el = document.createElement("div");
  el.className = "toast toast-" + type;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

function switchTab(tabName) {
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tabName));
  document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("active", s.id === `tab-${tabName}`));
  if (tabName === "run") refreshJobsList();
}

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

// ---------------------------------------------------------------------------
// Toolbar: refresh, tema, indikator koneksi, status bar
// ---------------------------------------------------------------------------

function setStatus(left, right) {
  if (left !== undefined) document.getElementById("status-left").textContent = left;
  if (right !== undefined) document.getElementById("status-right").textContent = right;
}

function applyTheme(theme) {
  if (theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("theme", theme);
  } else {
    delete document.documentElement.dataset.theme;
    localStorage.removeItem("theme");
  }
}

(function initTheme() {
  const saved = localStorage.getItem("theme");
  if (saved) document.documentElement.dataset.theme = saved;
})();

document.getElementById("btn-theme").addEventListener("click", () => {
  const current = document.documentElement.dataset.theme;
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const effectiveDark = current ? current === "dark" : prefersDark;
  applyTheme(effectiveDark ? "light" : "dark");
});

document.getElementById("btn-refresh").addEventListener("click", async () => {
  await refreshAll();
  toast("Data diperbarui", "success");
});

async function checkHealth() {
  try {
    const health = await api("/api/health");
    ["old", "new", "migration"].forEach((key) => {
      const el = document.getElementById(`conn-${key}`);
      el.classList.toggle("ok", !!health[key]);
      el.classList.toggle("fail", !health[key]);
    });
  } catch {
    // biarkan indikator apa adanya kalau health check sendiri gagal
  }
}

async function refreshAll() {
  setStatus("Memuat data...");
  await Promise.all([loadDashboard(), loadMappingsList(), checkHealth(), loadConnectionSettings()]);
  renderSidebar();
  setStatus(
    "Siap.",
    `${state.tables.old.length} tabel OLD_DB · ${state.tables.new.length} tabel NEW_DB`
  );
}

// ---------------------------------------------------------------------------
// Setup Koneksi (OLD_DB / NEW_DB / MIGRATION_DB)
// ---------------------------------------------------------------------------

async function loadConnectionSettings() {
  let settings;
  try {
    settings = await api("/api/settings/connections");
  } catch {
    return;
  }
  document.querySelectorAll(".conn-panel").forEach((panel) => {
    const group = panel.dataset.group;
    const s = settings[group];
    if (!s) return;
    panel.querySelector('[data-field="host"]').value = s.host || "";
    panel.querySelector('[data-field="port"]').value = s.port || "";
    panel.querySelector('[data-field="user"]').value = s.user || "";
    panel.querySelector('[data-field="database"]').value = s.database || "";
    const pwField = panel.querySelector('[data-field="password"]');
    pwField.value = "";
    pwField.placeholder = s.has_password ? "•••••• (sudah diisi, kosongkan jika tidak diubah)" : "(belum diisi)";
    panel.querySelector(".conn-result").textContent = "";
  });
}

function readConnPanel(panel) {
  const data = { group: panel.dataset.group };
  panel.querySelectorAll(".conn-field").forEach((input) => {
    data[input.dataset.field] = input.value;
  });
  return data;
}

document.querySelectorAll(".conn-panel").forEach((panel) => {
  const resultEl = panel.querySelector(".conn-result");

  panel.querySelector(".btn-test-conn").addEventListener("click", async () => {
    resultEl.className = "conn-result pending";
    resultEl.textContent = "Menguji koneksi...";
    try {
      const res = await api("/api/settings/connections/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(readConnPanel(panel)),
      });
      resultEl.className = "conn-result " + (res.ok ? "ok" : "fail");
      resultEl.textContent = res.ok ? "✔ Berhasil terhubung" : `✘ ${res.error}`;
    } catch (err) {
      resultEl.className = "conn-result fail";
      resultEl.textContent = "✘ " + err.message;
    }
  });

  panel.querySelector(".btn-save-conn").addEventListener("click", async () => {
    const group = panel.dataset.group;
    try {
      const res = await api(`/api/settings/connections/${encodeURIComponent(group)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(readConnPanel(panel)),
      });
      if (res.warning) {
        toast(res.warning, "error");
      } else {
        toast(`Koneksi ${group.toUpperCase()} tersimpan`, "success");
      }
      await loadConnectionSettings();
      await checkHealth();
    } catch (err) {
      toast("Gagal simpan koneksi: " + err.message, "error");
    }
  });
});

// ---------------------------------------------------------------------------
// Sidebar navigator (tree ala Navicat)
// ---------------------------------------------------------------------------

document.querySelectorAll(".tree-header").forEach((header) => {
  header.addEventListener("click", () => {
    const list = document.getElementById(header.dataset.collapse);
    header.classList.toggle("collapsed");
    list.classList.toggle("collapsed");
  });
});

function renderSidebar() {
  renderSidebarOld();
  renderSidebarNew();
}

function renderSidebarOld() {
  const el = document.getElementById("tree-old");
  document.getElementById("count-old").textContent = state.tables.old.length ? `(${state.tables.old.length})` : "";
  el.innerHTML = "";
  if (state.tables.old.length === 0) {
    el.innerHTML = '<div class="tree-empty">Tidak ada tabel</div>';
    return;
  }
  state.tables.old.forEach((t) => {
    const item = document.createElement("div");
    item.className = "tree-item";
    item.innerHTML = `
      <span class="ti-icon">📄</span>
      <span class="ti-name">${t.name}</span>
      ${t.mapped ? '<span class="ti-mapped" title="Punya mapping khusus"></span>' : ""}
    `;
    item.title = `${t.name} — ~${formatCount(t.row_estimate)} baris${t.mapped ? ` — mapping: ${t.mapping_name}.json` : ""}`;
    item.addEventListener("click", () => {
      switchTab("mapping");
      document.getElementById("block-source-table").value = t.name;
      document.getElementById("block-source-table").dispatchEvent(new Event("change"));
      highlightTreeItem(item);
    });
    el.appendChild(item);
  });
}

function renderSidebarNew() {
  const el = document.getElementById("tree-new");
  document.getElementById("count-new").textContent = state.tables.new.length ? `(${state.tables.new.length})` : "";
  el.innerHTML = "";
  if (state.tables.new.length === 0) {
    el.innerHTML = '<div class="tree-empty">Tidak ada tabel</div>';
    return;
  }
  state.tables.new.forEach((t) => {
    const item = document.createElement("div");
    item.className = "tree-item";
    item.innerHTML = `<span class="ti-icon">📄</span><span class="ti-name">${t.name}</span>`;
    item.title = `${t.name} — ~${formatCount(t.row_estimate)} baris`;
    item.addEventListener("click", () => {
      switchTab("mapping");
      document.getElementById("block-target-table").value = t.name;
      document.getElementById("block-target-table").dispatchEvent(new Event("change"));
      highlightTreeItem(item);
    });
    el.appendChild(item);
  });
}

function renderSidebarMappings(list) {
  const el = document.getElementById("tree-mappings");
  document.getElementById("count-mappings").textContent = list.length ? `(${list.length})` : "";
  el.innerHTML = "";
  if (list.length === 0) {
    el.innerHTML = '<div class="tree-empty">Belum ada mapping</div>';
    return;
  }
  list.forEach((m) => {
    const item = document.createElement("div");
    item.className = "tree-item";
    item.innerHTML = `<span class="ti-icon">🔀</span><span class="ti-name">${m.name}.json</span>`;
    item.title = `${m.description || m.name} — ${m.blocks} blok (${m.source_tables.join(", ")})`;
    item.addEventListener("click", () => {
      switchTab("mapping");
      document.getElementById("mapping-select").value = m.name;
      document.getElementById("mapping-select").dispatchEvent(new Event("change"));
      highlightTreeItem(item);
    });
    el.appendChild(item);
  });
}

function highlightTreeItem(item) {
  document.querySelectorAll(".tree-item.active").forEach((el) => el.classList.remove("active"));
  item.classList.add("active");
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

async function loadDashboard() {
  const data = await api("/api/tables");
  state.tables = data;
  renderTableGrid("dashboard-old-tables", data.old, true);
  renderTableGrid("dashboard-new-tables", data.new, false);
  populateTableSelects();
  renderRunTableList();
}

function renderTableGrid(elId, tables, showMappingBadge) {
  const el = document.getElementById(elId);
  el.innerHTML = "";
  if (tables.length === 0) {
    el.innerHTML = '<div class="obj-list-empty">Tidak ada tabel</div>';
    return;
  }
  tables.forEach((t) => {
    const row = document.createElement("div");
    row.className = "obj-row";
    row.innerHTML = `
      <span class="obj-icon">📄</span>
      <span class="obj-name">${t.name}</span>
      <span class="obj-meta">~${formatCount(t.row_estimate)} baris</span>
      <span class="obj-badge-slot">${showMappingBadge && t.mapped ? `<span class="badge mapped">${t.mapping_name}.json</span>` : ""}</span>
    `;
    el.appendChild(row);
  });
}

function populateTableSelects() {
  fillSelect("block-source-table", state.tables.old.map((t) => t.name), "-- pilih --");
  fillSelect("block-target-table", state.tables.new.map((t) => t.name), "-- pilih --");
  fillSelect("fk-ref-table", state.tables.old.map((t) => t.name), "-- pilih --");
}

// ---------------------------------------------------------------------------
// Mapping builder: pilih tabel sumber/tujuan
// ---------------------------------------------------------------------------

async function onSourceTableChange() {
  const table = document.getElementById("block-source-table").value;
  if (!table) {
    state.oldColumns = [];
    renderDnd();
    return;
  }
  state.oldColumns = await api(`/api/columns/old/${encodeURIComponent(table)}`);
  fillSelect("block-id-source", state.oldColumns.map((c) => c.name));
  fillSelect("fk-source-column", state.oldColumns.map((c) => c.name));
  renderDnd();
}

async function onTargetTableChange() {
  const table = document.getElementById("block-target-table").value;
  if (!table) {
    state.newColumns = [];
    renderDnd();
    return;
  }
  state.newColumns = await api(`/api/columns/new/${encodeURIComponent(table)}`);
  fillSelect("block-id-target", state.newColumns.map((c) => c.name));
  fillSelect("fk-target-column", state.newColumns.map((c) => c.name));
  renderDnd();
}

document.getElementById("block-source-table").addEventListener("change", async () => {
  state.pairs = {};
  await onSourceTableChange();
  updateAddBlockState();
});
document.getElementById("block-target-table").addEventListener("change", async () => {
  state.pairs = {};
  await onTargetTableChange();
  updateAddBlockState();
});

document.getElementById("fk-ref-table").addEventListener("change", async (e) => {
  const table = e.target.value;
  if (!table) {
    fillSelect("fk-ref-column", []);
    return;
  }
  const cols = await api(`/api/columns/old/${encodeURIComponent(table)}`);
  fillSelect("fk-ref-column", cols.map((c) => c.name));
});

document.getElementById("block-fk-enable").addEventListener("change", (e) => {
  document.getElementById("fk-fields").hidden = !e.target.checked;
  renderDnd();
  updateAddBlockState();
});

// ----- Segmented control: mode ID (preserve / preserve_secondary) -----

function getIdMode() {
  return document.querySelector("#block-id-mode-group .seg-btn.active")?.dataset.value || "preserve";
}

function setIdMode(value) {
  document.querySelectorAll("#block-id-mode-group .seg-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.value === value);
  });
  applyIdModeSideEffects(value);
}

function applyIdModeSideEffects(value) {
  const secondary = value === "preserve_secondary";
  const upsert = document.getElementById("block-upsert");
  upsert.checked = !secondary;
  upsert.disabled = secondary;
}

document.querySelectorAll("#block-id-mode-group .seg-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    setIdMode(btn.dataset.value);
    updateAddBlockState();
  });
});

["block-id-source", "block-id-target", "fk-source-column", "fk-target-column"].forEach((id) => {
  document.getElementById(id).addEventListener("change", () => {
    renderDnd();
    updateAddBlockState();
  });
});

// ---------------------------------------------------------------------------
// Drag & drop pemetaan kolom
// ---------------------------------------------------------------------------

function renderDnd() {
  const idSource = document.getElementById("block-id-source").value;
  const fkEnabled = document.getElementById("block-fk-enable").checked;
  const fkSourceCol = fkEnabled ? document.getElementById("fk-source-column").value : null;

  const sourceEl = document.getElementById("source-columns");
  sourceEl.innerHTML = "";
  state.oldColumns
    .filter((c) => c.name !== idSource && c.name !== fkSourceCol)
    .forEach((c) => {
      const used = Object.values(state.pairs).includes(c.name);
      const chip = document.createElement("div");
      chip.className = "chip" + (used ? " used" : "");
      chip.draggable = true;
      chip.title = c.type;
      chip.innerHTML = `<span class="chip-name">${c.name}</span><span class="chip-type">${c.type}</span>`;
      chip.addEventListener("dragstart", (ev) => {
        ev.dataTransfer.setData("text/plain", c.name);
      });
      sourceEl.appendChild(chip);
    });

  const idTarget = document.getElementById("block-id-target").value;
  const fkTargetCol = fkEnabled ? document.getElementById("fk-target-column").value : null;

  const targetEl = document.getElementById("target-columns");
  targetEl.innerHTML = "";
  state.newColumns
    .filter((c) => c.name !== idTarget && c.name !== fkTargetCol)
    .forEach((c) => {
      const paired = state.pairs[c.name];
      const slot = document.createElement("div");
      slot.className = "drop-slot" + (paired ? " filled" : "");
      slot.innerHTML = `
        <span class="slot-name">${c.name}</span>
        <span class="slot-type">${c.type}</span>
        <span class="slot-pair">${paired ? "← " + paired : "seret kolom ke sini"}</span>
        ${paired ? '<button type="button" class="chip-remove">✕</button>' : ""}
      `;
      slot.addEventListener("dragover", (ev) => {
        ev.preventDefault();
        slot.classList.add("dragover");
      });
      slot.addEventListener("dragleave", () => slot.classList.remove("dragover"));
      slot.addEventListener("drop", (ev) => {
        ev.preventDefault();
        slot.classList.remove("dragover");
        const oldCol = ev.dataTransfer.getData("text/plain");
        if (!oldCol) return;

        for (const [newCol, mappedOld] of Object.entries(state.pairs)) {
          if (mappedOld === oldCol) delete state.pairs[newCol];
        }
        state.pairs[c.name] = oldCol;
        renderDnd();
        updateAddBlockState();
      });

      const removeBtn = slot.querySelector(".chip-remove");
      if (removeBtn) {
        removeBtn.addEventListener("click", () => {
          delete state.pairs[c.name];
          renderDnd();
          updateAddBlockState();
        });
      }
      targetEl.appendChild(slot);
    });
}

// ---------------------------------------------------------------------------
// Tambah / update blok
// ---------------------------------------------------------------------------

function validateBlockForm() {
  const sourceTable = document.getElementById("block-source-table").value;
  const targetTable = document.getElementById("block-target-table").value;
  const idSource = document.getElementById("block-id-source").value;
  const idTarget = document.getElementById("block-id-target").value;
  const fkEnabled = document.getElementById("block-fk-enable").checked;

  if (!sourceTable || !targetTable) return "Pilih tabel sumber & tujuan dulu";
  if (!idSource || !idTarget) return "Pilih kolom ID sumber & tujuan";
  if (fkEnabled) {
    const fkOk =
      document.getElementById("fk-source-column").value &&
      document.getElementById("fk-target-column").value &&
      document.getElementById("fk-ref-table").value &&
      document.getElementById("fk-ref-column").value;
    if (!fkOk) return "Lengkapi field foreign key, atau matikan opsinya";
  }
  if (Object.keys(state.pairs).length === 0) return "Pasangkan minimal 1 kolom lewat drag & drop";
  return null;
}

function updateAddBlockState() {
  const reason = state.currentMapping ? validateBlockForm() : "Buat/pilih mapping dulu";
  document.getElementById("btn-add-block").disabled = !!reason;
  document.getElementById("block-hint").textContent = reason || "";
}

document.getElementById("btn-add-block").addEventListener("click", () => {
  const reason = state.currentMapping ? validateBlockForm() : "Buat/pilih mapping dulu";
  if (reason) {
    toast(reason, "error");
    return;
  }

  const sourceTable = document.getElementById("block-source-table").value;
  const targetTable = document.getElementById("block-target-table").value;
  const idSource = document.getElementById("block-id-source").value;
  const idTarget = document.getElementById("block-id-target").value;
  const idMode = getIdMode();
  const upsert = document.getElementById("block-upsert").checked;
  const fkEnabled = document.getElementById("block-fk-enable").checked;

  const columns = {};
  for (const [newCol, oldCol] of Object.entries(state.pairs)) columns[oldCol] = newCol;

  const block = {
    source_table: sourceTable,
    target_table: targetTable,
    id_source: idSource,
    id_target: idTarget,
    id_mode: idMode,
    upsert: idMode === "preserve" ? upsert : false,
    dedup_key: idMode === "preserve_secondary" ? idTarget : null,
    fk: fkEnabled
      ? {
          source_column: document.getElementById("fk-source-column").value,
          target_column: document.getElementById("fk-target-column").value,
          ref_source_table: document.getElementById("fk-ref-table").value,
          ref_source_column: document.getElementById("fk-ref-column").value,
        }
      : null,
    columns,
  };

  if (state.editingBlockIndex !== null) {
    state.currentMapping.blocks[state.editingBlockIndex] = block;
    state.editingBlockIndex = null;
  } else {
    state.currentMapping.blocks.push(block);
  }

  renderBlocksTable();
  toast('Blok ditambahkan. Klik "Simpan Mapping" untuk menuliskannya ke file.', "success");
});

function renderBlocksTable() {
  const tbody = document.querySelector("#blocks-table tbody");
  tbody.innerHTML = "";
  (state.currentMapping?.blocks || []).forEach((b, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${i + 1}</td>
      <td>${b.source_table}</td>
      <td>${b.target_table}</td>
      <td>${b.id_mode}</td>
      <td>${Object.keys(b.columns).length} kolom</td>
      <td>${b.fk ? `${b.fk.source_column} → ${b.fk.target_column}` : "-"}</td>
      <td>
        <button data-i="${i}" class="btn-edit-block">Edit</button>
        <button data-i="${i}" class="btn-remove-block danger">Hapus</button>
      </td>
    `;
    tbody.appendChild(tr);
  });

  tbody.querySelectorAll(".btn-edit-block").forEach((btn) =>
    btn.addEventListener("click", () => editBlock(+btn.dataset.i))
  );
  tbody.querySelectorAll(".btn-remove-block").forEach((btn) =>
    btn.addEventListener("click", () => removeBlock(+btn.dataset.i))
  );
}

function removeBlock(i) {
  state.currentMapping.blocks.splice(i, 1);
  renderBlocksTable();
}

async function editBlock(i) {
  const b = state.currentMapping.blocks[i];
  state.editingBlockIndex = i;

  document.getElementById("block-source-table").value = b.source_table;
  await onSourceTableChange();
  document.getElementById("block-target-table").value = b.target_table;
  await onTargetTableChange();

  document.getElementById("block-id-source").value = b.id_source;
  document.getElementById("block-id-target").value = b.id_target;
  setIdMode(b.id_mode);
  document.getElementById("block-upsert").checked = !!b.upsert;
  document.getElementById("block-upsert").disabled = b.id_mode === "preserve_secondary";

  document.getElementById("block-fk-enable").checked = !!b.fk;
  document.getElementById("fk-fields").hidden = !b.fk;
  if (b.fk) {
    document.getElementById("fk-source-column").value = b.fk.source_column;
    document.getElementById("fk-target-column").value = b.fk.target_column;
    document.getElementById("fk-ref-table").value = b.fk.ref_source_table;
    const cols = await api(`/api/columns/old/${encodeURIComponent(b.fk.ref_source_table)}`);
    fillSelect("fk-ref-column", cols.map((c) => c.name));
    document.getElementById("fk-ref-column").value = b.fk.ref_source_column;
  }

  state.pairs = {};
  for (const [oldCol, newCol] of Object.entries(b.columns)) state.pairs[newCol] = oldCol;
  renderDnd();
  updateAddBlockState();

  toast(`Mengedit blok #${i + 1} — ubah lalu klik "Tambah/Update Blok"`, "info");
}

// ---------------------------------------------------------------------------
// Mapping select / new / save / delete
// ---------------------------------------------------------------------------

async function loadMappingsList() {
  const list = await api("/api/mappings");
  const sel = document.getElementById("mapping-select");
  sel.innerHTML = '<option value="">-- Mapping baru --</option>';
  list.forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m.name;
    opt.textContent = `${m.name} (${m.blocks} blok)`;
    sel.appendChild(opt);
  });
  renderSidebarMappings(list);
  return list;
}

document.getElementById("mapping-select").addEventListener("change", async (e) => {
  const name = e.target.value;
  if (!name) {
    startNewMapping();
    return;
  }
  const mapping = await api(`/api/mappings/${encodeURIComponent(name)}`);
  state.currentMapping = mapping;
  state.editingBlockIndex = null;
  document.getElementById("mapping-name").value = mapping.name;
  document.getElementById("mapping-desc").value = mapping.description || "";
  document.getElementById("mapping-name").disabled = true;
  renderBlocksTable();
  updateAddBlockState();
});

function startNewMapping() {
  state.currentMapping = { name: "", description: "", blocks: [] };
  state.editingBlockIndex = null;
  document.getElementById("mapping-name").value = "";
  document.getElementById("mapping-desc").value = "";
  document.getElementById("mapping-name").disabled = false;
  renderBlocksTable();
  updateAddBlockState();
}

document.getElementById("btn-new-mapping").addEventListener("click", () => {
  document.getElementById("mapping-select").value = "";
  startNewMapping();
});

document.getElementById("btn-save-mapping").addEventListener("click", async () => {
  const name = document.getElementById("mapping-name").value.trim();
  if (!name) {
    toast("Isi nama mapping dulu", "error");
    return;
  }
  if (!state.currentMapping || state.currentMapping.blocks.length === 0) {
    toast("Tambahkan minimal 1 blok dulu", "error");
    return;
  }
  state.currentMapping.name = name;
  state.currentMapping.description = document.getElementById("mapping-desc").value;

  try {
    await api(`/api/mappings/${encodeURIComponent(name)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.currentMapping),
    });
    toast("Mapping tersimpan", "success");
    await loadMappingsList();
    document.getElementById("mapping-select").value = name;
    document.getElementById("mapping-name").disabled = true;
    await loadDashboard();
  } catch (err) {
    toast("Gagal simpan: " + err.message, "error");
  }
});

document.getElementById("btn-delete-mapping").addEventListener("click", async () => {
  const name = document.getElementById("mapping-name").value.trim();
  if (!name) return;
  if (!confirm(`Hapus mapping "${name}"? Tabel terkait akan kembali pakai migrasi generik 1:1.`)) return;

  await api(`/api/mappings/${encodeURIComponent(name)}`, { method: "DELETE" });
  toast("Mapping dihapus", "success");
  await loadMappingsList();
  startNewMapping();
  await loadDashboard();
});

// ---------------------------------------------------------------------------
// Jalankan & pantau migrasi
// ---------------------------------------------------------------------------

function renderRunTableList() {
  const el = document.getElementById("run-table-list");
  el.innerHTML = "";
  if (state.tables.old.length === 0) {
    el.innerHTML = '<div class="obj-list-empty">Tidak ada tabel</div>';
    return;
  }
  state.tables.old.forEach((t) => {
    const row = document.createElement("label");
    row.className = "obj-row";
    row.innerHTML = `
      <input type="checkbox" value="${t.name}" ${state.selectedRunTables.has(t.name) ? "checked" : ""}>
      <span class="obj-icon">📄</span>
      <span class="obj-name">${t.name}</span>
      <span class="obj-meta">~${formatCount(t.row_estimate)} baris</span>
      <span class="obj-badge-slot">${t.mapped ? `<span class="badge mapped">${t.mapping_name}.json</span>` : '<span class="badge">generik 1:1</span>'}</span>
    `;
    const cb = row.querySelector("input");
    cb.addEventListener("change", () => {
      if (cb.checked) state.selectedRunTables.add(t.name);
      else state.selectedRunTables.delete(t.name);
    });
    el.appendChild(row);
  });
}

document.getElementById("btn-select-all").addEventListener("click", () => {
  state.tables.old.forEach((t) => state.selectedRunTables.add(t.name));
  renderRunTableList();
});
document.getElementById("btn-select-none").addEventListener("click", () => {
  state.selectedRunTables.clear();
  renderRunTableList();
});

document.getElementById("btn-run").addEventListener("click", async () => {
  const tables = Array.from(state.selectedRunTables);
  if (tables.length === 0) {
    toast("Pilih minimal satu tabel", "error");
    return;
  }
  if (!confirm(`Yakin jalankan migrasi untuk ${tables.length} tabel?\n${tables.join(", ")}\n\nIni akan menulis data ke NEW_DB.`)) {
    return;
  }

  document.getElementById("log-panel").textContent = "";
  setRunStatus("RUNNING");

  try {
    const { job_id } = await api("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tables }),
    });
    state.currentJobId = job_id;
    streamJob(job_id);
  } catch (err) {
    toast("Gagal memulai migrasi: " + err.message, "error");
    setRunStatus("FAILED");
  }
});

function setRunStatus(status) {
  const el = document.getElementById("run-status");
  el.className = "status-badge " + status.toLowerCase();
  const labels = {
    RUNNING: "⏳ Sedang berjalan...",
    SUCCESS: "✔ Selesai",
    FAILED: "✘ Gagal",
    idle: "Belum dijalankan",
  };
  el.textContent = labels[status] || status;
  setStatus(undefined, `Job ${state.currentJobId || ""}: ${status}`);
}

function appendLog(entry) {
  const panel = document.getElementById("log-panel");
  const line = document.createElement("div");
  line.className = "log-line log-" + entry.level;
  line.textContent = entry.text;
  panel.appendChild(line);
  panel.scrollTop = panel.scrollHeight;
}

function streamJob(jobId) {
  if (state.eventSource) state.eventSource.close();
  const es = new EventSource(`/api/run/${jobId}/stream`);
  state.eventSource = es;

  es.onmessage = (ev) => appendLog(JSON.parse(ev.data));
  es.addEventListener("done", (ev) => {
    const data = JSON.parse(ev.data);
    setRunStatus(data.status);
    es.close();
    refreshJobsList();
  });
  es.onerror = () => es.close();
}

async function refreshJobsList() {
  const jobs = await api("/api/jobs");
  const tbody = document.querySelector("#jobs-table tbody");
  tbody.innerHTML = "";
  jobs.forEach((j) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${j.id}</td>
      <td>${j.tables.join(", ")}</td>
      <td><span class="status-badge ${j.status.toLowerCase()}">${j.status}</span></td>
      <td>${formatTime(j.started_at)}</td>
      <td>${j.finished_at ? formatTime(j.finished_at) : "-"}</td>
      <td><button data-id="${j.id}" class="btn-view-job">Lihat Log</button></td>
    `;
    tbody.appendChild(tr);
  });
  tbody.querySelectorAll(".btn-view-job").forEach((btn) =>
    btn.addEventListener("click", () => viewJobLogs(btn.dataset.id))
  );
}

async function viewJobLogs(jobId) {
  const logs = await api(`/api/run/${jobId}/logs`);
  document.getElementById("log-panel").textContent = "";
  logs.forEach(appendLog);
  const status = await api(`/api/run/${jobId}`);
  setRunStatus(status.status);
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

(async function init() {
  try {
    await refreshAll();
    startNewMapping();
    await refreshJobsList();
  } catch (err) {
    setStatus("Gagal memuat data.");
    toast("Gagal memuat data awal: " + err.message, "error");
  }
})();
