/* Safety Center and release-readiness workflows.
 * Kept in its own feature module so the legacy editor bundle does not keep
 * growing while the frontend is progressively decomposed.
 */
(function () {
  "use strict";

  const h = value => {
    const node = document.createElement("div");
    node.textContent = value == null ? "" : String(value);
    return node.innerHTML;
  };
  const byId = id => document.getElementById(id);
  let latestHistory = null;
  let previewedSource = null;

  function checklistRow(step) {
    return `<div class="safety-check ${step.done ? "done" : ""}">
      <span class="safety-check-icon">${step.done ? "✓" : "○"}</span>
      <span>${h(step.label || step.name)}</span>
    </div>`;
  }

  function metric(value, label, tone = "") {
    return `<div class="safety-metric ${tone}"><strong>${h(value)}</strong><span>${h(label)}</span></div>`;
  }

  async function loadOverview() {
    const data = await api("/api/safety/overview");
    byId("setup-progress").textContent = `${data.complete}/${data.total} complete`;
    byId("setup-progress").className = `dash-pill ${data.complete === data.total ? "ok" : "warn"}`;
    byId("setup-checklist").innerHTML = `<div class="safety-checklist">${data.steps.map(checklistRow).join("")}</div>
      <div class="safety-metrics">${metric(data.stats.sources, "Sources")}${metric(data.stats.groups, "Groups")}${metric(data.stats.channels, "Stations")}${metric(data.stats.locked, "Locked")}</div>`;
  }

  async function loadHistory() {
    latestHistory = await api("/api/history?limit=50");
    const list = byId("snapshot-list");
    if (!latestHistory.snapshots.length) {
      list.innerHTML = '<p class="text-muted">No lineup snapshots yet.</p>';
    } else {
      list.innerHTML = latestHistory.snapshots.map(s => `
        <div class="safety-list-row">
          <div><strong>${h(s.label)}</strong><div class="text-muted">${h(s.kind)} · ${h(s.created_at)} · ${Number(s.channel_count || 0).toLocaleString()} stations</div></div>
          <div class="btn-row">
            <button class="btn btn-sm snapshot-diff" data-id="${h(s.id)}">Compare</button>
            <button class="btn btn-sm snapshot-restore" data-id="${h(s.id)}" data-label="${h(s.label)}">Restore</button>
            ${s.kind === "manual" ? `<button class="btn btn-sm btn-ghost snapshot-delete" data-id="${h(s.id)}">Delete</button>` : ""}
          </div>
        </div>`).join("");
    }
    byId("action-history-list").innerHTML = latestHistory.actions.length
      ? latestHistory.actions.slice(0, 30).map(action => `<div class="safety-list-row"><div><strong>${h(action.action)}</strong><div class="text-muted">${h(action.created_at)} · ${h(action.status)}${action.snapshot_label ? ` · undo: ${h(action.snapshot_label)}` : ""}</div></div></div>`).join("")
      : '<p class="text-muted">No high-impact actions recorded yet.</p>';
  }

  async function loadSourcesForPreview() {
    const sources = await api("/api/sources");
    const rows = Array.isArray(sources) ? sources : (sources.sources || []);
    byId("preview-source").innerHTML = rows.map(source =>
      `<option value="${source.id}">${h(source.name)}${source.is_active ? "" : " (inactive)"}</option>`
    ).join("") || '<option value="">No sources configured</option>';
  }

  function renderCompatibility(data) {
    byId("teamarr-compatibility").innerHTML = `
      <div class="safety-status ${data.status === "ready" ? "ok" : "warn"}">${data.status === "ready" ? "Ready for live-TV clients" : "Setup needs attention"}</div>
      <div class="safety-checklist">${data.checks.map(checklistRow).join("")}</div>
      <p class="text-muted">Supported: ${data.contract.actions.map(h).join(", ")}. VOD, series, and catch-up intentionally return empty catalogs.</p>`;
  }

  async function loadSystemChecks() {
    const [teamarr, migrations] = await Promise.all([
      api("/api/teamarr/compatibility"), api("/api/system/migrations"),
    ]);
    renderCompatibility(teamarr);
    byId("migration-health").innerHTML = migrations.healthy
      ? `<div class="safety-status ok">Schema ${h(migrations.schema_version)} is current</div><p class="text-muted">Required history and stable-ID tables are present.</p>`
      : `<div class="safety-status bad">Migration incomplete</div><p class="text-muted">Missing: ${migrations.missing_tables.map(h).join(", ")}</p>`;
  }

  async function loadSafetyCenter() {
    const tasks = [loadOverview(), loadHistory(), loadSourcesForPreview(), loadSystemChecks()];
    const results = await Promise.allSettled(tasks);
    const failure = results.find(result => result.status === "rejected");
    if (failure) toast("error", "Safety Center partially loaded", failure.reason?.message || "Unknown error");
  }

  byId("safety-refresh")?.addEventListener("click", () => loadSafetyCenter());

  byId("snapshot-create")?.addEventListener("click", async () => {
    const label = byId("snapshot-label").value.trim() || `Named snapshot ${new Date().toLocaleString()}`;
    try {
      await api("/api/history/snapshots", {method: "POST", body: JSON.stringify({label})});
      byId("snapshot-label").value = "";
      toast("success", "Snapshot saved", label);
      await Promise.all([loadHistory(), loadOverview()]);
    } catch (error) { toast("error", "Snapshot failed", error.message); }
  });

  byId("history-undo")?.addEventListener("click", async () => {
    if (!await confirmDialog("Undo Last High-Impact Change", "Restore the lineup state captured immediately before the latest reorder, import, rule application, deletion, or bulk edit?", "Undo Change")) return;
    try {
      const result = await api("/api/history/undo", {method: "POST"});
      toast("success", "Change undone", `Restored ${result.restored.label}`);
      await loadSafetyCenter();
    } catch (error) { toast("error", "Undo unavailable", error.message); }
  });

  byId("snapshot-list")?.addEventListener("click", async event => {
    const button = event.target.closest("button");
    if (!button) return;
    const id = button.dataset.id;
    try {
      if (button.classList.contains("snapshot-diff")) {
        const diff = await api(`/api/history/snapshots/${id}/diff`);
        const c = diff.channels, g = diff.groups;
        await confirmDialog("Snapshot Comparison", `${c.added} stations added · ${c.removed} removed · ${c.moved} moved · ${c.changed} edited\n${g.added} groups added · ${g.removed} removed`, "Close");
      } else if (button.classList.contains("snapshot-restore")) {
        if (!await confirmDialog("Restore Lineup Snapshot", `Restore “${button.dataset.label}”? A new undo point will be created first. Sources and credentials will not change.`, "Restore")) return;
        await api(`/api/history/snapshots/${id}/restore`, {method: "POST"});
        toast("success", "Snapshot restored", button.dataset.label);
        await loadSafetyCenter();
      } else if (button.classList.contains("snapshot-delete")) {
        if (!await confirmDialog("Delete Snapshot", "Delete this named snapshot?", "Delete")) return;
        await api(`/api/history/snapshots/${id}`, {method: "DELETE"});
        await loadHistory();
      }
    } catch (error) { toast("error", "History action failed", error.message); }
  });

  byId("preview-source-run")?.addEventListener("click", async () => {
    const sourceId = byId("preview-source").value;
    if (!sourceId) return;
    const result = byId("import-preview-result");
    result.innerHTML = '<span class="spinner"></span> Fetching source without applying changes…';
    try {
      const data = await api(`/api/sources/${sourceId}/preview-refresh`, {timeout: 1200000});
      previewedSource = sourceId;
      result.innerHTML = `<div class="safety-metrics">${metric(data.added, "Added", "ok")}${metric(data.changed, "Changed", "warn")}${metric(data.removed, "Removed", data.removed ? "bad" : "")}${metric(data.locked_removals, "Locked removals", data.locked_removals ? "bad" : "")}</div>
        <p class="text-muted">${data.requires_review ? "Review removals before applying." : "No destructive changes detected."}</p>
        ${data.sample.removed.length ? `<details><summary>Removed station sample</summary><ul>${data.sample.removed.map(row => `<li>${h(row.name)} — ${h(row.group)}${row.locked ? " · LOCKED" : ""}</li>`).join("")}</ul></details>` : ""}
        <button id="preview-apply-refresh" class="btn btn-primary btn-sm">Apply Reviewed Refresh</button>`;
    } catch (error) { result.innerHTML = `<span class="text-danger">${h(error.message)}</span>`; }
  });

  byId("import-preview-result")?.addEventListener("click", async event => {
    if (event.target.id !== "preview-apply-refresh" || !previewedSource) return;
    if (!await confirmDialog("Apply Reviewed Refresh", "Apply the previewed provider refresh? An automatic undo point will be captured first.", "Refresh Source")) return;
    try {
      const data = await api(`/api/sources/${previewedSource}/refresh`, {method: "POST"});
      toast("success", "Source refreshed", `+${data.added} added · -${data.removed} removed · ${data.revived || 0} revived`);
      await loadSafetyCenter();
    } catch (error) { toast("error", "Refresh failed", error.message); }
  });

  byId("guide-quality-run")?.addEventListener("click", async () => {
    const target = byId("guide-quality-result");
    target.innerHTML = '<span class="spinner"></span> Scanning XMLTV…';
    try {
      const data = await api("/api/epg/quality", {timeout: 120000});
      if (data.status !== "ok") { target.innerHTML = `<p class="text-muted">${h(data.summary)}</p>`; return; }
      target.innerHTML = `<div class="safety-metrics">${metric(data.channels, "Channels")}${metric(data.empty_channels, "Empty", data.empty_channels ? "bad" : "ok")}${metric(data.generic_title_channels, "Generic titles", data.generic_title_channels ? "warn" : "ok")}${metric(data.blank_title_channels, "Blank titles", data.blank_title_channels ? "bad" : "ok")}</div>
        ${data.sample.generic.length ? `<details><summary>Generic listing sample</summary><ul>${data.sample.generic.slice(0, 10).map(row => `<li>${h(row.name)} — ${row.rows} rows</li>`).join("")}</ul></details>` : ""}`;
    } catch (error) { target.innerHTML = `<span class="text-danger">${h(error.message)}</span>`; }
  });

  byId("backup-validate-file")?.addEventListener("change", async event => {
    const target = byId("backup-validation-result");
    try {
      const data = JSON.parse(await event.target.files[0].text());
      const result = await api("/api/backup/validate", {method: "POST", body: JSON.stringify({backup: data})});
      target.innerHTML = `<div class="safety-status ${result.valid ? "ok" : "bad"}">${result.valid ? "Backup is structurally valid" : "Backup cannot be safely restored"}</div>
        <p class="text-muted">${result.counts.sources} sources · ${result.counts.groups} groups · ${result.counts.channels.toLocaleString()} stations · version ${h(result.version)}</p>
        ${result.errors.map(error => `<div class="text-danger">• ${h(error)}</div>`).join("")}${result.warnings.map(warning => `<div class="text-warning">• ${h(warning)}</div>`).join("")}`;
    } catch (error) { target.innerHTML = `<span class="text-danger">Invalid JSON: ${h(error.message)}</span>`; }
    event.target.value = "";
  });

  window.M3UBossFeatures = {loadSafetyCenter};
  if (location.hash === "#safety") loadSafetyCenter();
})();
