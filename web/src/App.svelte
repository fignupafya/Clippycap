<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from './lib/api';
  import type { AppConfig, AssetDetail, AssetSummary, Source, Tag } from './lib/api';

  type Quick = 'all' | 'untagged' | 'new';
  type EditKind = 'trim' | 'remove' | 'extract' | 'cut';

  let tags = $state<Tag[]>([]);
  let tagById = $derived(new Map(tags.map((t) => [t.id, t])));
  let sources = $state<Source[]>([]);
  let assets = $state<AssetSummary[]>([]);
  let total = $state(0);
  let loading = $state(false);
  let error = $state<string | null>(null);

  let quick = $state<Quick>('all');
  let filterTagIds = $state<number[]>([]);
  let searchText = $state('');
  let appliedText = $state('');
  let sort = $state('recorded_desc');

  let detail = $state<AssetDetail | null>(null);
  let scanJob = $state<{ scanned: number } | null>(null);
  let showTags = $state(false);
  let showKeys = $state(false);
  let newTagName = $state('');
  let newTagColor = $state('#56c271');
  let videoEl = $state<HTMLVideoElement>();
  let playbackRate = $state(1);
  let generalNoteText = $state('');

  // playback + editing
  let cfg = $state<AppConfig | null>(null);
  let editingAvailable = $state(true);
  let curMs = $state(0);
  let durMs = $state(0);
  let videoVersion = $state(0);             // bump to force the <video> to reload after an in-place edit
  let selIn = $state<number | null>(null);
  let selOut = $state<number | null>(null);
  let busy = $state(false);

  function getNum(meta: Record<string, unknown> | undefined, key: string, fallback: number): number {
    const v = meta?.[key];
    return typeof v === 'number' ? v : fallback;
  }
  let selValid = $derived(selIn !== null && selOut !== null && selOut > selIn);
  let fps = $derived(getNum(detail?.metadata, 'fps', 30) || 30);
  let timelineMs = $derived(getNum(detail?.metadata, 'duration_ms', 0) || durMs || 0);

  const FALLBACK_KEYS: Record<string, string> = {
    play_pause: 'space', toggle_play_k: 'k', skip_back: 'arrowleft', skip_fwd: 'arrowright',
    skip_back_fine: 'shift+arrowleft', skip_fwd_fine: 'shift+arrowright', frame_prev: ',', frame_next: '.',
    add_note_at_time: 'n', sel_in: 'i', sel_out: 'o', sel_clear: 'shift+i', add_interval_note: 'shift+n',
    seek_start: 'home', seek_end: 'end', prev_asset: 'pageup', next_asset: 'pagedown',
  };
  const SHORTCUT_ROWS: [string, string][] = [
    ['play_pause', 'Play / pause'], ['skip_back', 'Skip back'], ['skip_fwd', 'Skip forward'],
    ['skip_back_fine', 'Skip back (fine)'], ['skip_fwd_fine', 'Skip forward (fine)'],
    ['frame_prev', 'Previous frame'], ['frame_next', 'Next frame'], ['add_note_at_time', 'Add note at current time'],
    ['sel_in', 'Set selection IN'], ['sel_out', 'Set selection OUT'], ['sel_clear', 'Clear selection'],
    ['add_interval_note', 'Add interval note for the selection'], ['seek_start', 'Jump to start'],
    ['seek_end', 'Jump to end'], ['prev_asset', 'Previous clip'], ['next_asset', 'Next clip'],
  ];
  function binding(action: string): string { return (cfg?.keybindings?.[action] ?? FALLBACK_KEYS[action] ?? '').toLowerCase(); }
  function skipSeconds(fine: boolean): number {
    const v = cfg?.player?.[fine ? 'skip_seconds_fine' : 'skip_seconds'];
    return typeof v === 'number' ? v : (fine ? 1 : 5);
  }

  async function loadTags() { try { tags = await api.listTags(); } catch (e) { error = String(e); } }
  async function loadSources() { try { sources = await api.listSources(); } catch (e) { error = String(e); } }
  async function loadAssets() {
    loading = true; error = null;
    try {
      const page = await api.listAssets({
        tags_all: filterTagIds, untagged: quick === 'untagged', never_opened: quick === 'new',
        text: appliedText.trim() || undefined, sort, limit: 300,
      });
      assets = page.items; total = page.total;
    } catch (e) { error = String(e); } finally { loading = false; }
  }

  onMount(() => {
    void loadTags(); void loadSources();
    void api.getConfig().then((c) => { cfg = c; }).catch(() => { /* fall back to FALLBACK_KEYS */ });
    void api.getHealth().then((h) => { editingAvailable = !!h.ffmpeg; }).catch(() => { /* keep true */ });
  });
  $effect(() => { void loadAssets(); });
  $effect(() => { if (videoEl) videoEl.playbackRate = playbackRate; });
  $effect(() => { generalNoteText = detail?.general_note ?? ''; });
  $effect(() => { if (detail) { selIn = null; selOut = null; } });   // a new (or refreshed) asset clears the selection

  function fmt(ms: number): string {
    const s = Math.max(0, Math.floor(ms / 1000));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }
  function durationOf(meta: Record<string, unknown>): string {
    return typeof meta['duration_ms'] === 'number' ? fmt(meta['duration_ms'] as number) : '';
  }
  function toggleTagFilter(id: number) {
    quick = 'all';
    filterTagIds = filterTagIds.includes(id) ? filterTagIds.filter((x) => x !== id) : [...filterTagIds, id];
  }
  async function openDetail(a: AssetSummary) { detail = await api.getAsset(a.id); void api.markOpened(a.id); }
  async function refreshDetail() { if (detail) detail = await api.getAsset(detail.id); }
  function closeDetail() { detail = null; void loadAssets(); }
  function gotoSibling(delta: number) {
    if (!detail) return;
    const cur = detail;
    const next = assets[assets.findIndex((a) => a.id === cur.id) + delta];
    if (next) void openDetail(next);
  }

  async function addSourcePrompt() {
    const path = window.prompt('Folder path to add as a source:');
    if (!path) return;
    try { await api.addSource(path); await loadSources(); } catch (e) { window.alert(String(e)); }
  }
  function scanAll() {
    void (async () => {
      try {
        const { job_id } = await api.scanAll();
        const tick = async () => {
          const j = await api.getJob(job_id);
          if (j.state === 'pending' || j.state === 'running') {
            scanJob = { scanned: j.scanned }; setTimeout(() => void tick(), 400);
          } else { scanJob = null; await loadAssets(); await loadTags(); }
        };
        await tick();
      } catch (e) { scanJob = null; window.alert(String(e)); }
    })();
  }
  async function createTag() {
    if (!newTagName.trim()) return;
    try { await api.createTag({ name: newTagName.trim(), color: newTagColor, sort_order: tags.length }); newTagName = ''; await loadTags(); }
    catch (e) { window.alert(String(e)); }
  }
  async function deleteTag(id: number) {
    if (!window.confirm('Delete this tag everywhere?')) return;
    try { await api.deleteTag(id); await loadTags(); await loadAssets(); await refreshDetail(); } catch (e) { window.alert(String(e)); }
  }
  async function applyTag(tagId: number) {
    if (!detail) return;
    try { await api.applyTag(detail.id, tagId); await refreshDetail(); await loadTags(); } catch (e) { window.alert(String(e)); }
  }
  async function unapplyTag(tagId: number) {
    if (!detail) return;
    try { await api.unapplyTag(detail.id, tagId); await refreshDetail(); await loadTags(); } catch (e) { window.alert(String(e)); }
  }
  async function saveGeneralNote() {
    if (!detail) return;
    try { await api.setGeneralNote(detail.id, generalNoteText); } catch (e) { window.alert(String(e)); }
  }
  async function addNote(ms: number, endMs: number | null = null) {
    if (!detail) return;
    const body = window.prompt(endMs === null ? `Note at ${fmt(ms)}:` : `Note for ${fmt(ms)} – ${fmt(endMs)}:`);
    if (body === null) return;
    try { await api.addTimestampNote(detail.id, Math.floor(ms), body, endMs === null ? undefined : Math.floor(endMs)); await refreshDetail(); }
    catch (e) { window.alert(String(e)); }
  }
  function addNoteAtNow() { void addNote(curMs); }
  function addIntervalNote() {
    if (!selValid) { window.alert(`Set a selection first: press "${binding('sel_in')}" (in) then "${binding('sel_out')}" (out).`); return; }
    void addNote(selIn as number, selOut as number);
  }
  async function deleteNote(id: number) {
    try { await api.deleteNote(id); await refreshDetail(); } catch (e) { window.alert(String(e)); }
  }
  function seek(ms: number) { if (videoEl) { const t = Math.max(0, ms); videoEl.currentTime = t / 1000; curMs = Math.floor(t); } }
  function nudge(seconds: number) { if (videoEl) { videoEl.currentTime = Math.max(0, videoEl.currentTime + seconds); curMs = Math.floor(videoEl.currentTime * 1000); } }
  function step(frames: number) { if (videoEl) { videoEl.pause(); nudge(frames / fps); } }
  function togglePlay() { if (videoEl) { if (videoEl.paused) void videoEl.play(); else videoEl.pause(); } }
  function setIn() { selIn = curMs; if (selOut !== null && selOut <= selIn) selOut = null; }
  function setOut() { selOut = curMs; if (selIn !== null && selIn >= selOut) selIn = null; }
  function clearSel() { selIn = null; selOut = null; }
  function pct(ms: number): number { return timelineMs ? Math.max(0, Math.min(100, (ms / timelineMs) * 100)) : 0; }
  function timelineClick(e: MouseEvent) {
    if (!timelineMs) return;
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    seek(((e.clientX - r.left) / r.width) * timelineMs);
  }
  async function doEdit(kind: EditKind) {
    if (!detail || !selValid || busy) return;
    const id = detail.id, s = selIn as number, o = selOut as number;
    const span = `${fmt(s)} – ${fmt(o)}`;
    if (kind === 'trim' && !window.confirm(`Permanently shorten this clip to keep only ${span}? There is no undo.`)) return;
    if (kind === 'remove' && !window.confirm(`Permanently cut ${span} out of this clip? There is no undo.`)) return;
    if (kind === 'cut' && !window.confirm(`Save ${span} as a new clip AND cut it out of this clip? The cut has no undo.`)) return;
    busy = true;
    try {
      if (kind === 'trim') await api.trimAsset(id, s, o);
      else if (kind === 'remove') await api.removeSegment(id, s, o);
      else { const made = await api.extractSegment(id, s, o, kind === 'cut'); window.alert(`Saved as a new clip: ${made.title}`); }
      clearSel();
      videoVersion += 1;
      await refreshDetail();
      await loadAssets();
    } catch (e) { window.alert(String(e)); } finally { busy = false; }
  }

  function inEditable(el: EventTarget | null): boolean {
    const t = el as HTMLElement | null;
    return !!t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
  }
  function keyName(e: KeyboardEvent): string {
    const mods: string[] = [];
    if (e.ctrlKey) mods.push('ctrl');
    if (e.shiftKey) mods.push('shift');
    if (e.altKey) mods.push('alt');
    return [...mods, e.key === ' ' ? 'space' : e.key].join('+').toLowerCase();
  }
  function onKey(e: KeyboardEvent) {
    if (!detail || inEditable(e.target)) return;
    if (e.key === 'Escape') { closeDetail(); return; }
    const k = keyName(e);
    const actions: [string, () => void][] = [
      [binding('play_pause'), togglePlay], [binding('toggle_play_k'), togglePlay],
      [binding('skip_back'), () => nudge(-skipSeconds(false))], [binding('skip_fwd'), () => nudge(skipSeconds(false))],
      [binding('skip_back_fine'), () => nudge(-skipSeconds(true))], [binding('skip_fwd_fine'), () => nudge(skipSeconds(true))],
      [binding('frame_prev'), () => step(-1)], [binding('frame_next'), () => step(1)],
      [binding('add_note_at_time'), addNoteAtNow], [binding('add_interval_note'), addIntervalNote],
      [binding('sel_in'), setIn], [binding('sel_out'), setOut], [binding('sel_clear'), clearSel],
      [binding('seek_start'), () => seek(0)], [binding('seek_end'), () => seek(timelineMs)],
      [binding('prev_asset'), () => gotoSibling(-1)], [binding('next_asset'), () => gotoSibling(1)],
    ];
    for (const [bind, run] of actions) {
      if (bind && bind === k) { e.preventDefault(); run(); return; }
    }
  }
  function hideBrokenImg(e: Event) { (e.currentTarget as HTMLImageElement).style.display = 'none'; }
</script>

<svelte:window onkeydown={onKey} />

<div class="app">
  <header>
    <div class="brand"><span class="logo">C</span> Clippycap</div>
    <input class="field search" placeholder="Search clips & notes — press Enter" bind:value={searchText}
           onkeydown={(e) => { if (e.key === 'Enter') appliedText = searchText; }} />
    <select class="field sort" bind:value={sort}>
      <option value="recorded_desc">Recorded (newest)</option>
      <option value="recorded_asc">Recorded (oldest)</option>
      <option value="added_desc">Added (newest)</option>
      <option value="duration_desc">Duration</option>
      <option value="title_asc">Title</option>
    </select>
    <button class="btn sm" onclick={scanAll}>{scanJob ? `Scanning… ${scanJob.scanned}` : 'Scan'}</button>
    <button class="btn sm" onclick={() => (showTags = true)}>Tags</button>
  </header>

  <div class="body">
    <aside>
      <nav>
        <button class="nav" class:on={quick === 'all' && filterTagIds.length === 0} onclick={() => { quick = 'all'; filterTagIds = []; }}>All clips <span class="ct">{total}</span></button>
        <button class="nav" class:on={quick === 'new'} onclick={() => { quick = 'new'; filterTagIds = []; }}>New (unopened)</button>
        <button class="nav" class:on={quick === 'untagged'} onclick={() => { quick = 'untagged'; filterTagIds = []; }}>Untagged</button>
      </nav>
      <div class="sec-title">Tags</div>
      <div class="tagcloud">
        {#each tags as t (t.id)}
          <button class="tagchip" class:on={filterTagIds.includes(t.id)} style:background={filterTagIds.includes(t.id) ? t.color : ''} onclick={() => toggleTagFilter(t.id)}>
            {t.icon ?? ''} {t.name} <span class="n">{t.asset_count ?? 0}</span>
          </button>
        {/each}
        {#if tags.length === 0}<span class="faint" style:font-size="12px">No tags yet — create some via “Tags”.</span>{/if}
      </div>
      <div class="sec-title">Sources</div>
      {#each sources as s (s.id)}<div class="src" title={s.path}>{s.path}</div>{/each}
      <button class="btn sm" style:margin-top="6px" onclick={addSourcePrompt}>+ Add source folder</button>
    </aside>

    <main>
      {#if error}<div class="err">{error}</div>{/if}
      <div class="bar"><b>{total}</b> clips{loading ? ' · loading…' : ''}{filterTagIds.length ? ' · filtered by ' + filterTagIds.length + ' tag(s)' : ''}</div>
      <div class="grid">
        {#each assets as a (a.id)}
          <button class="card" onclick={() => openDetail(a)}>
            <div class="thumb">
              <img src={a.thumbnail_url} alt="" onerror={hideBrokenImg} />
              <span class="film">▶</span>
              {#if durationOf(a.metadata)}<span class="dur">{durationOf(a.metadata)}</span>{/if}
              {#if a.is_new}<span class="badge bnew">new</span>{/if}
              {#if a.note_count}<span class="badge">📝 {a.note_count}</span>{/if}
            </div>
            <div class="cb">
              <div class="ct" title={a.title}>{a.title}</div>
              <div class="tags">
                {#each a.tag_ids as id (id)}
                  {@const t = tagById.get(id)}
                  {#if t}<span class="pill" style:background={t.color}>{t.icon ?? ''} {t.name}</span>{/if}
                {/each}
              </div>
            </div>
          </button>
        {/each}
      </div>
    </main>
  </div>
</div>

{#if detail}
  {@const d = detail}
  <div class="overlay">
    <div class="otop">
      <button class="btn sm" onclick={closeDetail}>← Library</button>
      <button class="btn sm" onclick={() => gotoSibling(-1)} title="Previous clip ({binding('prev_asset')})">‹</button>
      <button class="btn sm" onclick={() => gotoSibling(1)} title="Next clip ({binding('next_asset')})">›</button>
      <div class="otitle">{d.title}</div>
      <span style:flex="1"></span>
      <button class="btn sm" onclick={() => (showKeys = true)} title="Keyboard shortcuts">⌨ Keys</button>
    </div>
    <div class="obody">
      <div class="player">
        <video bind:this={videoEl} src={`${d.stream_url}?v=${videoVersion}`} controls
               ontimeupdate={() => { if (videoEl) curMs = Math.floor(videoEl.currentTime * 1000); }}
               onloadedmetadata={() => { if (videoEl && Number.isFinite(videoEl.duration)) durMs = Math.floor(videoEl.duration * 1000); }}></video>
        <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
        <div class="timeline" onclick={timelineClick} title="Click to seek">
          {#if selValid}<div class="sel" style:left={pct(selIn ?? 0) + '%'} style:width={(pct(selOut ?? 0) - pct(selIn ?? 0)) + '%'}></div>{/if}
          {#each d.timestamped_notes as n (n.id)}
            {#if n.end_timestamp_ms != null}
              <div class="nbar" style:left={pct(n.timestamp_ms ?? 0) + '%'} style:width={Math.max(0.7, pct(n.end_timestamp_ms) - pct(n.timestamp_ms ?? 0)) + '%'} title={n.body}></div>
            {:else}
              <div class="ntick" style:left={pct(n.timestamp_ms ?? 0) + '%'} title={n.body}></div>
            {/if}
          {/each}
          <div class="playhead" style:left={pct(curMs) + '%'}></div>
        </div>
        <div class="pctrl">
          <button class="btn sm" onclick={() => nudge(-skipSeconds(false))} title="back {skipSeconds(false)}s ({binding('skip_back')})">« {skipSeconds(false)}s</button>
          <button class="btn sm" onclick={() => nudge(-skipSeconds(true))} title="back {skipSeconds(true)}s ({binding('skip_back_fine')})">‹ {skipSeconds(true)}s</button>
          <button class="btn sm" onclick={() => step(-1)} title="back 1 frame ({binding('frame_prev')})">⟨ frame</button>
          <button class="btn sm" onclick={togglePlay} title="play / pause ({binding('play_pause')})">⏯</button>
          <button class="btn sm" onclick={() => step(1)} title="forward 1 frame ({binding('frame_next')})">frame ⟩</button>
          <button class="btn sm" onclick={() => nudge(skipSeconds(true))} title="forward {skipSeconds(true)}s ({binding('skip_fwd_fine')})">{skipSeconds(true)}s ›</button>
          <button class="btn sm" onclick={() => nudge(skipSeconds(false))} title="forward {skipSeconds(false)}s ({binding('skip_fwd')})">{skipSeconds(false)}s »</button>
          <label>Speed
            <select bind:value={playbackRate}>
              {#each [0.1, 0.25, 0.5, 1, 1.5, 2] as r}<option value={r}>{r}×</option>{/each}
            </select>
          </label>
          <span class="time">{fmt(curMs)} / {fmt(timelineMs)}</span>
          <button class="btn sm" onclick={addNoteAtNow} title="note at current time ({binding('add_note_at_time')})">+ note @ now</button>
        </div>
        <div class="trim">
          <button class="btn sm" onclick={setIn} title="set selection IN here ({binding('sel_in')})">⟦ in @ {fmt(curMs)}</button>
          <button class="btn sm" onclick={setOut} title="set selection OUT here ({binding('sel_out')})">out @ {fmt(curMs)} ⟧</button>
          {#if selIn !== null || selOut !== null}
            <span class="seltext">selection {selIn !== null ? fmt(selIn) : '—'} – {selOut !== null ? fmt(selOut) : '—'}{selValid ? ` (${fmt((selOut as number) - (selIn as number))})` : ''}</span>
            <button class="btn sm" onclick={clearSel}>clear</button>
          {/if}
          {#if selValid}
            <button class="btn sm" disabled={busy} onclick={addIntervalNote} title="note covering the selection ({binding('add_interval_note')})">+ interval note</button>
            {#if editingAvailable}
              <span style:flex="1"></span>
              <button class="btn sm" disabled={busy} onclick={() => doEdit('extract')}>save as new clip</button>
              <button class="btn sm" disabled={busy} onclick={() => doEdit('cut')}>cut to new clip</button>
              <button class="btn sm" disabled={busy} onclick={() => doEdit('remove')}>remove selection</button>
              <button class="btn sm primary" disabled={busy} onclick={() => doEdit('trim')}>✂ trim to selection</button>
            {:else}<span class="faint">trimming / cutting needs ffmpeg</span>{/if}
          {/if}
        </div>
      </div>
      <div class="side">
        <h4>Tags</h4>
        <div class="tagcloud">
          {#each d.tag_ids as id (id)}
            {@const t = tagById.get(id)}
            {#if t}<span class="pill" style:background={t.color}>{t.icon ?? ''} {t.name} <button class="x" onclick={() => unapplyTag(t.id)}>×</button></span>{/if}
          {/each}
          {#if d.tag_ids.length === 0}<span class="faint">no tags</span>{/if}
        </div>
        <div class="tagcloud" style:margin-top="6px">
          {#each tags.filter((t) => !d.tag_ids.includes(t.id)) as t (t.id)}
            <button class="tagchip" onclick={() => applyTag(t.id)}>+ {t.icon ?? ''} {t.name}</button>
          {/each}
        </div>
        <h4>General note</h4>
        <textarea class="field" rows="5" bind:value={generalNoteText} onblur={saveGeneralNote}></textarea>
        <h4>Timestamped notes</h4>
        {#each d.timestamped_notes as n (n.id)}
          <div class="tsn">
            <button class="ts" onclick={() => seek(n.timestamp_ms ?? 0)}>{n.end_timestamp_ms != null ? `${fmt(n.timestamp_ms ?? 0)}–${fmt(n.end_timestamp_ms)}` : fmt(n.timestamp_ms ?? 0)}</button>
            <span class="body">{n.body}</span>
            <button class="x" onclick={() => deleteNote(n.id)}>🗑</button>
          </div>
        {/each}
        {#if d.timestamped_notes.length === 0}<span class="faint">none yet — use “+ note @ now”</span>{/if}
        <h4>References</h4>
        <span class="faint">{d.reference_count} reference(s). Editing references (with relation types) is in the desktop UI mockup; this minimal build doesn't include it yet.</span>
        <h4>File</h4>
        {#each d.paths as p (p.path)}<div class="src" class:miss={!p.present} title={p.path}>{p.present ? '✓' : '✗'} {p.path}</div>{/each}
      </div>
    </div>
  </div>
{/if}

{#if showKeys}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="modal-bg" onclick={(e) => { if (e.target === e.currentTarget) showKeys = false; }}>
    <div class="modal">
      <div class="mtop"><h3>Keyboard shortcuts</h3><span style:flex="1"></span><button class="btn sm" onclick={() => (showKeys = false)}>Close</button></div>
      <div class="mbody">
        <table class="keys"><tbody>
          {#each SHORTCUT_ROWS as [action, label] (action)}
            <tr><td><kbd>{binding(action) || '—'}</kbd></td><td>{label}</td></tr>
          {/each}
          <tr><td><kbd>esc</kbd></td><td>Close the clip</td></tr>
        </tbody></table>
        <p class="faint" style:margin-top="10px" style:font-size="12px">From <code>[keybindings]</code> in the config — edit it there to change these.</p>
      </div>
    </div>
  </div>
{/if}

{#if showTags}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="modal-bg" onclick={(e) => { if (e.target === e.currentTarget) showTags = false; }}>
    <div class="modal">
      <div class="mtop"><h3>Tags</h3><span style:flex="1"></span><button class="btn sm" onclick={() => (showTags = false)}>Close</button></div>
      <div class="mbody">
        {#each tags as t (t.id)}
          <div class="trow"><span class="sw" style:background={t.color}></span> <b>{t.icon ?? ''} {t.name}</b> <span class="faint" style:font-size="11px">{t.asset_count ?? 0} clips</span><span style:flex="1"></span><button class="x" onclick={() => deleteTag(t.id)}>🗑</button></div>
        {/each}
        <div class="trow" style:margin-top="6px">
          <input class="field" style:max-width="160px" placeholder="new tag name" bind:value={newTagName} />
          <input type="color" bind:value={newTagColor} />
          <button class="btn sm primary" onclick={createTag}>+ Create</button>
        </div>
        <p class="faint" style:margin-top="10px" style:font-size="12px">Tags are flat and entirely yours: name + colour (and, in the full UI, a chosen icon or your own uploaded image). Renaming/recolouring/reordering and per-note tags are in the desktop UI mockup; this minimal build supports create / delete / apply.</p>
      </div>
    </div>
  </div>
{/if}

<style>
  .app { display: flex; flex-direction: column; height: 100%; }
  header { display: flex; align-items: center; gap: 12px; padding: 0 14px; height: 50px; background: var(--bg-1); border-bottom: 1px solid var(--border); flex: none; }
  .brand { font-weight: 800; display: flex; align-items: center; gap: 8px; white-space: nowrap; }
  .logo { width: 24px; height: 24px; border-radius: 6px; background: linear-gradient(135deg, var(--accent), #9a3412); color: #1a0e07; display: grid; place-items: center; font-weight: 900; }
  .search { flex: 1; max-width: 460px; border-radius: 999px; }
  .sort { width: auto; }
  .body { flex: 1; display: flex; min-height: 0; }
  aside { width: 250px; flex: none; background: var(--bg-1); border-right: 1px solid var(--border); overflow-y: auto; padding: 10px; }
  nav { display: flex; flex-direction: column; gap: 2px; margin-bottom: 4px; }
  .nav { text-align: left; padding: 6px 9px; border-radius: 7px; color: var(--text-2); }
  .nav:hover { background: var(--bg-2); color: var(--text); }
  .nav.on { background: var(--accent-soft); color: var(--text); font-weight: 600; }
  .nav .ct { float: right; font-family: ui-monospace, monospace; font-size: 11px; color: var(--text-3); }
  .sec-title { font-size: 11px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; color: var(--text-3); margin: 12px 0 6px; }
  .tagcloud { display: flex; flex-wrap: wrap; gap: 5px; }
  .tagchip { padding: 3px 9px; border-radius: 999px; font-size: 11.5px; font-weight: 600; background: var(--bg-3); border: 1px solid var(--border); color: var(--text-2); }
  .tagchip:hover { color: var(--text); }
  .tagchip.on { color: #0e1116; border-color: transparent; }
  .tagchip .n { font-family: ui-monospace, monospace; font-size: 10px; opacity: .6; }
  .src { font-family: ui-monospace, monospace; font-size: 11px; color: var(--text-2); padding: 3px 0; word-break: break-all; }
  .src.miss { color: var(--text-3); text-decoration: line-through; }
  main { flex: 1; overflow-y: auto; padding: 14px; min-width: 0; }
  .bar { color: var(--text-2); margin-bottom: 10px; }
  .err { background: #4a1d1d; border: 1px solid #6b2b2b; border-radius: 7px; padding: 8px 11px; margin-bottom: 10px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 14px; }
  .card { text-align: left; background: var(--bg-1); border: 1px solid var(--border); border-radius: var(--r); overflow: hidden; transition: .12s; }
  .card:hover { border-color: #3a4350; transform: translateY(-2px); }
  .thumb { position: relative; aspect-ratio: 16/9; background: linear-gradient(135deg, #1c2027, #11141a); display: grid; place-items: center; overflow: hidden; }
  .thumb img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
  .thumb .film { font-size: 28px; color: #39424f; }
  .thumb .dur { position: absolute; right: 6px; bottom: 6px; background: rgba(0,0,0,.72); padding: 1px 5px; border-radius: 4px; font-size: 11px; font-family: ui-monospace, monospace; }
  .thumb .badge { position: absolute; left: 6px; bottom: 6px; background: rgba(0,0,0,.66); padding: 1px 6px; border-radius: 5px; font-size: 10.5px; font-weight: 700; }
  .thumb .badge.bnew { top: 6px; bottom: auto; background: var(--amber); color: #0e1116; }
  .cb { padding: 8px 9px; }
  .cb .ct { font-size: 12.5px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cb .tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
  .pill .x { color: rgba(0,0,0,.55); font-weight: 800; }
  .overlay { position: fixed; inset: 0; background: var(--bg); display: flex; flex-direction: column; z-index: 50; }
  .otop { display: flex; align-items: center; gap: 12px; padding: 0 14px; height: 48px; background: var(--bg-1); border-bottom: 1px solid var(--border); flex: none; }
  .otitle { font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .obody { flex: 1; display: flex; min-height: 0; }
  .player { flex: 1; display: flex; flex-direction: column; padding: 12px; gap: 10px; background: #080a0d; min-width: 0; }
  .player video { flex: 1; min-height: 0; width: 100%; background: #000; border-radius: var(--r); }
  .pctrl { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .pctrl label { font-size: 12px; display: inline-flex; align-items: center; gap: 5px; }
  .side { width: 380px; flex: none; border-left: 1px solid var(--border); padding: 14px; overflow-y: auto; background: var(--bg-1); }
  .side h4 { font-size: 11px; text-transform: uppercase; letter-spacing: .07em; color: var(--text-3); margin: 14px 0 7px; }
  .side h4:first-child { margin-top: 0; }
  .side textarea { resize: vertical; }
  .tsn { display: flex; gap: 8px; align-items: flex-start; padding: 7px; background: var(--bg-2); border: 1px solid var(--border); border-radius: 7px; margin-bottom: 6px; }
  .tsn .ts { font-family: ui-monospace, monospace; font-size: 11.5px; font-weight: 700; color: var(--amber); background: rgba(240,179,79,.13); border: 1px solid rgba(240,179,79,.3); padding: 2px 6px; border-radius: 5px; flex: none; }
  .tsn .body { flex: 1; font-size: 13px; }
  .x { color: var(--text-3); }
  .x:hover { color: #ef5b5b; }
  .modal-bg { position: fixed; inset: 0; background: rgba(4,6,9,.66); display: grid; place-items: center; z-index: 60; padding: 30px; }
  .modal { width: min(560px, 100%); max-height: 80vh; background: var(--bg-1); border: 1px solid var(--border); border-radius: var(--r); display: flex; flex-direction: column; overflow: hidden; }
  .mtop { display: flex; align-items: center; padding: 12px 16px; border-bottom: 1px solid var(--border); }
  .mtop h3 { font-size: 15px; }
  .mbody { padding: 14px 16px; overflow-y: auto; }
  .trow { display: flex; align-items: center; gap: 8px; padding: 6px 0; }
  .trow .sw { width: 16px; height: 16px; border-radius: 4px; border: 2px solid rgba(255,255,255,.13); flex: none; }
  .btn:disabled { opacity: .5; cursor: default; }
  .timeline { position: relative; height: 16px; background: var(--bg-2); border-radius: 5px; cursor: pointer; flex: none; overflow: hidden; }
  .timeline .sel { position: absolute; top: 0; bottom: 0; background: rgba(255,106,43,.22); border-left: 2px solid var(--accent); border-right: 2px solid var(--accent); }
  .timeline .nbar { position: absolute; top: 3px; bottom: 3px; background: rgba(240,179,79,.55); border-radius: 2px; }
  .timeline .ntick { position: absolute; top: 0; bottom: 0; width: 2px; margin-left: -1px; background: var(--amber); }
  .timeline .playhead { position: absolute; top: -2px; bottom: -2px; width: 2px; margin-left: -1px; background: #fff; box-shadow: 0 0 5px rgba(255,255,255,.7); pointer-events: none; }
  .trim { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; }
  .trim .seltext { font-family: ui-monospace, monospace; font-size: 11.5px; font-weight: 700; color: var(--amber); }
  .pctrl .time { font-family: ui-monospace, monospace; font-size: 11.5px; color: var(--text-2); }
  .keys { border-collapse: collapse; width: 100%; }
  .keys td { padding: 3px 12px 3px 0; font-size: 12.5px; vertical-align: top; }
  kbd { font-family: ui-monospace, monospace; font-size: 11px; background: var(--bg-3); border: 1px solid var(--border); border-radius: 4px; padding: 1px 6px; white-space: nowrap; }
</style>
