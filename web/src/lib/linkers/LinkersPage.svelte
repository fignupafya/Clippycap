<script lang="ts">
  // The Linkers management page: list every linker with its enable toggle, run / clone / delete, and
  // a "New linker" gallery of starter presets. Editing or creating opens the LinkerBuilder inline.
  import { api, type Linker, type LinkerPreset } from '../api';
  import LinkerBuilder from './LinkerBuilder.svelte';
  import { parseDefinition, type Definition } from './defs';

  interface Props { onClose: () => void; }
  let { onClose }: Props = $props();

  let linkers = $state<Linker[]>([]);
  let presets = $state<LinkerPreset[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let mode = $state<'list' | 'gallery' | 'edit'>('list');
  let editing = $state<Linker | null>(null);
  let initial = $state<{ name: string; color: string; definition: Definition } | undefined>(undefined);
  let busy = $state<number | null>(null);

  async function load() {
    loading = true; error = null;
    try {
      [linkers, presets] = await Promise.all([api.listLinkers(), api.getLinkerPresets()]);
    } catch (e) { error = String(e); }
    finally { loading = false; }
  }
  $effect(() => { void load(); });

  async function toggle(lk: Linker) {
    busy = lk.id;
    try { await api.setLinkerEnabled(lk.id, !lk.enabled); await load(); }
    catch (e) { error = String(e); }
    finally { busy = null; }
  }
  async function run(lk: Linker) { busy = lk.id; try { await api.runLinker(lk.id); } catch (e) { error = String(e); } finally { busy = null; } }
  async function clone(lk: Linker) { try { await api.cloneLinker(lk.id); await load(); } catch (e) { error = String(e); } }
  async function remove(lk: Linker) {
    if (!confirm(`Delete linker "${lk.name}"? Its auto-attached links go too (pinned files are kept).`)) return;
    try { await api.deleteLinker(lk.id); await load(); } catch (e) { error = String(e); }
  }
  function editLinker(lk: Linker) { editing = lk; initial = undefined; mode = 'edit'; }
  function newBlank() { editing = null; initial = undefined; mode = 'edit'; }
  function fromPreset(p: LinkerPreset) {
    editing = null;
    initial = { name: p.name, color: p.color, definition: parseDefinition(p.definition_json) };
    mode = 'edit';
  }
  function onSaved() { mode = 'list'; void load(); }
  function onCancel() { mode = 'list'; }

  // ---- import / export shareable recipes ----
  let showImport = $state(false);
  let importText = $state('');
  let importError = $state<string | null>(null);

  function exportLinker(lk: Linker) {
    const recipe = { name: lk.name, description: lk.description, color: lk.color, definition: JSON.parse(lk.definition_json) };
    const blob = new Blob([JSON.stringify(recipe, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `${lk.name.replace(/[^\w.-]+/g, '_')}.linker.json`;
    a.click(); URL.revokeObjectURL(url);
  }
  async function doImport() {
    importError = null;
    try {
      const r = JSON.parse(importText) as { name?: string; description?: string; color?: string; definition?: unknown; definition_json?: string };
      const definition_json = r.definition_json ?? JSON.stringify(r.definition ?? {});
      await api.createLinker({ name: r.name ?? 'Imported linker', definition_json, description: r.description ?? '', color: r.color ?? '' });
      showImport = false; importText = ''; await load();
    } catch (e) { importError = String(e); }
  }
</script>

<div class="lp-wrap">
  <div class="lp-top">
    <button class="btn sm" onclick={() => (mode === 'list' ? onClose() : (mode = 'list'))}>← {mode === 'list' ? 'Library' : 'Linkers'}</button>
    <h2>{mode === 'edit' ? (editing ? 'Edit linker' : 'New linker') : mode === 'gallery' ? 'Choose a starting point' : 'Linkers'}</h2>
    <span style:flex="1"></span>
    {#if mode === 'list'}
      <button class="btn sm" onclick={() => { importText = ''; importError = null; showImport = true; }}>Import</button>
      <button class="btn sm" onclick={() => (mode = 'gallery')}>+ New from preset</button>
      <button class="btn sm primary" onclick={newBlank}>+ Blank linker</button>
    {/if}
  </div>

  {#if error}<div class="lp-err">{error}</div>{/if}

  {#if mode === 'edit'}
    <div class="lp-builder">
      <LinkerBuilder linker={editing} {initial} {onSaved} {onCancel} />
    </div>
  {:else if mode === 'gallery'}
    <div class="lp-gallery">
      {#each presets as p (p.key)}
        <button class="lp-preset" style:--c={p.color} onclick={() => fromPreset(p)}>
          <div class="lp-preset-h">{p.name}</div>
          <div class="lp-preset-d">{p.description}</div>
        </button>
      {/each}
      <button class="lp-preset blank" onclick={newBlank}>
        <div class="lp-preset-h">Start from scratch</div>
        <div class="lp-preset-d">Build a rule yourself from an empty template.</div>
      </button>
    </div>
  {:else}
    {#if loading}<div class="faint">loading…</div>{/if}
    {#if !loading && linkers.length === 0}
      <div class="lp-empty">No linkers yet. Create one to auto-attach demos, scripts, transcripts, RAWs… to your clips.</div>
    {/if}
    <div class="lp-list">
      {#each linkers as lk (lk.id)}
        <div class="lp-row" class:on={lk.enabled}>
          <button class="lp-toggle" class:on={lk.enabled} disabled={busy === lk.id} onclick={() => toggle(lk)} title={lk.enabled ? 'enabled — click to disable' : 'disabled — click to enable'}>
            <span class="knob"></span>
          </button>
          <span class="lp-dot" style:background={lk.color || 'var(--text-3)'}></span>
          <div class="lp-info">
            <div class="lp-name">{lk.name}</div>
            {#if lk.description}<div class="lp-desc">{lk.description}</div>{/if}
          </div>
          <div class="lp-acts">
            <button class="btn xs" onclick={() => run(lk)} disabled={busy === lk.id} title="run now">Run</button>
            <button class="btn xs" onclick={() => editLinker(lk)}>Edit</button>
            <button class="btn xs" onclick={() => clone(lk)}>Clone</button>
            <button class="btn xs" onclick={() => exportLinker(lk)} title="export this recipe as JSON">Export</button>
            <button class="btn xs danger" onclick={() => remove(lk)}>Delete</button>
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>

{#if showImport}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="modal-bg" onclick={(e) => { if (e.target === e.currentTarget) showImport = false; }}>
    <div class="modal">
      <div class="mtop"><h3>Import a linker recipe</h3><span style:flex="1"></span><button class="btn sm" onclick={() => (showImport = false)}>Close</button></div>
      <div class="mbody">
        <p class="faint" style:font-size="12px">Paste a recipe exported from another library (or this one).</p>
        <textarea class="field lp-import" bind:value={importText} placeholder={'{ "name": "...", "definition": { ... } }'} spellcheck="false"></textarea>
        {#if importError}<div class="lp-err">{importError}</div>{/if}
      </div>
      <div class="lp-mfoot"><button class="btn sm" onclick={() => (showImport = false)}>Cancel</button>
        <button class="btn sm primary" onclick={doImport} disabled={!importText.trim()}>Import</button></div>
    </div>
  </div>
{/if}

<style>
  .lp-wrap { display: flex; flex-direction: column; height: 100%; min-height: 0; padding: 14px 18px; gap: 12px; }
  .lp-top { display: flex; align-items: center; gap: 12px; }
  .lp-top h2 { margin: 0; font-size: 18px; }
  .lp-err { color: var(--red, #ef4444); font-size: 13px; }
  .lp-builder { flex: 1; min-height: 0; }
  .lp-list { display: flex; flex-direction: column; gap: 8px; overflow-y: auto; }
  .lp-row { display: flex; align-items: center; gap: 10px; padding: 10px 12px; background: var(--bg-1, #14171d); border: 1px solid var(--border, #2a2e38); border-radius: 10px; }
  .lp-row.on { border-color: color-mix(in srgb, var(--accent, #7c9cff) 50%, var(--border)); }
  .lp-toggle { width: 38px; height: 22px; border-radius: 20px; background: var(--bg-3, #2a2e38); border: none; position: relative; cursor: pointer; flex: none; transition: background .12s; }
  .lp-toggle.on { background: var(--accent, #7c9cff); }
  .lp-toggle .knob { position: absolute; top: 3px; left: 3px; width: 16px; height: 16px; border-radius: 50%; background: #fff; transition: left .12s; }
  .lp-toggle.on .knob { left: 19px; }
  .lp-dot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
  .lp-info { flex: 1; min-width: 0; }
  .lp-name { font-weight: 600; }
  .lp-desc { font-size: 12px; color: var(--text-3, #889); }
  .lp-acts { display: flex; gap: 5px; flex: none; }
  .lp-empty { color: var(--text-3, #889); padding: 24px; text-align: center; }
  .lp-gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; overflow-y: auto; }
  .lp-preset { text-align: left; padding: 14px; border-radius: 12px; background: var(--bg-1, #14171d); border: 1px solid var(--border, #2a2e38); border-left: 3px solid var(--c, var(--accent)); cursor: pointer; transition: border-color .12s, transform .08s; }
  .lp-preset:hover { transform: translateY(-1px); border-color: var(--c, var(--accent)); }
  .lp-preset.blank { border-left-color: var(--text-3, #889); }
  .lp-preset-h { font-weight: 700; margin-bottom: 6px; }
  .lp-preset-d { font-size: 12px; color: var(--text-3, #889); line-height: 1.4; }
  .btn.xs { font-size: 11px; padding: 2px 7px; }
  .btn.danger:hover { background: var(--red, #ef4444); color: #fff; }
  .lp-import { width: 100%; min-height: 220px; font-family: ui-monospace, monospace; font-size: 12px; }
  .lp-mfoot { display: flex; justify-content: flex-end; gap: 8px; padding: 10px 0 0; }
</style>
