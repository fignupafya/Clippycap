<script lang="ts">
  // The linker builder: a top-to-bottom, plain-language form over a Definition, with a live,
  // non-destructive preview docked alongside (LINKERS.md §9). Full power is reachable through the
  // visual controls; a raw-JSON escape hatch guarantees nothing is ever gated behind the form.
  import { api, type Linker, type PreviewResult } from '../api';
  import {
    type Condition, type Definition, type FieldDef, type Step,
    ATTRS, CONDITION_OPS, FIELD_TYPES, STEP_OPS, blankDefinition, commaList, parseCommaList, parseDefinition,
  } from './defs';

  interface Props {
    linker?: Linker | null;
    initial?: { name: string; color: string; definition: Definition };
    onSaved: () => void;
    onCancel: () => void;
  }
  let { linker = null, initial = undefined, onSaved, onCancel }: Props = $props();

  // The builder is mounted fresh for each create/edit, so we seed the form from the props once.
  // svelte-ignore state_referenced_locally
  const seedDef: Definition = linker ? parseDefinition(linker.definition_json) : (initial?.definition ?? blankDefinition());
  // svelte-ignore state_referenced_locally
  const seed = { name: linker?.name ?? initial?.name ?? 'New linker', description: linker?.description ?? '', color: linker?.color ?? initial?.color ?? '#7c9cff', enabled: linker?.enabled ?? false };

  let name = $state(seed.name);
  let description = $state(seed.description);
  let color = $state(seed.color);
  let enabled = $state(seed.enabled);
  let def = $state<Definition>(seedDef);
  let saving = $state(false);
  let saveError = $state<string | null>(null);
  let showRaw = $state(false);
  let rawText = $state('');
  let rawError = $state<string | null>(null);

  let preview = $state<PreviewResult | null>(null);
  let previewing = $state(false);
  let previewError = $state<string | null>(null);
  let previewTimer: ReturnType<typeof setTimeout> | null = null;

  const clipFieldNames = $derived(def.clip.fields.map((f) => f.name).filter(Boolean));
  const fileFieldNames = $derived(def.file.fields.map((f) => f.name).filter(Boolean));

  // Debounced live preview whenever the definition changes.
  $effect(() => {
    JSON.stringify(def);                       // track deep changes
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(runPreview, 400);
  });

  async function runPreview() {
    previewing = true; previewError = null;
    try { preview = await api.previewLinker(JSON.stringify(def)); }
    catch (e) { previewError = String(e); preview = null; }
    finally { previewing = false; }
  }

  // ---- field / step / condition / action mutations (Svelte 5 deep reactivity) ----
  function addField(side: 'clip' | 'file') {
    def[side].fields.push({ name: '', type: 'string', source: { kind: 'capture', name: '' }, steps: [] });
  }
  function removeField(side: 'clip' | 'file', i: number) { def[side].fields.splice(i, 1); }
  function addStep(field: FieldDef) { (field.steps ??= []).push({ op: 'trim' }); }
  function removeStep(field: FieldDef, i: number) { field.steps?.splice(i, 1); }
  function stepArg(op: string): string | undefined { return STEP_OPS.find((s) => s.value === op)?.arg; }

  function addCondition() {
    def.match.conditions.push({
      op: 'equals', left: { side: 'clip', field: clipFieldNames[0] ?? '' },
      right: { side: 'file', field: fileFieldNames[0] ?? '' }, tolerance: 10, slack: 10, threshold: 0.8,
    });
  }
  function removeCondition(i: number) { def.match.conditions.splice(i, 1); }
  function opNeeds(op: string, key: string): boolean {
    return CONDITION_OPS.find((o) => o.value === op)?.needs.includes(key) ?? false;
  }

  function addAction() { def.actions.open_with.push({ name: 'Open', extensions: [], program: '', args: ['%PATH%'] }); }
  function removeAction(i: number) { def.actions.open_with.splice(i, 1); }

  function addDirectory() { def.target.directories.push(''); }
  function removeDirectory(i: number) { def.target.directories.splice(i, 1); }

  // ---- simple resolution (3 questions) mapped onto the spec ----
  let perClip = $state<'any' | 'one'>(seedDef.resolve.per_clip_max === 1 ? 'one' : 'any');
  let sharedFiles = $state<'yes' | 'no'>(seedDef.resolve.per_file_max === 1 ? 'no' : 'yes');
  let competition = $state<'closest' | 'best' | 'all' | 'ask'>(
    seedDef.resolve.strategy === 'keep_all' ? 'all'
      : (seedDef.resolve.ambiguity_margin ?? 0) > 0 ? 'ask'
      : (seedDef.resolve.tiebreak ?? []).includes('nearest_time') ? 'closest' : 'best',
  );
  function applySimpleResolution() {
    def.resolve.per_clip_max = perClip === 'one' ? 1 : null;
    def.resolve.per_file_max = sharedFiles === 'no' ? 1 : null;
    if (competition === 'all') { def.resolve.strategy = 'keep_all'; def.resolve.ambiguity_margin = 0; }
    else if (sharedFiles === 'no') { def.resolve.strategy = 'best_overall'; def.resolve.ambiguity_margin = 0; }
    else { def.resolve.strategy = 'best_per_clip'; def.resolve.ambiguity_margin = competition === 'ask' ? 0.05 : 0; }
    def.resolve.tiebreak = competition === 'closest' ? ['nearest_time'] : ['score'];
  }
  $effect(applySimpleResolution);

  let showAdvanced = $state(false);

  function openRaw() { rawText = JSON.stringify(def, null, 2); rawError = null; showRaw = true; }
  function applyRaw() {
    try { def = parseDefinition(rawText); rawError = null; showRaw = false; }
    catch (e) { rawError = String(e); }
  }

  async function save() {
    if (saving) return;
    saving = true; saveError = null;
    const body = { name, definition_json: JSON.stringify(def), description, color, enabled };
    try {
      if (linker) await api.updateLinker(linker.id, body);
      else await api.createLinker(body);
      onSaved();
    } catch (e) { saveError = String(e); }
    finally { saving = false; }
  }
</script>

<div class="lb">
  <div class="lb-form">
    <div class="lb-head">
      <input class="field lb-name" bind:value={name} placeholder="Linker name" />
      <input class="lb-color" type="color" bind:value={color} title="colour" />
      <label class="lb-enable"><input type="checkbox" bind:checked={enabled} /> Enabled</label>
    </div>
    <input class="field lb-desc" bind:value={description} placeholder="What does this linker do? (optional)" />

    <section>
      <div class="lb-step-n">1 · Which clips do we attach files to?</div>
      <div class="row">
        <label>Media type <input class="field sm" bind:value={def.source.media_type} placeholder="video (blank = any)" /></label>
        <label>In folder <input class="field" bind:value={def.source.path_under} placeholder="any folder (optional)" /></label>
      </div>
    </section>

    <section>
      <div class="lb-step-n">2 · What files do we attach?</div>
      <div class="lb-dirs">
        {#each def.target.directories as _dir, i (i)}
          <div class="row"><input class="field" bind:value={def.target.directories[i]} placeholder="C:\path\to\folder" />
            <button class="btn sm" onclick={() => removeDirectory(i)}>×</button></div>
        {/each}
        <button class="btn sm" onclick={addDirectory}>+ Add a folder</button>
      </div>
      <div class="row">
        <label>File types
          <input class="field sm" value={commaList(def.target.extensions)}
                 oninput={(e) => (def.target.extensions = parseCommaList(e.currentTarget.value))}
                 placeholder="dem, docx, txt (blank = any)" /></label>
        <label class="chk"><input type="checkbox" bind:checked={def.target.recursive} /> include sub-folders</label>
      </div>
    </section>

    <section>
      <div class="lb-step-n">3 · How do we read each side?</div>
      {#each (['clip', 'file'] as const) as side (side)}
        <div class="lb-side">
          <div class="lb-side-h">{side === 'clip' ? 'From each clip' : 'From each file'}</div>
          <div class="row">
            <label>Read the name with
              <input class="field" bind:value={def[side].template} placeholder="e.g. Scene%n%_take%take%  (blank = no name parse)" /></label>
            <select class="field sm" bind:value={def[side].template_target}>
              <option value="stem">name (no ext)</option><option value="name">full name</option>
              <option value="path">full path</option><option value="folder">folder</option>
            </select>
          </div>
          {#each def[side].fields as field, fi (fi)}
            <div class="lb-field">
              <input class="field xs" bind:value={field.name} placeholder="value name" />
              <select class="field xs" bind:value={field.type}>
                {#each FIELD_TYPES as t (t.value)}<option value={t.value}>{t.label}</option>{/each}
              </select>
              <select class="field xs" bind:value={field.source.kind}>
                <option value="capture">from name</option><option value="metadata">from metadata</option>
                <option value="attr">file detail</option><option value="const">fixed value</option>
              </select>
              {#if field.source.kind === 'capture'}
                <input class="field xs" bind:value={field.source.name} placeholder="token (e.g. n)" />
              {:else if field.source.kind === 'metadata'}
                <input class="field xs" bind:value={field.source.key} placeholder="key (e.g. recorded_at)" />
              {:else if field.source.kind === 'attr'}
                <select class="field xs" bind:value={field.source.attr}>
                  {#each ATTRS as a (a.value)}<option value={a.value}>{a.label}</option>{/each}
                </select>
              {:else}
                <input class="field xs" value={String(field.source.value ?? '')}
                       oninput={(e) => (field.source.value = e.currentTarget.value)} placeholder="value" />
              {/if}
              <button class="btn xs" onclick={() => addStep(field)} title="add a transform step">+step</button>
              <button class="btn xs" onclick={() => removeField(side, fi)} title="remove">×</button>
              {#if field.steps && field.steps.length}
                <div class="lb-steps">
                  {#each field.steps as step, si (si)}
                    {@render stepRow(field, step, si)}
                  {/each}
                </div>
              {/if}
            </div>
          {/each}
          <button class="btn sm" onclick={() => addField(side)}>+ Add a value</button>
        </div>
      {/each}
    </section>

    <section>
      <div class="lb-step-n">4 · When do a clip and a file go together?</div>
      <div class="row">Match when
        <select class="field sm" bind:value={def.match.combine}><option value="all">ALL</option><option value="any">ANY</option></select>
        of these are true:</div>
      {#each def.match.conditions as cond, ci (ci)}
        {@render conditionRow(cond, ci)}
      {/each}
      <button class="btn sm" onclick={addCondition} disabled={clipFieldNames.length === 0 || fileFieldNames.length === 0}>+ Add a condition</button>
      {#if clipFieldNames.length === 0 || fileFieldNames.length === 0}
        <div class="hint">Add a value to each side first (step 3).</div>{/if}
    </section>

    <section>
      <div class="lb-step-n">5 · How do we resolve matches?</div>
      <div class="row">
        <label>One clip can have
          <select class="field sm" bind:value={perClip}><option value="any">any number of files</option><option value="one">at most one file</option></select></label>
        <label>One file shared by several clips?
          <select class="field sm" bind:value={sharedFiles}><option value="yes">Yes</option><option value="no">No</option></select></label>
      </div>
      <div class="row"><label>When files compete for a clip
        <select class="field sm" bind:value={competition}>
          <option value="closest">pick the closest</option><option value="best">pick the best score</option>
          <option value="all">keep all</option><option value="ask">ask me (flag)</option>
        </select></label>
        <button class="btn xs" onclick={() => (showAdvanced = !showAdvanced)}>{showAdvanced ? 'hide' : 'advanced…'}</button>
      </div>
      {#if showAdvanced}
        <div class="lb-adv">
          <label>Strategy <select class="field sm" bind:value={def.resolve.strategy}>
            <option value="keep_all">keep all</option><option value="best_per_clip">best per clip</option>
            <option value="best_per_file">best per file</option><option value="best_overall">best overall pairing</option>
            <option value="quota">limited total</option></select></label>
          <label>Quota <input class="field xs" type="number" bind:value={def.resolve.quota} /></label>
          <label>Within Δ of best <input class="field xs" type="number" step="0.05" bind:value={def.resolve.relative_threshold} /></label>
          <label>Ambiguity margin <input class="field xs" type="number" step="0.01" bind:value={def.resolve.ambiguity_margin} /></label>
          <label>Min per clip <input class="field xs" type="number" bind:value={def.resolve.per_clip_min} /></label>
          <label class="chk"><input type="checkbox" bind:checked={def.resolve.stable} /> stable (don't reshuffle)</label>
        </div>
      {/if}
    </section>

    <section>
      <div class="lb-step-n">6 · Buttons on each linked file</div>
      <div class="hint">Reveal & Open (default program) are always shown. Add programs to open specific file types with:</div>
      {#each def.actions.open_with as act, ai (ai)}
        <div class="lb-field">
          <input class="field xs" bind:value={act.name} placeholder="button label" />
          <input class="field xs" value={commaList(act.extensions)} oninput={(e) => (act.extensions = parseCommaList(e.currentTarget.value))} placeholder="exts (blank=any)" />
          <input class="field" bind:value={act.program} placeholder="C:\path\program.exe" />
          <button class="btn xs" onclick={() => removeAction(ai)}>×</button>
        </div>
      {/each}
      <button class="btn sm" onclick={addAction}>+ Add an "Open with"</button>
    </section>

    <div class="lb-bottom">
      <button class="btn sm" onclick={openRaw} title="edit the raw rule (advanced)">{'{ } Raw'}</button>
      <span style:flex="1"></span>
      {#if saveError}<span class="lb-err">{saveError}</span>{/if}
      <button class="btn sm" onclick={onCancel}>Cancel</button>
      <button class="btn sm primary" onclick={save} disabled={saving}>{saving ? 'Saving…' : (linker ? 'Save' : 'Create')}</button>
    </div>
  </div>

  <div class="lb-preview">
    <div class="lb-prev-h">Live preview {#if previewing}<span class="faint">· running…</span>{/if}</div>
    {#if previewError}<div class="lb-err">{previewError}</div>{/if}
    {#if preview}
      <div class="lb-buckets">
        <span class="bk ok">{preview.counts.links} linked</span>
        <span class="bk">{preview.counts.matched} clips</span>
        <span class="bk warn">{preview.counts.ambiguous} needs you</span>
        <span class="bk">{preview.counts.unmatched} unmatched</span>
        <span class="bk">{preview.counts.unused} unused files</span>
      </div>
      {#if Object.keys(preview.clip_errors).length || Object.keys(preview.file_errors).length}
        <div class="lb-prev-sec">Parse issues</div>
        {#each Object.entries(preview.clip_errors).slice(0, 4) as [cid, errs] (cid)}
          <div class="lb-perr">clip {cid}: {Object.values(errs).join('; ')}</div>{/each}
        {#each Object.entries(preview.file_errors).slice(0, 4) as [path, errs] (path)}
          <div class="lb-perr">{path.split(/[\\/]/).pop()}: {Object.values(errs).join('; ')}</div>{/each}
      {/if}
      <div class="lb-prev-sec">Sample links</div>
      {#if preview.links.length === 0}<div class="faint">no links yet — adjust the rule above.</div>{/if}
      {#each preview.links.slice(0, 12) as lk (lk.clip_id + lk.file_path)}
        <div class="lb-link">
          <div class="lb-link-h">clip {lk.clip_id} → {lk.file_path.split(/[\\/]/).pop()} <span class="sc">{Math.round(lk.score * 100)}%</span></div>
          {#if lk.reasons.length}<div class="lb-why">{lk.reasons.join(' · ')}</div>{/if}
        </div>
      {/each}
    {:else if !previewError}
      <div class="faint">building preview…</div>
    {/if}
  </div>
</div>

{#if showRaw}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="modal-bg" onclick={(e) => { if (e.target === e.currentTarget) showRaw = false; }}>
    <div class="modal">
      <div class="mtop"><h3>Raw rule (advanced)</h3><span style:flex="1"></span><button class="btn sm" onclick={() => (showRaw = false)}>Close</button></div>
      <div class="mbody">
        <textarea class="field lb-raw" bind:value={rawText} spellcheck="false"></textarea>
        {#if rawError}<div class="lb-err">{rawError}</div>{/if}
      </div>
      <div class="mfoot"><button class="btn sm" onclick={() => (showRaw = false)}>Cancel</button>
        <button class="btn sm primary" onclick={applyRaw}>Apply</button></div>
    </div>
  </div>
{/if}

{#snippet stepRow(field: FieldDef, step: Step, si: number)}
  <div class="lb-step">
    <select class="field xs" bind:value={step.op}>{#each STEP_OPS as s (s.value)}<option value={s.value}>{s.label}</option>{/each}</select>
    {#if stepArg(step.op) === 'value'}<input class="field xs" value={String(step.value ?? '')} oninput={(e) => (step.value = e.currentTarget.value)} placeholder="value" />{/if}
    {#if stepArg(step.op) === 'field'}<input class="field xs" bind:value={step.field} placeholder="other field" />{/if}
    {#if stepArg(step.op) === 'unit'}<input class="field xs" value={String(step.value ?? '')} oninput={(e) => (step.value = e.currentTarget.value)} placeholder="amount" /><select class="field xs" bind:value={step.unit}><option value="second">sec</option><option value="minute">min</option><option value="hour">hr</option><option value="day">day</option></select>{/if}
    {#if stepArg(step.op) === 'sep+index'}<input class="field xs" bind:value={step.sep} placeholder="sep" /><input class="field xs" type="number" bind:value={step.index} placeholder="#" />{/if}
    <button class="btn xs" onclick={() => removeStep(field, si)}>×</button>
  </div>
{/snippet}

{#snippet conditionRow(cond: Condition, ci: number)}
  <div class="lb-cond">
    <span class="cw">the clip's</span>
    <select class="field xs" bind:value={cond.left.field}>{#each clipFieldNames as n (n)}<option value={n}>{n}</option>{/each}</select>
    <select class="field sm" bind:value={cond.op}>{#each CONDITION_OPS as o (o.value)}<option value={o.value}>{o.label}</option>{/each}</select>
    {#if opNeeds(cond.op, 'right') && cond.right}
      <span class="cw">the file's</span>
      <select class="field xs" bind:value={cond.right.field}>{#each fileFieldNames as n (n)}<option value={n}>{n}</option>{/each}</select>
    {/if}
    {#if opNeeds(cond.op, 'start') && cond.start && cond.end}
      <span class="cw">[</span>
      <select class="field xs" bind:value={cond.start.field}>{#each fileFieldNames as n (n)}<option value={n}>{n}</option>{/each}</select>
      <span class="cw">…</span>
      <select class="field xs" bind:value={cond.end.field}>{#each fileFieldNames as n (n)}<option value={n}>{n}</option>{/each}</select>
      <span class="cw">]</span>
    {/if}
    {#if opNeeds(cond.op, 'tolerance')}<input class="field xs" type="number" bind:value={cond.tolerance} title="tolerance (seconds)" /><span class="cw">s</span>{/if}
    {#if opNeeds(cond.op, 'slack')}<input class="field xs" type="number" bind:value={cond.slack} title="slack (seconds)" /><span class="cw">s slack</span>{/if}
    {#if opNeeds(cond.op, 'threshold')}<input class="field xs" type="number" step="0.05" min="0" max="1" bind:value={cond.threshold} title="min similarity" />{/if}
    <button class="btn xs" onclick={() => removeCondition(ci)}>×</button>
  </div>
{/snippet}

<style>
  .lb { display: grid; grid-template-columns: 1fr 340px; gap: 14px; height: 100%; min-height: 0; }
  .lb-form { overflow-y: auto; padding-right: 6px; display: flex; flex-direction: column; gap: 14px; }
  .lb-preview { overflow-y: auto; background: var(--bg-1, #14171d); border: 1px solid var(--border, #2a2e38); border-radius: 10px; padding: 12px; }
  .lb-head { display: flex; gap: 8px; align-items: center; }
  .lb-name { font-size: 16px; font-weight: 700; flex: 1; }
  .lb-color { width: 34px; height: 30px; border: none; background: none; padding: 0; }
  .lb-enable { display: flex; align-items: center; gap: 4px; font-size: 13px; white-space: nowrap; }
  .lb-desc { width: 100%; }
  section { border: 1px solid var(--border, #2a2e38); border-radius: 10px; padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; }
  .lb-step-n { font-weight: 700; font-size: 13px; color: var(--accent, #7c9cff); }
  .row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 13px; }
  .row label { display: flex; align-items: center; gap: 5px; }
  .chk { gap: 4px !important; }
  .lb-side { border-left: 2px solid var(--border, #2a2e38); padding-left: 10px; margin-top: 4px; display: flex; flex-direction: column; gap: 6px; }
  .lb-side-h { font-weight: 600; font-size: 12px; color: var(--text-2, #aab); }
  .lb-field { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
  .lb-steps { display: flex; flex-direction: column; gap: 3px; width: 100%; padding-left: 16px; }
  .lb-step { display: flex; align-items: center; gap: 4px; }
  .lb-cond { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; padding: 5px 0; }
  .cw { font-size: 12px; color: var(--text-3, #889); }
  .lb-adv { display: flex; flex-wrap: wrap; gap: 10px; font-size: 12px; padding-top: 6px; }
  .lb-adv label { display: flex; align-items: center; gap: 4px; }
  .field.xs { font-size: 12px; padding: 2px 5px; max-width: 130px; }
  .field.sm { font-size: 12px; padding: 3px 6px; }
  .btn.xs { font-size: 11px; padding: 2px 6px; }
  .hint { font-size: 11px; color: var(--text-3, #889); }
  .lb-bottom { display: flex; align-items: center; gap: 8px; padding-top: 4px; }
  .lb-err { color: var(--red, #ef4444); font-size: 12px; }
  .lb-prev-h { font-weight: 700; margin-bottom: 8px; }
  .lb-buckets { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
  .bk { font-size: 11px; padding: 2px 8px; border-radius: 20px; background: var(--bg-2, #1a1d24); border: 1px solid var(--border, #2a2e38); }
  .bk.ok { background: color-mix(in srgb, var(--accent, #7c9cff) 22%, transparent); border-color: var(--accent, #7c9cff); }
  .bk.warn { color: var(--amber, #f59e0b); border-color: currentColor; }
  .lb-prev-sec { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--text-3, #889); margin: 10px 0 4px; }
  .lb-link { padding: 5px 7px; background: var(--bg-2, #1a1d24); border-radius: 6px; margin-bottom: 4px; }
  .lb-link-h { font-size: 12px; display: flex; gap: 6px; align-items: center; }
  .lb-link-h .sc { margin-left: auto; color: var(--accent, #7c9cff); font-weight: 700; }
  .lb-why { font-size: 11px; color: var(--text-3, #889); margin-top: 2px; }
  .lb-perr { font-size: 11px; color: var(--amber, #f59e0b); }
  .lb-raw { width: 100%; min-height: 300px; font-family: ui-monospace, monospace; font-size: 12px; }
  .mfoot { display: flex; justify-content: flex-end; gap: 8px; padding: 10px 0 0; }
</style>
