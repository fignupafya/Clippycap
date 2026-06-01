<script lang="ts">
  // The "Linked files" panel shown on a clip's detail view: the companion files attached by linkers
  // (demos, scripts, ...). Reveal in the file manager, open with the OS default, or open with a
  // linker-configured program. Files are never opened automatically -- only on an explicit click.
  import { api, type Attachment, type Linker } from '../api';

  interface Props { assetId: number; }
  let { assetId }: Props = $props();

  let attachments = $state<Attachment[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);
  // linker_id -> its configured open-with actions (name + which extensions they apply to)
  let actionsByLinker = $state<Map<number, { name: string; extensions: string[] }[]>>(new Map());

  async function load() {
    loading = true; error = null;
    try {
      const [atts, linkers] = await Promise.all([api.assetAttachments(assetId), api.listLinkers()]);
      attachments = atts;
      actionsByLinker = buildActions(linkers);
    } catch (e) { error = String(e); }
    finally { loading = false; }
  }

  function buildActions(linkers: Linker[]): Map<number, { name: string; extensions: string[] }[]> {
    const m = new Map<number, { name: string; extensions: string[] }[]>();
    for (const lk of linkers) {
      try {
        const def = JSON.parse(lk.definition_json);
        const acts = (def?.actions?.open_with ?? []) as { name: string; extensions: string[] }[];
        if (acts.length) m.set(lk.id, acts);
      } catch { /* ignore a malformed definition */ }
    }
    return m;
  }

  function actionsFor(a: Attachment): { name: string }[] {
    const acts = actionsByLinker.get(a.linker_id) ?? [];
    return acts.filter((x) => x.extensions.length === 0 || x.extensions.includes(a.ext));
  }

  // reload whenever the clip changes
  $effect(() => { void assetId; void load(); });

  async function reveal(a: Attachment) { try { await api.revealAttachment(a.id); } catch (e) { error = String(e); } }
  async function open(a: Attachment) { try { await api.openAttachment(a.id); } catch (e) { error = String(e); } }
  async function openWith(a: Attachment, action: string) { try { await api.openAttachmentWith(a.id, action); } catch (e) { error = String(e); } }
  let working = $state(false);
  // mark an auto match wrong (the override sticks across every future re-run)
  async function exclude(a: Attachment) {
    working = true;
    try { await api.setOverride(assetId, { linker_id: a.linker_id, path: a.path, decision: 'exclude' }); await load(); }
    catch (e) { error = String(e); } finally { working = false; }
  }
  // remove a manual pin (let the rule decide again)
  async function unpin(a: Attachment) {
    working = true;
    try { await api.clearOverride(assetId, { linker_id: a.linker_id, path: a.path }); await load(); }
    catch (e) { error = String(e); } finally { working = false; }
  }
</script>

<h4>Linked files</h4>
{#if loading}
  <span class="faint">loading…</span>
{:else if error}
  <div class="lf-err">{error}</div>
{:else if attachments.length === 0}
  <span class="faint">none — set up a linker to auto-attach demos, scripts, transcripts… (Tags → Linkers).</span>
{:else}
  <div class="lf-list">
    {#each attachments as a (a.id)}
      <div class="lf-row" class:missing={a.status === 'missing'}>
        <div class="lf-info" title={a.path}>
          <span class="lf-icon">{a.status === 'missing' ? '⚠' : (a.origin === 'manual' ? '📌' : '📎')}</span>
          <span class="lf-name">{a.label}</span>
          {#if a.status === 'missing'}<span class="lf-tag">missing</span>{/if}
        </div>
        <div class="lf-actions">
          <button class="btn xs" onclick={() => reveal(a)} title="show this file in the file manager">Reveal</button>
          <button class="btn xs" onclick={() => open(a)} disabled={a.status === 'missing'} title="open with the default program">Open</button>
          {#each actionsFor(a) as act (act.name)}
            <button class="btn xs" onclick={() => openWith(a, act.name)} disabled={a.status === 'missing'} title={`open with ${act.name}`}>{act.name}</button>
          {/each}
          {#if a.origin === 'manual'}
            <button class="btn xs" onclick={() => unpin(a)} disabled={working} title="remove this manual pin">Unpin</button>
          {:else}
            <button class="btn xs warn" onclick={() => exclude(a)} disabled={working} title="wrong match — don't link this file (sticks across re-runs)">✕</button>
          {/if}
        </div>
      </div>
    {/each}
  </div>
{/if}

<style>
  .lf-list { display: flex; flex-direction: column; gap: 5px; }
  .lf-row { display: flex; align-items: center; gap: 8px; padding: 6px 8px; background: var(--bg-2, #1a1d24);
            border: 1px solid var(--border, #2a2e38); border-radius: 7px; }
  .lf-row.missing { opacity: .7; border-style: dashed; }
  .lf-info { display: flex; align-items: center; gap: 6px; flex: 1; min-width: 0; }
  .lf-icon { flex: none; }
  .lf-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
  .lf-tag { font-size: 10px; color: var(--amber, #f59e0b); border: 1px solid currentColor; border-radius: 4px; padding: 0 4px; flex: none; }
  .lf-actions { display: flex; gap: 4px; flex: none; }
  .lf-err { color: var(--red, #ef4444); font-size: 12px; }
  .btn.xs { font-size: 11px; padding: 2px 7px; }
  .btn.xs.warn:hover { background: var(--red, #ef4444); color: #fff; }
</style>
