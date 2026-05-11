<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from './lib/api';
  import type { AppConfig, AssetDetail, AssetSummary, EditingConfig, Note, PlayerConfig, ReferenceType, ReferenceView, Source, Tag } from './lib/api';

  type Quick = 'all' | 'untagged' | 'new';
  type EditKind = 'trim' | 'remove' | 'extract' | 'cut';

  let tags = $state<Tag[]>([]);
  let tagById = $derived(new Map(tags.map((t) => [t.id, t])));
  let sources = $state<Source[]>([]);
  let assets = $state<AssetSummary[]>([]);
  let total = $state(0);
  let loading = $state(false);
  let error = $state<string | null>(null);
  // in-app dialogs (replace window.confirm / window.prompt) and toasts (replace window.alert)
  let dialog = $state<{ kind: 'confirm' | 'prompt'; message: string; detail: string; value: string; placeholder: string; okLabel: string; danger: boolean; multiline: boolean; mentions: boolean; done: (r: string | boolean | null) => void } | null>(null);
  let dlgInputEl = $state<HTMLInputElement | HTMLTextAreaElement>();
  let toasts = $state<{ id: number; text: string; kind: 'info' | 'error' | 'success' }[]>([]);
  let toastSeq = 0;

  let quick = $state<Quick>('all');
  let filterTagIds = $state<number[]>([]);
  let searchText = $state('');
  let appliedText = $state('');
  let sort = $state('recorded_desc');
  let selected = $state<Set<number>>(new Set());     // selected asset ids (grid multi-select)
  let lastSelectedId = $state<number | null>(null);  // anchor for shift-range select
  let bulkAddTag = $state('');
  let bulkRemoveTag = $state('');
  let bulkBusy = $state(false);

  let detail = $state<AssetDetail | null>(null);
  let refs = $state<{ outgoing: ReferenceView[]; incoming: ReferenceView[] }>({ outgoing: [], incoming: [] });
  let refTypes = $state<ReferenceType[]>([]);
  let addingRef = $state(false);
  let refClipQuery = $state('');
  let refTypeId = $state('');
  let editingTitle = $state(false);
  let titleDraft = $state('');
  let titleInputEl = $state<HTMLInputElement>();
  let mention = $state<{ el: HTMLTextAreaElement; queryStart: number; query: string; top: number; left: number; set: (s: string) => void } | null>(null);
  let mentionPopup = $state<{ id: number; title: string; top: number; left: number } | null>(null);
  let editingGeneralNote = $state(false);
  let generalNoteEl = $state<HTMLTextAreaElement>();
  let editNoteBodyId = $state<number | null>(null);
  let noteBodyDraft = $state('');
  let scanJob = $state<{ scanned: number } | null>(null);
  let showTags = $state(false);
  let showKeys = $state(false);
  let showSettings = $state(false);
  let settingsTab = $state<'editing' | 'player' | 'keys'>('editing');
  let pendingEditing = $state<EditingConfig | null>(null);
  let pendingPlayer = $state<PlayerConfig | null>(null);
  let pendingKeybindings = $state<Record<string, string> | null>(null);
  let capturingAction = $state<string | null>(null);
  let savingSettings = $state(false);
  let settingsError = $state<string | null>(null);
  let newTagName = $state('');
  let newTagColor = $state('#56c271');
  let tagIcon = $state('');
  let editingTagId = $state<number | null>(null);
  let tagImageRef = $state<string | null>(null);
  let uploadingImg = $state(false);
  // per-note tag dropdown (only one open at a time)
  let tagDropdownNoteId = $state<number | null>(null);
  let tagDropdownSearch = $state('');
  let tagDropdownInput = $state<HTMLInputElement>();
  let videoEl = $state<HTMLVideoElement>();
  let timelineEl = $state<HTMLDivElement>();
  let dragging = $state<'in' | 'out' | 'play' | null>(null);
  let draggingNote = $state<{ id: number; isInterval: boolean; durationMs: number } | null>(null);
  let draggingNotePreviewMs = $state<number | null>(null);
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
  let exactFrameTime = 0;          // exact PTS (s) of the displayed frame, from requestVideoFrameCallback

  function getNum(meta: Record<string, unknown> | undefined, key: string, fallback: number): number {
    const v = meta?.[key];
    return typeof v === 'number' ? v : fallback;
  }
  let fps = $derived(getNum(detail?.metadata, 'fps', 30) || 30);
  let timelineMs = $derived(getNum(detail?.metadata, 'duration_ms', 0) || durMs || 0);
  // an IN-only selection means "[selIn, end]", an OUT-only selection means "[0, selOut]" -- so the
  // user can trim just the head or just the tail without having to set both ends explicitly.
  let effIn = $derived(selIn ?? 0);
  let effOut = $derived(selOut ?? timelineMs);
  let selValid = $derived(timelineMs > 0 && (selIn !== null || selOut !== null) && effOut > effIn);

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

  function toast(text: string, kind: 'info' | 'error' | 'success' = 'info') {
    const id = ++toastSeq;
    toasts = [...toasts, { id, text, kind }];
    setTimeout(() => { toasts = toasts.filter((t) => t.id !== id); }, kind === 'error' ? 6000 : 3500);
  }
  function confirmDialog(message: string, opts: { detail?: string; okLabel?: string; danger?: boolean } = {}): Promise<boolean> {
    return new Promise<boolean>((res) => {
      dialog = { kind: 'confirm', message, detail: opts.detail ?? '', value: '', placeholder: '', okLabel: opts.okLabel ?? 'OK', danger: opts.danger ?? false, multiline: false, mentions: false, done: (r) => res(r === true) };
    });
  }
  function promptDialog(message: string, opts: { value?: string; placeholder?: string; okLabel?: string; detail?: string; multiline?: boolean; mentions?: boolean } = {}): Promise<string | null> {
    return new Promise<string | null>((res) => {
      dialog = { kind: 'prompt', message, detail: opts.detail ?? '', value: opts.value ?? '', placeholder: opts.placeholder ?? '', okLabel: opts.okLabel ?? 'OK', danger: false, multiline: opts.multiline ?? false, mentions: opts.mentions ?? false, done: (r) => res(typeof r === 'string' ? r : null) };
      setTimeout(() => dlgInputEl?.select(), 0);
    });
  }
  function dialogOk() { const d = dialog; dialog = null; mention = null; if (d) d.done(d.kind === 'prompt' ? d.value : true); }
  function dialogCancel() { const d = dialog; dialog = null; mention = null; if (d) d.done(d.kind === 'prompt' ? null : false); }
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
      if (selected.size > 0) {
        const vis = new Set(assets.map((a) => a.id));
        const kept = [...selected].filter((id) => vis.has(id));
        if (kept.length !== selected.size) selected = new Set(kept);
      }
    } catch (e) { error = String(e); } finally { loading = false; }
  }

  onMount(() => {
    void loadTags(); void loadSources();
    void api.getConfig().then((c) => { cfg = c; }).catch(() => { /* fall back to FALLBACK_KEYS */ });
    void api.getHealth().then((h) => { editingAvailable = !!h.ffmpeg; }).catch(() => { /* keep true */ });
    void api.listReferenceTypes().then((t) => { refTypes = t; }).catch(() => { /* none */ });
  });
  $effect(() => { void loadAssets(); });
  $effect(() => { if (videoEl) videoEl.playbackRate = playbackRate; });
  $effect(() => { generalNoteText = detail?.general_note ?? ''; });
  $effect(() => { if (detail) { selIn = null; selOut = null; } });   // a new (or refreshed) asset clears the selection
  $effect(() => { if (videoEl) trackFrameTime(videoEl); });

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
  function clearSelection() { selected = new Set(); lastSelectedId = null; }
  function selectAllVisible() { selected = new Set(assets.map((a) => a.id)); lastSelectedId = assets.at(-1)?.id ?? null; }
  function toggleSelect(a: AssetSummary) {
    selected = selected.has(a.id) ? new Set([...selected].filter((x) => x !== a.id)) : new Set([...selected, a.id]);
    lastSelectedId = a.id;
  }
  function rangeSelect(a: AssetSummary) {
    const i = assets.findIndex((x) => x.id === lastSelectedId);
    const j = assets.findIndex((x) => x.id === a.id);
    if (i < 0 || j < 0) { toggleSelect(a); return; }
    const next = new Set(selected);
    for (let k = Math.min(i, j); k <= Math.max(i, j); k++) next.add(assets[k].id);
    selected = next; lastSelectedId = a.id;
  }
  function cardClick(a: AssetSummary, e: MouseEvent) {
    if (e.shiftKey && lastSelectedId != null) rangeSelect(a);
    else if (e.ctrlKey || e.metaKey) toggleSelect(a);
    else void openDetail(a);
  }
  async function bulkRun(label: string, fn: (id: number) => Promise<unknown>) {
    const ids = [...selected];
    if (ids.length === 0 || bulkBusy) return;
    bulkBusy = true;
    try {
      const results = await Promise.allSettled(ids.map(fn));
      const failed = results.filter((r) => r.status === 'rejected').length;
      await loadAssets(); await loadTags(); await refreshDetail();
      toast(failed > 0 ? `${label}: ${ids.length - failed} ok, ${failed} failed` : `${label}: ${ids.length} done`, failed > 0 ? 'error' : 'success');
    } finally { bulkBusy = false; }
  }
  function bulkApplyTag(tagId: number) { void bulkRun('apply tag', (id) => api.applyTag(id, tagId)); }
  function bulkRemoveTagFn(tagId: number) { void bulkRun('remove tag', (id) => api.unapplyTag(id, tagId)); }
  async function bulkDelete() {
    const n = selected.size;
    if (n === 0 || !await confirmDialog(`Delete ${n} clip${n === 1 ? '' : 's'}?`, { detail: 'This removes them AND their files on disk — there is no undo.', okLabel: `Delete ${n}`, danger: true })) return;
    await bulkRun('deleted', (id) => api.deleteAsset(id, true));
  }
  async function loadRefs() { if (!detail) { refs = { outgoing: [], incoming: [] }; return; } try { refs = await api.getReferences(detail.id); } catch { /* ignore */ } }
  async function openDetail(a: AssetSummary) { detail = await api.getAsset(a.id); void api.markOpened(a.id); editingTitle = false; addingRef = false; await loadRefs(); }
  async function openClip(id: number) { try { detail = await api.getAsset(id); void api.markOpened(id); editingTitle = false; addingRef = false; await loadRefs(); } catch (e) { toast(String(e), 'error'); } }
  async function refreshDetail() { if (detail) { detail = await api.getAsset(detail.id); await loadRefs(); } }
  function closeDetail() { detail = null; refs = { outgoing: [], incoming: [] }; editingTitle = false; addingRef = false; void loadAssets(); }
  async function addRef(toId: number) {
    if (!detail) return;
    try { await api.addReference({ from_asset_id: detail.id, to_asset_id: toId, type_id: refTypeId ? Number(refTypeId) : null }); addingRef = false; refClipQuery = ''; await loadRefs(); } catch (e) { toast(String(e), 'error'); }
  }
  async function deleteRef(id: number) { try { await api.deleteReference(id); await loadRefs(); } catch (e) { toast(String(e), 'error'); } }
  function startEditTitle() { if (!detail) return; titleDraft = detail.title; editingTitle = true; setTimeout(() => titleInputEl?.focus(), 0); }
  async function saveTitle() {
    if (!detail || !editingTitle) return;
    editingTitle = false;
    const t = titleDraft.trim();
    if (!t || t === detail.title) return;
    try { await api.renameAsset(detail.id, t); await refreshDetail(); await loadAssets(); } catch (e) { toast(String(e), 'error'); }
  }
  async function deleteCurrentClip() {
    if (!detail) return;
    if (!await confirmDialog(`Delete “${detail.title}”?`, { detail: 'This removes the clip AND its file on disk — there is no undo.', okLabel: 'Delete clip', danger: true })) return;
    try { await api.deleteAsset(detail.id, true); closeDetail(); await loadTags(); } catch (e) { toast(String(e), 'error'); }
  }
  async function renameFile() {
    if (!detail) return;
    const cur = ((detail.paths.find((p) => p.present) ?? detail.paths[0])?.path) ?? '';
    const base = cur ? (cur.split(/[\\/]/).pop() ?? '') : detail.title;
    const next = await promptDialog('Rename the file', { value: base, detail: 'The extension is kept.', okLabel: 'Rename' });
    if (next == null || next.trim() === '' || next.trim() === base) return;
    if (videoEl) { videoEl.pause(); videoEl.removeAttribute('src'); videoEl.load(); }   // release the file
    try {
      await new Promise<void>((r) => setTimeout(r, 250));
      await api.renameFile(detail.id, next.trim());
      await refreshDetail(); await loadAssets();
    } catch (e) { toast(String(e), 'error'); } finally { videoVersion += 1; }
  }
  function gotoSibling(delta: number) {
    if (!detail) return;
    const cur = detail;
    const next = assets[assets.findIndex((a) => a.id === cur.id) + delta];
    if (next) void openDetail(next);
  }

  async function addSourcePrompt() {
    const path = await promptDialog('Add a source folder', { placeholder: 'e.g. D:\\Clips', okLabel: 'Add' });
    if (!path) return;
    try { await api.addSource(path); await loadSources(); } catch (e) { toast(String(e), 'error'); }
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
      } catch (e) { scanJob = null; toast(String(e), 'error'); }
    })();
  }
  function startEditTag(t: Tag) { editingTagId = t.id; newTagName = t.name; newTagColor = t.color; tagIcon = t.icon ?? ''; tagImageRef = t.image_ref; }
  function cancelEditTag() { editingTagId = null; newTagName = ''; tagIcon = ''; tagImageRef = null; }
  function removeTagImage() { tagImageRef = null; }
  async function pickTagImage(e: Event) {
    const input = e.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    input.value = '';                                   // allow re-picking the same file
    if (!file) return;
    uploadingImg = true;
    try { tagImageRef = (await api.uploadTagImage(file)).image_ref; }
    catch (err) { toast(String(err), 'error'); }
    finally { uploadingImg = false; }
  }
  async function saveTag() {
    const name = newTagName.trim();
    if (!name) return;
    const icon = tagIcon.trim() || null;
    try {
      if (editingTagId === null) {
        await api.createTag({ name, color: newTagColor, icon, image_ref: tagImageRef, sort_order: tags.length });
      } else {
        const orig = tags.find((t) => t.id === editingTagId);
        await api.updateTag(editingTagId, { name, color: newTagColor, icon, image_ref: tagImageRef, description: orig?.description ?? '', sort_order: orig?.sort_order ?? 0 });
      }
      cancelEditTag(); await loadTags(); await loadAssets(); await refreshDetail();
    } catch (e) { toast(String(e), 'error'); }
  }
  async function deleteTag(id: number) {
    if (!await confirmDialog('Delete this tag everywhere?', { detail: 'It is removed from every clip and note.', okLabel: 'Delete', danger: true })) return;
    try { await api.deleteTag(id); if (editingTagId === id) cancelEditTag(); await loadTags(); await loadAssets(); await refreshDetail(); } catch (e) { toast(String(e), 'error'); }
  }
  async function applyTag(tagId: number) {
    if (!detail) return;
    try { await api.applyTag(detail.id, tagId); await refreshDetail(); await loadTags(); } catch (e) { toast(String(e), 'error'); }
  }
  async function unapplyTag(tagId: number) {
    if (!detail) return;
    try { await api.unapplyTag(detail.id, tagId); await refreshDetail(); await loadTags(); } catch (e) { toast(String(e), 'error'); }
  }
  async function saveGeneralNote() {
    if (!detail) return;
    try { await api.setGeneralNote(detail.id, generalNoteText); await refreshDetail(); } catch (e) { toast(String(e), 'error'); }
  }
  // ---- @-mention: link another clip from inside a note ------------------
  const _MIRROR_KEYS = ['fontFamily','fontSize','fontWeight','fontStyle','lineHeight','letterSpacing','textTransform','textAlign','paddingTop','paddingRight','paddingBottom','paddingLeft','borderTopWidth','borderRightWidth','borderBottomWidth','borderLeftWidth','boxSizing'] as const;
  function caretCoords(el: HTMLTextAreaElement): { top: number; left: number } {
    const cs = window.getComputedStyle(el);
    const mirror = document.createElement('div');
    for (const k of _MIRROR_KEYS) mirror.style[k] = cs[k];
    Object.assign(mirror.style, { position: 'absolute', top: '-9999px', left: '-9999px', width: el.clientWidth + 'px', whiteSpace: 'pre-wrap', wordWrap: 'break-word', overflow: 'hidden' });
    const caret = el.selectionStart ?? el.value.length;
    mirror.textContent = el.value.slice(0, caret);
    const marker = document.createElement('span');
    marker.textContent = '​';
    mirror.appendChild(marker);
    document.body.appendChild(mirror);
    const rect = el.getBoundingClientRect();
    const lh = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.4 || 18;
    const r = { top: rect.top + marker.offsetTop - el.scrollTop + lh, left: rect.left + marker.offsetLeft - el.scrollLeft };
    document.body.removeChild(mirror);
    return r;
  }
  function onMentionInput(e: Event, set: (s: string) => void) {
    const el = e.currentTarget as HTMLTextAreaElement;
    const caret = el.selectionStart ?? el.value.length;
    const m = /(?:^|[\s(])@([\w-]*)$/.exec(el.value.slice(0, caret));
    if (!m) { mention = null; return; }
    const c = caretCoords(el);
    mention = { el, queryStart: caret - m[1].length - 1, query: m[1].toLowerCase(), top: c.top, left: c.left, set };
  }
  function pickMention(clipId: number) {
    if (!mention) return;
    const { el, queryStart, query, set } = mention;
    const token = `@{${clipId}} `;
    const newText = el.value.slice(0, queryStart) + token + el.value.slice(queryStart + 1 + query.length);
    const newCaret = queryStart + token.length;
    mention = null;
    set(newText);
    setTimeout(() => { el.focus(); el.setSelectionRange(newCaret, newCaret); }, 0);
  }
  function closeMention() { mention = null; }
  function splitMentions(body: string): { text: string; id: number | null }[] {
    const out: { text: string; id: number | null }[] = [];
    const re = /@\{(\d+)\}/g;
    let last = 0, m: RegExpExecArray | null;
    while ((m = re.exec(body)) !== null) {
      if (m.index > last) out.push({ text: body.slice(last, m.index), id: null });
      const id = Number(m[1]);
      out.push({ text: detail?.mentioned_assets?.[String(id)] ?? `clip #${id}`, id });
      last = m.index + m[0].length;
    }
    if (last < body.length || out.length === 0) out.push({ text: body.slice(last), id: null });
    return out;
  }
  function showMentionPopup(e: MouseEvent, id: number, title: string) {
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    mentionPopup = { id, title, top: r.bottom + 4, left: Math.max(8, r.left) };
  }
  function hideMentionPopup() { mentionPopup = null; }
  function startEditNoteBody(n: Note) { editNoteBodyId = n.id; noteBodyDraft = n.body; mention = null; }
  async function saveNoteBody(noteId: number) {
    editNoteBodyId = null; mention = null;
    try { await api.updateNote(noteId, noteBodyDraft); await refreshDetail(); } catch (e) { toast(String(e), 'error'); }
  }
  async function addNote(ms: number, endMs: number | null = null) {
    if (!detail) return;
    const body = await promptDialog(endMs === null ? `Add a note at ${fmt(ms)}` : `Add a note for ${fmt(ms)} – ${fmt(endMs)}`,
      { multiline: true, mentions: true, placeholder: 'what happened here… (type @ to link a clip)', okLabel: 'Add note' });
    if (body === null) return;
    try { await api.addTimestampNote(detail.id, Math.floor(ms), body, endMs === null ? undefined : Math.floor(endMs)); await refreshDetail(); }
    catch (e) { toast(String(e), 'error'); }
  }
  function addNoteAtNow() { void addNote(frameNowMs()); }
  function addIntervalNote() {
    if (!selValid) { toast(`Set a selection first: press “${binding('sel_in')}” (in) and/or “${binding('sel_out')}” (out) — one is enough; the rest is taken from start/end.`, 'info'); return; }
    void addNote(effIn, effOut);
  }
  async function deleteNote(id: number) {
    try { await api.deleteNote(id); await refreshDetail(); } catch (e) { toast(String(e), 'error'); }
  }
  async function retimeNote(n: Note) {
    const start = frameNowMs();
    const end = n.end_timestamp_ms != null ? start + Math.max(1, n.end_timestamp_ms - (n.timestamp_ms ?? 0)) : undefined;
    try { await api.retimeNote(n.id, start, end); await refreshDetail(); } catch (e) { toast(String(e), 'error'); }
  }
  function parseTimeRange(s: string): { start: number; end: number | null } | null {
    const one = (x: string): number | null => {
      const segs = x.trim().split(':');
      if (segs.length < 1 || segs.length > 3) return null;
      let total = 0;
      for (const seg of segs) { const v = Number(seg.trim()); if (!isFinite(v) || v < 0) return null; total = total * 60 + v; }
      return Math.round(total * 1000);
    };
    const part = s.trim().split(/\s*[-–—]\s*/);
    if (part.length === 1) { const t = one(part[0]); return t === null ? null : { start: t, end: null }; }
    if (part.length === 2) { const a = one(part[0]), b = one(part[1]); return a === null || b === null || b <= a ? null : { start: a, end: b }; }
    return null;
  }
  async function editNoteTime(n: Note) {
    const cur = n.end_timestamp_ms != null ? `${fmt(n.timestamp_ms ?? 0)}-${fmt(n.end_timestamp_ms)}` : fmt(n.timestamp_ms ?? 0);
    const input = await promptDialog('Set the note time', { value: cur, detail: 'e.g. "1:23", "83.5" (seconds), or "1:23-1:30" for an interval', okLabel: 'Set time' });
    if (input == null) return;
    const parsed = parseTimeRange(input);
    if (parsed === null) { toast("Couldn't understand that time.", 'error'); return; }
    try { await api.retimeNote(n.id, parsed.start, parsed.end ?? undefined); await refreshDetail(); } catch (e) { toast(String(e), 'error'); }
  }
  async function addTagToNote(n: Note, tagId: number) {
    if (!detail) return;
    // convenience: tagging a moment also tags the clip (the user can still untag the clip below)
    try { await api.setNoteTags(n.id, [...n.tag_ids, tagId]); await api.applyTag(detail.id, tagId); await refreshDetail(); await loadTags(); }
    catch (e) { toast(String(e), 'error'); }
  }
  async function removeTagFromNote(n: Note, tagId: number) {
    try { await api.setNoteTags(n.id, n.tag_ids.filter((x) => x !== tagId)); await refreshDetail(); } catch (e) { toast(String(e), 'error'); }
  }
  function openTagDropdown(noteId: number) {
    tagDropdownNoteId = noteId; tagDropdownSearch = '';
    setTimeout(() => tagDropdownInput?.focus(), 0);            // input only exists once the dropdown renders
  }
  function closeTagDropdown() { tagDropdownNoteId = null; tagDropdownSearch = ''; }
  function toggleTagDropdown(noteId: number) { if (tagDropdownNoteId === noteId) closeTagDropdown(); else openTagDropdown(noteId); }
  async function pickTag(n: Note, tagId: number) { closeTagDropdown(); await addTagToNote(n, tagId); }
  function pickFirstMatch(n: Note) {
    const q = tagDropdownSearch.trim().toLowerCase();
    if (!q) return;
    const m = tags.find((t) => !n.tag_ids.includes(t.id) && t.name.toLowerCase().includes(q));
    if (m) void pickTag(n, m.id);
  }
  function onWindowPointerDown(e: PointerEvent) {
    const target = e.target as HTMLElement | null;
    if (tagDropdownNoteId !== null && !(target && (target.closest('.tag-dropdown') || target.closest('.add-tag-btn')))) closeTagDropdown();
    if (mention !== null && !(target && (target.closest('.mention-dropdown') || target === mention.el))) mention = null;
  }
  // Adding ~half a frame keeps the seek target solidly inside that frame's presentation window, so
  // millisecond rounding (in the stored timestamp, or after a trim's shift) can't land us a frame early.
  function frameNudge(): number { return Math.min(0.5 / Math.max(1, fps), 0.05); }
  function seek(ms: number) { if (videoEl) { const t = Math.max(0, ms); videoEl.currentTime = t / 1000 + frameNudge(); curMs = Math.floor(t); } }
  function nudge(seconds: number) { if (videoEl) { videoEl.currentTime = Math.max(0, videoEl.currentTime + seconds); curMs = Math.floor(videoEl.currentTime * 1000); } }
  function step(frames: number) { if (videoEl) { videoEl.pause(); nudge(frames / fps); } }
  function trackFrameTime(el: HTMLVideoElement) {
    if (typeof el.requestVideoFrameCallback !== 'function') return;
    const onFrame = (_now: number, meta: { mediaTime: number }) => {
      exactFrameTime = meta.mediaTime;
      el.requestVideoFrameCallback(onFrame);
    };
    el.requestVideoFrameCallback(onFrame);
  }
  function frameNowMs(): number {
    const haveExact = videoEl != null && typeof videoEl.requestVideoFrameCallback === 'function' && exactFrameTime > 0;
    return Math.round((haveExact ? exactFrameTime : (videoEl?.currentTime ?? 0)) * 1000);
  }
  function togglePlay() { if (videoEl) { if (videoEl.paused) void videoEl.play(); else videoEl.pause(); } }
  // The <video controls> buttons (mute / fullscreen / ...) live in the user-agent shadow DOM and grab
  // focus when clicked. Once focus is in there, the keydown event does not compose out of the shadow
  // root -- so our window onkeydown never fires, and space / Enter re-activate the focused button
  // instead. Bumping focus back to <body> after any interaction keeps the app's shortcuts in charge.
  function deflectVideoFocus() { setTimeout(() => videoEl?.blur(), 0); }
  function setIn() { selIn = frameNowMs(); if (selOut !== null && selOut <= selIn) selOut = null; }
  function setOut() { selOut = frameNowMs(); if (selIn !== null && selIn >= selOut) selIn = null; }
  function clearSel() { selIn = null; selOut = null; }
  function pct(ms: number): number { return timelineMs ? Math.max(0, Math.min(100, (ms / timelineMs) * 100)) : 0; }
  function pointerToMs(e: PointerEvent | MouseEvent): number {
    if (!timelineEl || !timelineMs) return 0;
    const r = timelineEl.getBoundingClientRect();
    const x = Math.max(0, Math.min(r.width, e.clientX - r.left));
    return r.width > 0 ? (x / r.width) * timelineMs : 0;
  }
  function startDrag(kind: 'in' | 'out', e: PointerEvent) {
    if (e.button !== 0) return;
    e.stopPropagation(); e.preventDefault();
    dragging = kind;
  }
  function timelinePointerDown(e: PointerEvent) {
    if (e.button !== 0 || !timelineMs) return;
    seek(pointerToMs(e));
    dragging = 'play';                                           // keep scrubbing while the button is held
  }
  function startNoteDrag(n: Note, e: PointerEvent) {
    if (e.button !== 0 || !timelineMs) return;
    e.stopPropagation(); e.preventDefault();
    draggingNote = { id: n.id, isInterval: n.end_timestamp_ms != null, durationMs: (n.end_timestamp_ms ?? 0) - (n.timestamp_ms ?? 0) };
    draggingNotePreviewMs = n.timestamp_ms ?? 0;
  }
  function onPointerMove(e: PointerEvent) {
    if (draggingNote && timelineMs) {                            // moving a note marker
      const ms = Math.round(pointerToMs(e));
      const maxStart = draggingNote.isInterval ? Math.max(0, timelineMs - draggingNote.durationMs) : timelineMs;
      draggingNotePreviewMs = Math.max(0, Math.min(ms, maxStart));
      return;
    }
    if (!dragging || !timelineMs) return;
    const ms = Math.round(pointerToMs(e));
    if (dragging === 'in') {
      const hi = selOut ?? timelineMs;
      selIn = Math.max(0, Math.min(ms, Math.max(0, hi - 1)));    // never cross the OUT handle
    } else if (dragging === 'out') {
      const lo = selIn ?? 0;
      selOut = Math.min(timelineMs, Math.max(ms, lo + 1));       // never cross the IN handle
    } else {
      seek(ms);
    }
  }
  function endDrag() {
    if (draggingNote) {
      const dn = draggingNote, prev = draggingNotePreviewMs;
      draggingNote = null; draggingNotePreviewMs = null; dragging = null;
      if (prev !== null && detail) {
        const orig = detail.timestamped_notes.find((n) => n.id === dn.id)?.timestamp_ms ?? 0;
        if (prev === orig) seek(orig);                           // a plain click on the marker -> seek to it
        else void api.retimeNote(dn.id, prev, dn.isInterval ? prev + dn.durationMs : undefined)
          .then(() => refreshDetail()).catch((e) => toast(String(e), 'error'));
      }
      return;
    }
    dragging = null;
  }
  async function doEdit(kind: EditKind) {
    if (!detail || !selValid || busy) return;
    const id = detail.id, s = Math.round(effIn), o = Math.round(effOut);
    const span = `${fmt(s)} – ${fmt(o)}`;
    if (kind === 'trim' && !await confirmDialog(`Trim this clip to keep only ${span}?`, { detail: 'The rest is permanently removed — there is no undo.', okLabel: 'Trim', danger: true })) return;
    if (kind === 'remove' && !await confirmDialog(`Cut ${span} out of this clip?`, { detail: 'There is no undo.', okLabel: 'Remove', danger: true })) return;
    if (kind === 'cut' && !await confirmDialog(`Save ${span} as a new clip AND cut it out of this one?`, { detail: 'The cut has no undo.', okLabel: 'Cut to new clip', danger: true })) return;
    busy = true;
    const touchesSource = kind !== 'extract';                          // 'extract' only writes a new file
    if (touchesSource && videoEl) { videoEl.pause(); videoEl.removeAttribute('src'); videoEl.load(); }  // release the file
    try {
      if (touchesSource) await new Promise<void>((r) => setTimeout(r, 250));   // let the backend close its stream handle
      if (kind === 'trim') await api.trimAsset(id, s, o);
      else if (kind === 'remove') await api.removeSegment(id, s, o);
      else { const made = await api.extractSegment(id, s, o, kind === 'cut'); toast(`Saved as a new clip: ${made.title}`, 'success'); }
      clearSel();
      await refreshDetail();
      await loadAssets();
    } catch (e) { toast(String(e), 'error'); } finally { videoVersion += 1; busy = false; }
  }

  function openSettings() {
    if (!cfg) return;
    pendingEditing = { ...cfg.editing };
    pendingPlayer = { ...cfg.player };
    pendingKeybindings = { ...cfg.keybindings };
    capturingAction = null; settingsError = null; settingsTab = 'editing';
    showSettings = true;
  }
  function closeSettings() {
    showSettings = false; capturingAction = null;
    pendingEditing = null; pendingPlayer = null; pendingKeybindings = null; settingsError = null;
  }
  function startCapture(action: string) { capturingAction = capturingAction === action ? null : action; }
  async function saveSettings() {
    if (!pendingEditing || !pendingPlayer || !pendingKeybindings || savingSettings) return;
    savingSettings = true; settingsError = null;
    try {
      cfg = await api.updateConfig({ editing: pendingEditing, player: pendingPlayer, keybindings: pendingKeybindings });
      closeSettings();
    } catch (e) { settingsError = String(e); }
    finally { savingSettings = false; }
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
    // (1) keybinding capture eats the next non-modifier press (Esc cancels)
    if (capturingAction !== null) {
      e.preventDefault(); e.stopPropagation();
      if (e.key === 'Escape') { capturingAction = null; return; }
      if (e.key === 'Control' || e.key === 'Shift' || e.key === 'Alt' || e.key === 'Meta') return;
      if (pendingKeybindings) pendingKeybindings[capturingAction] = keyName(e);
      capturingAction = null;
      return;
    }
    // (2) an in-app dialog: Esc cancels it, Enter confirms it
    if (dialog !== null) {
      if (e.key === 'Escape') { e.preventDefault(); dialogCancel(); }
      else if (e.key === 'Enter' && dialog.kind === 'confirm') { e.preventDefault(); dialogOk(); }
      return;
    }
    // (3) Esc always closes the Settings modal, even from a focused input
    if (showSettings && e.key === 'Escape') { closeSettings(); return; }
    if (inEditable(e.target)) return;
    // (3) while Settings is open, don't fire app shortcuts behind it
    if (showSettings) return;
    if (!detail) {                                          // grid view
      if (e.key === 'Escape' && selected.size > 0) clearSelection();
      else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a' && assets.length > 0) { e.preventDefault(); selectAllVisible(); }
      return;
    }
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

<svelte:window onkeydown={onKey} onpointermove={onPointerMove} onpointerup={endDrag} onpointercancel={endDrag} onpointerdown={onWindowPointerDown} />

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
    <button class="btn sm" onclick={openSettings} disabled={!cfg} title="Settings">⚙ Settings</button>
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
            {@render tagFace(t)} {t.name} <span class="n">{t.asset_count ?? 0}</span>
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
      {#if selected.size > 0}
        <div class="bulkbar">
          <b>{selected.size}</b> selected
          <button class="btn sm" onclick={selectAllVisible}>all ({assets.length})</button>
          <button class="btn sm" onclick={clearSelection}>clear</button>
          <span style:flex="1"></span>
          <label>+ tag <select bind:value={bulkAddTag} disabled={bulkBusy} onchange={() => { if (bulkAddTag) { bulkApplyTag(Number(bulkAddTag)); bulkAddTag = ''; } }}><option value="">—</option>{#each tags as t (t.id)}<option value={t.id}>{@render tagFace(t)} {t.name}</option>{/each}</select></label>
          <label>− tag <select bind:value={bulkRemoveTag} disabled={bulkBusy} onchange={() => { if (bulkRemoveTag) { bulkRemoveTagFn(Number(bulkRemoveTag)); bulkRemoveTag = ''; } }}><option value="">—</option>{#each tags as t (t.id)}<option value={t.id}>{@render tagFace(t)} {t.name}</option>{/each}</select></label>
          <button class="btn sm" disabled={bulkBusy} onclick={bulkDelete}>🗑 delete {selected.size}</button>
        </div>
      {/if}
      <div class="bar"><b>{total}</b> clips{loading ? ' · loading…' : ''}{filterTagIds.length ? ' · filtered by ' + filterTagIds.length + ' tag(s)' : ''}</div>
      <div class="grid">
        {#each assets as a (a.id)}
          <button class="card" class:sel={selected.has(a.id)} onclick={(e) => cardClick(a, e)}>
            <div class="thumb">
              <img src={a.thumbnail_url} alt="" onerror={hideBrokenImg} />
              <span class="film">▶</span>
              <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
              <span class="selbox" class:on={selected.has(a.id)} onclick={(e) => { e.stopPropagation(); toggleSelect(a); }} title="select (Ctrl-click / Shift-click range)">{selected.has(a.id) ? '✓' : ''}</span>
              {#if durationOf(a.metadata)}<span class="dur">{durationOf(a.metadata)}</span>{/if}
              {#if a.is_new}<span class="badge bnew">new</span>{/if}
              {#if a.note_count}<span class="badge">📝 {a.note_count}</span>{/if}
            </div>
            <div class="cb">
              <div class="ct" title={a.title}>{a.title}</div>
              <div class="tags">
                {#each a.tag_ids as id (id)}
                  {@const t = tagById.get(id)}
                  {#if t}<span class="pill" style:background={t.color}>{@render tagFace(t)} {t.name}</span>{/if}
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
      {#if editingTitle}
        <input class="field" style:max-width="320px" bind:value={titleDraft} bind:this={titleInputEl}
               onkeydown={(e) => { if (e.key === 'Enter') saveTitle(); else if (e.key === 'Escape') editingTitle = false; }} onblur={saveTitle} />
      {:else}
        <button class="otitle" onclick={startEditTitle} title="rename — the displayed title (the file on disk isn't touched)">{d.title} <span class="faint" style:font-size="11px">✎</span></button>
      {/if}
      <span style:flex="1"></span>
      <button class="btn sm" onclick={deleteCurrentClip} title="Delete this clip and its file from disk">🗑 Delete clip</button>
      <button class="btn sm" onclick={() => (showKeys = true)} title="Keyboard shortcuts">⌨ Keys</button>
    </div>
    <div class="obody">
      <div class="player">
        <video bind:this={videoEl} src={`${d.stream_url}?v=${videoVersion}`} controls
               onpointerdown={deflectVideoFocus} onfocusin={deflectVideoFocus}
               ontimeupdate={() => { if (videoEl) curMs = Math.floor(videoEl.currentTime * 1000); }}
               onloadedmetadata={() => { if (videoEl && Number.isFinite(videoEl.duration)) durMs = Math.floor(videoEl.duration * 1000); }}></video>
        <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
        <div class="timeline" bind:this={timelineEl} class:dragging onpointerdown={timelinePointerDown} title="Click or drag to seek; drag the IN / OUT handles to adjust">
          {#if selValid}<div class="sel" style:left={pct(effIn) + '%'} style:width={Math.max(0, pct(effOut) - pct(effIn)) + '%'}></div>{/if}
          {#each d.timestamped_notes as n (n.id)}
            {@const ndrag = draggingNote?.id === n.id && draggingNotePreviewMs !== null}
            {@const ns = ndrag ? (draggingNotePreviewMs as number) : (n.timestamp_ms ?? 0)}
            {#if n.end_timestamp_ms != null}
              {@const ne = ndrag ? (draggingNotePreviewMs as number) + (draggingNote?.durationMs ?? 0) : n.end_timestamp_ms}
              <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
              <div class="nbar" class:ndrag onpointerdown={(e) => startNoteDrag(n, e)} style:left={pct(ns) + '%'} style:width={Math.max(0.7, pct(ne) - pct(ns)) + '%'} title="{n.body} — drag to move"></div>
            {:else}
              <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
              <div class="ntick" class:ndrag onpointerdown={(e) => startNoteDrag(n, e)} style:left={pct(ns) + '%'} title="{n.body} — drag to move"></div>
            {/if}
          {/each}
          <div class="playhead" style:left={pct(curMs) + '%'}></div>
          {#if selIn !== null}
            <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
            <div class="handle in" style:left={pct(selIn) + '%'} onpointerdown={(e) => startDrag('in', e)} title="drag to adjust IN ({fmt(selIn)})"></div>
          {/if}
          {#if selOut !== null}
            <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
            <div class="handle out" style:left={pct(selOut) + '%'} onpointerdown={(e) => startDrag('out', e)} title="drag to adjust OUT ({fmt(selOut)})"></div>
          {/if}
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
            <span class="seltext">selection {fmt(effIn)}{selIn === null ? ' (start)' : ''} – {fmt(effOut)}{selOut === null ? ' (end)' : ''}{selValid ? ` · ${fmt(effOut - effIn)}` : ''}</span>
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
            {#if t}<span class="pill" style:background={t.color}>{@render tagFace(t)} {t.name} <button class="x" onclick={() => unapplyTag(t.id)}>×</button></span>{/if}
          {/each}
          {#if d.tag_ids.length === 0}<span class="faint">no tags</span>{/if}
        </div>
        <div class="tagcloud" style:margin-top="6px">
          {#each tags.filter((t) => !d.tag_ids.includes(t.id)) as t (t.id)}
            <button class="tagchip" onclick={() => applyTag(t.id)}>+ {@render tagFace(t)} {t.name}</button>
          {/each}
        </div>
        <h4>General note</h4>
        {#if editingGeneralNote}
          <textarea class="field" rows="5" bind:value={generalNoteText} bind:this={generalNoteEl}
                    oninput={(e) => onMentionInput(e, (s) => { generalNoteText = s; })}
                    onkeydown={(e) => { if (e.key === 'Escape') { if (mention) closeMention(); else { editingGeneralNote = false; void saveGeneralNote(); } } }}
                    onblur={() => { if (!mention) { editingGeneralNote = false; void saveGeneralNote(); } }}></textarea>
        {:else}
          <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
          <div class="note-display" onclick={() => { editingGeneralNote = true; setTimeout(() => generalNoteEl?.focus(), 0); }} title="click to edit — type @ to link another clip">
            {#if generalNoteText.trim()}{@render noteBody(generalNoteText)}{:else}<span class="faint">add a note… (type @ to link a clip)</span>{/if}
          </div>
        {/if}
        <h4>Timestamped notes</h4>
        {#each d.timestamped_notes as n (n.id)}
          {@const available = tags.filter((t) => !n.tag_ids.includes(t.id))}
          <div class="tsn">
            <button class="ts" onclick={() => seek(n.timestamp_ms ?? 0)} title="seek here">{n.end_timestamp_ms != null ? `${fmt(n.timestamp_ms ?? 0)}–${fmt(n.end_timestamp_ms)}` : fmt(n.timestamp_ms ?? 0)}</button>
            {#if available.length > 0}
              <div class="tagpicker">
                <button class="add-tag-btn" onclick={() => toggleTagDropdown(n.id)} title="add a tag">🏷</button>
                {#if tagDropdownNoteId === n.id}
                  {@const matches = available.filter((t) => t.name.toLowerCase().includes(tagDropdownSearch.toLowerCase()))}
                  <div class="tag-dropdown">
                    <input class="field tag-dd-search" bind:value={tagDropdownSearch} bind:this={tagDropdownInput}
                           placeholder="search tags…"
                           onkeydown={(e) => { if (e.key === 'Escape') closeTagDropdown(); else if (e.key === 'Enter') pickFirstMatch(n); }} />
                    <div class="tag-dd-list">
                      {#each matches as t (t.id)}
                        <button class="tag-dd-item" onclick={() => pickTag(n, t.id)}><span class="sw" style:background={t.color}></span> {@render tagFace(t)} {t.name}</button>
                      {/each}
                      {#if matches.length === 0}<div class="faint" style:padding="8px 10px" style:font-size="12px">no matches</div>{/if}
                    </div>
                  </div>
                {/if}
              </div>
            {/if}
            {#if editNoteBodyId === n.id}
              <textarea class="field notebody-edit" rows="2" bind:value={noteBodyDraft}
                        oninput={(e) => onMentionInput(e, (s) => { noteBodyDraft = s; })}
                        onkeydown={(e) => { if (e.key === 'Escape') { if (mention) closeMention(); else editNoteBodyId = null; } else if (e.key === 'Enter' && !e.shiftKey && !mention) { e.preventDefault(); void saveNoteBody(n.id); } }}></textarea>
              <button class="x" onclick={() => saveNoteBody(n.id)} title="save">✔</button>
              <button class="x" onclick={() => (editNoteBodyId = null)} title="cancel">×</button>
            {:else}
              <span class="body">{@render noteBody(n.body)}</span>
              <button class="x" onclick={() => startEditNoteBody(n)} title="edit the text — type @ to link a clip">📝</button>
              <button class="x" onclick={() => retimeNote(n)} title="move to the current playhead ({fmt(curMs)})">↻</button>
              <button class="x" onclick={() => editNoteTime(n)} title="type a new time">✏</button>
              <button class="x" onclick={() => deleteNote(n.id)} title="delete">🗑</button>
            {/if}
          </div>
          {#if n.tag_ids.length > 0}
            <div class="tsntags">
              {#each n.tag_ids as id (id)}
                {@const t = tagById.get(id)}
                {#if t}<span class="pill" style:background={t.color}>{@render tagFace(t)} {t.name} <button class="x" onclick={() => removeTagFromNote(n, t.id)}>×</button></span>{/if}
              {/each}
            </div>
          {/if}
        {/each}
        {#if d.timestamped_notes.length === 0}<span class="faint">none yet — use “+ note @ now”</span>{/if}
        <h4>References</h4>
        {#each [...refs.outgoing.map((r) => ({ r, dir: '→' })), ...refs.incoming.map((r) => ({ r, dir: '←' }))] as { r, dir } (dir + r.id)}
          <div class="refrow">
            <span class="refdir">{dir}</span>
            {#if r.type_name}<span class="reftype">{r.type_name}</span>{/if}
            <button class="reflink" onclick={() => openClip(r.other_asset_id)} title="open this clip">{r.other_asset_title}</button>
            {#if r.from_timestamp_ms != null}<span class="faint" style:font-size="10.5px">@ {fmt(r.from_timestamp_ms)}</span>{/if}
            <span style:flex="1"></span>
            <button class="x" onclick={() => deleteRef(r.id)} title="remove this reference">×</button>
          </div>
        {/each}
        {#if refs.outgoing.length === 0 && refs.incoming.length === 0 && !addingRef}<span class="faint">no references yet — add one below, or @-mention a clip inside a note.</span>{/if}
        {#if addingRef}
          <div class="refadd">
            <input class="field" placeholder="search clips…" bind:value={refClipQuery} />
            <select bind:value={refTypeId}><option value="">— relation —</option>{#each refTypes as rt (rt.id)}<option value={rt.id}>{rt.name}</option>{/each}</select>
            <button class="btn sm" onclick={() => { addingRef = false; refClipQuery = ''; }}>Cancel</button>
            <div class="refadd-list">
              {#each assets.filter((a) => a.id !== d.id && a.title.toLowerCase().includes(refClipQuery.toLowerCase())).slice(0, 14) as a (a.id)}
                <button class="refadd-item" onclick={() => addRef(a.id)}><img src={a.thumbnail_url} alt="" onerror={hideBrokenImg} /> {a.title}</button>
              {/each}
              {#if assets.filter((a) => a.id !== d.id && a.title.toLowerCase().includes(refClipQuery.toLowerCase())).length === 0}<div class="faint" style:padding="6px 8px" style:font-size="12px">no matching clips loaded</div>{/if}
            </div>
          </div>
        {:else}
          <button class="btn sm" style:margin-top="6px" onclick={() => { addingRef = true; refClipQuery = ''; }}>+ Add a reference</button>
        {/if}
        <h4>File</h4>
        {#each d.paths as p (p.path)}<div class="src" class:miss={!p.present} title={p.path}>{p.present ? '✓' : '✗'} {p.path}</div>{/each}
        <button class="btn sm" style:margin-top="7px" onclick={renameFile}>✎ Rename the file on disk</button>
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

{#snippet tagFace(t: Tag)}{#if t.image_ref}<img class="tagimg" src="/api/tag-images/{t.image_ref}" alt="" />{:else if t.icon}{t.icon}{/if}{/snippet}
{#snippet noteBody(body: string)}{#each splitMentions(body) as seg}{#if seg.id != null}<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<span class="mention" role="link" tabindex="0" onclick={() => openClip(seg.id ?? 0)} onmouseenter={(e) => showMentionPopup(e, seg.id ?? 0, seg.text)} onmouseleave={hideMentionPopup}>@{seg.text}</span>{:else}{seg.text}{/if}{/each}{/snippet}
{#if showSettings && cfg}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="modal-bg" onclick={(e) => { if (e.target === e.currentTarget) closeSettings(); }}>
    <div class="modal settings">
      <div class="mtop"><h3>Settings</h3><span style:flex="1"></span><button class="btn sm" onclick={closeSettings}>Close</button></div>
      <div class="settings-tabs">
        <button class:on={settingsTab === 'editing'} onclick={() => (settingsTab = 'editing')}>Editing</button>
        <button class:on={settingsTab === 'player'} onclick={() => (settingsTab = 'player')}>Player</button>
        <button class:on={settingsTab === 'keys'} onclick={() => (settingsTab = 'keys')}>Keyboard</button>
      </div>
      <div class="mbody">
        {#if settingsTab === 'editing' && pendingEditing}
          <label class="srow"><span class="slabel">Re-encode on trim / cut <span class="faint" style:font-size="11px">(frame-accurate; slower)</span></span>
            <input type="checkbox" bind:checked={pendingEditing.reencode} /></label>
          <label class="srow"><span class="slabel">Re-encode CRF <span class="faint" style:font-size="11px">(0 best — 51 worst)</span></span>
            <input type="number" min="0" max="51" bind:value={pendingEditing.reencode_crf} /></label>
          <label class="srow"><span class="slabel">Re-encode preset</span>
            <select bind:value={pendingEditing.reencode_preset}>
              {#each ['ultrafast','superfast','veryfast','faster','fast','medium','slow','slower','veryslow'] as p}<option value={p}>{p}</option>{/each}
            </select></label>
          <label class="srow"><span class="slabel">Keep a backup of the pre-edit file</span>
            <input type="checkbox" bind:checked={pendingEditing.keep_original_backup} /></label>
          <label class="srow"><span class="slabel">New-clip name template <span class="faint" style:font-size="11px">{`{stem} {start} {end} {ext}`}</span></span>
            <input class="field" type="text" bind:value={pendingEditing.new_clip_name_template} /></label>
          <label class="srow"><span class="slabel">Excerpt reference type</span>
            <input class="field" type="text" bind:value={pendingEditing.excerpt_reference_type} /></label>
          <p class="faint" style:font-size="12px" style:margin-top="10px">All editing settings apply right away — on the very next trim / cut / extract.</p>
        {:else if settingsTab === 'player' && pendingPlayer}
          <label class="srow"><span class="slabel">Skip seconds <span class="faint" style:font-size="11px">(skip back / forward buttons)</span></span>
            <input type="number" step="0.5" min="0.1" bind:value={pendingPlayer.skip_seconds} /></label>
          <label class="srow"><span class="slabel">Skip seconds — fine <span class="faint" style:font-size="11px">(Shift + arrow)</span></span>
            <input type="number" step="0.5" min="0.1" bind:value={pendingPlayer.skip_seconds_fine} /></label>
          <label class="srow"><span class="slabel">Pause when adding a note</span>
            <input type="checkbox" bind:checked={pendingPlayer.pause_on_add_note} /></label>
          <p class="faint" style:font-size="12px" style:margin-top="10px">Player settings apply right away.</p>
        {:else if settingsTab === 'keys' && pendingKeybindings}
          <p class="faint" style:font-size="12px" style:margin-bottom="8px">Click a binding and press the new key combination. Esc cancels the capture.</p>
          {#each SHORTCUT_ROWS as [action, label] (action)}
            <div class="srow kbrow">
              <span class="slabel">{label}</span>
              <button class="kbd-edit" class:capturing={capturingAction === action} onclick={() => startCapture(action)}>
                {capturingAction === action ? 'press a key…' : (pendingKeybindings[action] || '—')}
              </button>
            </div>
          {/each}
        {/if}
        {#if settingsError}<div class="err" style:margin-top="10px">{settingsError}</div>{/if}
      </div>
      <div class="mfoot">
        <span style:flex="1"></span>
        <button class="btn sm" onclick={closeSettings}>Cancel</button>
        <button class="btn sm primary" onclick={saveSettings} disabled={savingSettings}>{savingSettings ? 'Saving…' : 'Save'}</button>
      </div>
    </div>
  </div>
{/if}

{#if showTags}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="modal-bg" onclick={(e) => { if (e.target === e.currentTarget) { showTags = false; cancelEditTag(); } }}>
    <div class="modal">
      <div class="mtop"><h3>Tags</h3><span style:flex="1"></span><button class="btn sm" onclick={() => { showTags = false; cancelEditTag(); }}>Close</button></div>
      <div class="mbody">
        {#each tags as t (t.id)}
          <div class="trow" class:editing={editingTagId === t.id}><span class="sw" style:background={t.color}></span> <b>{@render tagFace(t)} {t.name}</b> <span class="faint" style:font-size="11px">{t.asset_count ?? 0} clips</span><span style:flex="1"></span><button class="x" onclick={() => startEditTag(t)} title="edit">✎</button><button class="x" onclick={() => deleteTag(t.id)} title="delete">🗑</button></div>
        {/each}
        <div class="trow" style:margin-top="8px">
          <input class="field" style:max-width="146px" placeholder={editingTagId === null ? 'new tag name' : 'name'} bind:value={newTagName} />
          <input type="color" bind:value={newTagColor} title="colour" />
          <input class="field" style:max-width="54px" placeholder="icon" maxlength="8" bind:value={tagIcon} disabled={tagImageRef !== null} title="an emoji shown before the name" />
          <label class="btn sm" title="upload an image to use as the icon (a copy is kept by the app)">{uploadingImg ? '…' : '📷'}<input type="file" accept="image/png,image/jpeg,image/webp,image/gif" onchange={pickTagImage} style:display="none" /></label>
          {#if tagImageRef}<img class="tagimg tagimg-prev" src="/api/tag-images/{tagImageRef}" alt="" /><button class="x" onclick={removeTagImage} title="remove the image">×</button>{/if}
          <button class="btn sm primary" onclick={saveTag}>{editingTagId === null ? '+ Create' : '💾 Save'}</button>
          {#if editingTagId !== null}<button class="btn sm" onclick={cancelEditTag}>Cancel</button>{/if}
        </div>
        <p class="faint" style:margin-top="10px" style:font-size="12px">Tags are flat and yours: name + colour + either an emoji or an uploaded image as the icon. Click ✎ to edit a tag; 🗑 deletes it everywhere. Uploaded images are copied into the app's data folder, so deleting the original file is fine.</p>
      </div>
    </div>
  </div>
{/if}

{#if dialog}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="modal-bg" onclick={(e) => { if (e.target === e.currentTarget) dialogCancel(); }}>
    <div class="modal dlg">
      <div class="dlg-msg">{dialog.message}</div>
      {#if dialog.detail}<div class="dlg-detail">{dialog.detail}</div>{/if}
      {#if dialog.kind === 'prompt'}
        {#if dialog.multiline}
          <textarea class="field dlg-input" rows="3" bind:value={dialog.value} bind:this={dlgInputEl} placeholder={dialog.placeholder}
                    oninput={(e) => { if (dialog?.mentions) onMentionInput(e, (s) => { if (dialog) dialog.value = s; }); }}
                    onkeydown={(e) => { if (e.key === 'Escape') { e.stopPropagation(); if (mention) closeMention(); else dialogCancel(); } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !mention) { e.preventDefault(); dialogOk(); } }}></textarea>
        {:else}
          <input class="field dlg-input" bind:value={dialog.value} bind:this={dlgInputEl} placeholder={dialog.placeholder}
                 onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); dialogOk(); } else if (e.key === 'Escape') { e.stopPropagation(); dialogCancel(); } }} />
        {/if}
      {/if}
      <div class="dlg-btns">
        {#if dialog.multiline}<span class="faint" style:font-size="11px">Ctrl+Enter to save</span>{/if}
        <span style:flex="1"></span>
        <button class="btn sm" onclick={dialogCancel}>Cancel</button>
        <button class="btn sm" class:primary={!dialog.danger} class:danger={dialog.danger} onclick={dialogOk}>{dialog.okLabel}</button>
      </div>
    </div>
  </div>
{/if}
{#if toasts.length}
  <div class="toasts">
    {#each toasts as t (t.id)}
      <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
      <div class="toast {t.kind}" onclick={() => (toasts = toasts.filter((x) => x.id !== t.id))} title="dismiss">{t.text}</div>
    {/each}
  </div>
{/if}
{#if mention}
  {@const matches = assets.filter((a) => a.title.toLowerCase().includes(mention.query)).slice(0, 10)}
  <div class="mention-dropdown" style:top={mention.top + 'px'} style:left={mention.left + 'px'}>
    {#each matches as a (a.id)}
      <button class="mention-item" onmousedown={(e) => { e.preventDefault(); pickMention(a.id); }}><img src={a.thumbnail_url} alt="" onerror={hideBrokenImg} /><span>{a.title}</span></button>
    {/each}
    {#if matches.length === 0}<div class="faint" style:padding="6px 8px" style:font-size="12px">no matching clips</div>{/if}
  </div>
{/if}
{#if mentionPopup}
  <div class="mention-popup" style:top={mentionPopup.top + 'px'} style:left={mentionPopup.left + 'px'}>
    <img src="/thumbnails/{mentionPopup.id}" alt="" onerror={hideBrokenImg} />
    <div class="mention-popup-title">{mentionPopup.title}</div>
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
  .tagchip.on { color: #f7f9fb; border-color: transparent; text-shadow: -1px -1px 0 rgba(0,0,0,.72), 1px -1px 0 rgba(0,0,0,.72), -1px 1px 0 rgba(0,0,0,.72), 1px 1px 0 rgba(0,0,0,.72); }
  .tagchip .n { font-family: ui-monospace, monospace; font-size: 10px; opacity: .6; }
  .src { font-family: ui-monospace, monospace; font-size: 11px; color: var(--text-2); padding: 3px 0; word-break: break-all; }
  .src.miss { color: var(--text-3); text-decoration: line-through; }
  .refrow { display: flex; align-items: center; gap: 6px; padding: 4px 0; font-size: 12.5px; }
  .refdir { color: var(--text-3); font-weight: 700; flex: none; }
  .reftype { font-size: 10.5px; color: var(--text-3); background: var(--bg-3); border-radius: 4px; padding: 1px 5px; white-space: nowrap; flex: none; }
  .reflink { background: transparent; border: none; color: var(--accent); cursor: pointer; text-align: left; padding: 0; text-decoration: underline; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .reflink:hover { color: #ffb482; }
  .refadd { margin-top: 6px; padding: 8px; background: var(--bg-2); border: 1px solid var(--border); border-radius: 7px; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .refadd input.field { flex: 1; min-width: 110px; }
  .refadd select { padding: 3px 6px; background: var(--bg-1); color: var(--text); border: 1px solid var(--border); border-radius: 5px; }
  .refadd-list { width: 100%; max-height: 210px; overflow-y: auto; display: flex; flex-direction: column; gap: 2px; }
  .refadd-item { display: flex; align-items: center; gap: 8px; width: 100%; text-align: left; padding: 4px 6px; border-radius: 5px; font-size: 12px; color: var(--text); background: transparent; border: none; cursor: pointer; }
  .refadd-item:hover { background: var(--bg-3); }
  .refadd-item img { width: 40px; height: 24px; object-fit: cover; border-radius: 3px; flex: none; background: #11141a; }
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
  .pill { color: #f7f9fb; text-shadow: -1px -1px 0 rgba(0,0,0,.72), 1px -1px 0 rgba(0,0,0,.72), -1px 1px 0 rgba(0,0,0,.72), 1px 1px 0 rgba(0,0,0,.72); }
  .pill .x { font-weight: 800; }
  .overlay { position: fixed; inset: 0; background: var(--bg); display: flex; flex-direction: column; z-index: 50; }
  .otop { display: flex; align-items: center; gap: 12px; padding: 0 14px; height: 48px; background: var(--bg-1); border-bottom: 1px solid var(--border); flex: none; }
  .otitle { font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  button.otitle { background: transparent; border: none; color: inherit; cursor: pointer; padding: 0; font: inherit; font-weight: 700; text-align: left; }
  button.otitle:hover { color: var(--accent); }
  .timeline .ntick, .timeline .nbar { cursor: grab; }
  .timeline .ntick::before { content: ''; position: absolute; inset: 0 -5px; }   /* fatter hit area for grabbing the tick */
  .timeline .ntick.ndrag, .timeline .nbar.ndrag { opacity: .85; z-index: 3; box-shadow: 0 0 0 1px rgba(255,255,255,.45); cursor: grabbing; }
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
  .tsn .body { flex: 1; font-size: 13px; word-break: break-word; }
  .note-display { white-space: pre-wrap; word-break: break-word; font-size: 13px; min-height: 44px; padding: 7px 9px; background: var(--bg-2); border: 1px solid var(--border); border-radius: 7px; cursor: text; line-height: 1.45; }
  .note-display:hover { border-color: #3a4350; }
  .mention { color: var(--accent); cursor: pointer; text-decoration: underline; font-weight: 600; }
  .mention:hover { color: #ffb482; }
  .notebody-edit { flex: 1; min-width: 110px; font-size: 12px; resize: vertical; }
  .mention-dropdown { position: fixed; z-index: 70; width: 240px; max-height: 240px; overflow-y: auto; background: var(--bg-1); border: 1px solid var(--border); border-radius: 8px; box-shadow: 0 6px 22px rgba(0,0,0,.5); padding: 4px; }
  .mention-item { display: flex; align-items: center; gap: 8px; width: 100%; text-align: left; padding: 4px 6px; border-radius: 5px; font-size: 12px; color: var(--text); background: transparent; border: none; cursor: pointer; }
  .mention-item:hover { background: var(--bg-3); }
  .mention-item img { width: 38px; height: 22px; object-fit: cover; border-radius: 3px; flex: none; background: #11141a; }
  .mention-item span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mention-popup { position: fixed; z-index: 75; pointer-events: none; background: var(--bg-1); border: 1px solid var(--border); border-radius: 8px; box-shadow: 0 6px 22px rgba(0,0,0,.55); padding: 6px; width: 200px; }
  .mention-popup img { width: 100%; aspect-ratio: 16/9; object-fit: cover; border-radius: 5px; background: #11141a; display: block; }
  .mention-popup-title { font-size: 12px; font-weight: 600; margin-top: 5px; word-break: break-word; }
  .toasts { position: fixed; bottom: 16px; right: 16px; z-index: 90; display: flex; flex-direction: column; gap: 8px; max-width: 360px; }
  .toast { background: var(--bg-1); border: 1px solid var(--border); border-left: 3px solid var(--text-3); border-radius: 8px; padding: 9px 13px; font-size: 12.5px; box-shadow: 0 6px 22px rgba(0,0,0,.5); cursor: pointer; animation: toastin .18s ease; word-break: break-word; line-height: 1.4; }
  .toast.error { border-left-color: #ef5b5b; }
  .toast.success { border-left-color: #56c271; }
  .toast.info { border-left-color: var(--accent); }
  @keyframes toastin { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: none; } }
  .dlg { width: min(420px, 100%); padding: 18px; }
  .dlg-msg { font-size: 14.5px; font-weight: 600; line-height: 1.4; }
  .dlg-detail { font-size: 12.5px; color: var(--text-2); margin-top: 7px; line-height: 1.45; }
  .dlg-input { width: 100%; margin-top: 13px; box-sizing: border-box; }
  textarea.dlg-input { resize: vertical; }
  .dlg-btns { display: flex; align-items: center; gap: 8px; margin-top: 16px; }
  .btn.danger { background: #b3261e; border-color: #c0392b; color: #fff; }
  .btn.danger:hover { background: #c0392b; }
  .tsntags { display: flex; flex-wrap: wrap; gap: 4px; margin: -2px 0 9px; padding-left: 2px; }
  .tagchip.ntag { font-size: 10px; padding: 1px 6px; }
  .tagpicker { position: relative; display: inline-block; }
  .add-tag-btn { background: transparent; border: none; padding: 0 4px; font-size: 13px; line-height: 1; cursor: pointer; color: var(--text-3); }
  .add-tag-btn:hover { color: var(--accent); }
  .tag-dropdown { position: absolute; top: 100%; left: 0; margin-top: 4px; z-index: 30; width: 220px; max-height: 250px;
                  background: var(--bg-1); border: 1px solid var(--border); border-radius: 8px;
                  box-shadow: 0 6px 22px rgba(0,0,0,.5); display: flex; flex-direction: column; overflow: hidden; }
  .tag-dd-search { margin: 6px 6px 0; padding: 4px 7px; font-size: 12px; }
  .tag-dd-list { overflow-y: auto; padding: 4px; flex: 1; min-height: 0; }
  .tag-dd-item { display: flex; align-items: center; gap: 6px; width: 100%; text-align: left;
                 padding: 5px 7px; border-radius: 5px; font-size: 12px; color: var(--text);
                 background: transparent; border: none; cursor: pointer; }
  .tag-dd-item:hover { background: var(--bg-2); }
  .tag-dd-item .sw { width: 10px; height: 10px; border-radius: 3px; border: 1px solid rgba(255,255,255,.13); flex: none; }
  .card.sel { outline: 2.5px solid var(--accent); outline-offset: -2px; }
  .selbox { position: absolute; left: 6px; top: 6px; width: 18px; height: 18px; border-radius: 4px; border: 2px solid rgba(255,255,255,.55); background: rgba(0,0,0,.45); display: grid; place-items: center; font-size: 12px; font-weight: 900; color: #fff; opacity: 0; pointer-events: none; transition: opacity .1s; }
  .thumb:hover .selbox, .selbox.on { opacity: 1; pointer-events: auto; }
  .selbox.on { background: var(--accent); border-color: var(--accent); color: #1a0e07; }
  .bulkbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 7px 11px; margin-bottom: 10px; background: var(--accent-soft); border: 1px solid var(--accent); border-radius: 8px; font-size: 13px; }
  .bulkbar label { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; }
  .bulkbar select { background: var(--bg-2); color: inherit; border: 1px solid var(--border); border-radius: 5px; padding: 2px 4px; }
  .x { color: var(--text-3); }
  .x:hover { color: #ef5b5b; }
  .modal-bg { position: fixed; inset: 0; background: rgba(4,6,9,.66); display: grid; place-items: center; z-index: 60; padding: 30px; }
  .modal { width: min(560px, 100%); max-height: 80vh; background: var(--bg-1); border: 1px solid var(--border); border-radius: var(--r); display: flex; flex-direction: column; overflow: hidden; }
  .mtop { display: flex; align-items: center; padding: 12px 16px; border-bottom: 1px solid var(--border); }
  .mtop h3 { font-size: 15px; }
  .mbody { padding: 14px 16px; overflow-y: auto; }
  .trow { display: flex; align-items: center; gap: 8px; padding: 6px 0; }
  .trow .sw { width: 16px; height: 16px; border-radius: 4px; border: 2px solid rgba(255,255,255,.13); flex: none; }
  .trow.editing { background: var(--accent-soft); border-radius: 6px; padding-left: 6px; }
  .tagimg { width: 14px; height: 14px; object-fit: cover; border-radius: 3px; vertical-align: -2px; display: inline-block; }
  .tagimg-prev { width: 26px; height: 26px; }
  .btn:disabled { opacity: .5; cursor: default; }
  .timeline { position: relative; height: 16px; background: var(--bg-2); border-radius: 5px; cursor: pointer; flex: none; overflow: hidden; }
  .timeline .sel { position: absolute; top: 0; bottom: 0; background: rgba(255,106,43,.22); border-left: 2px solid var(--accent); border-right: 2px solid var(--accent); }
  .timeline .nbar { position: absolute; top: 3px; bottom: 3px; background: rgba(240,179,79,.55); border-radius: 2px; }
  .timeline .ntick { position: absolute; top: 0; bottom: 0; width: 2px; margin-left: -1px; background: var(--amber); }
  .timeline .playhead { position: absolute; top: -2px; bottom: -2px; width: 2px; margin-left: -1px; background: #fff; box-shadow: 0 0 5px rgba(255,255,255,.7); pointer-events: none; }
  .timeline { touch-action: none; }
  .timeline .handle { position: absolute; top: 0; bottom: 0; width: 10px; margin-left: -5px; background: var(--accent); border-radius: 3px; cursor: ew-resize; z-index: 2; box-shadow: 0 0 0 1px rgba(0,0,0,.45); display: grid; place-items: center; touch-action: none; }
  .timeline .handle::after { content: ''; width: 2px; height: 60%; background: rgba(0,0,0,.55); border-radius: 1px; }
  .timeline .handle:hover { background: #ffb482; }
  .timeline.dragging { cursor: ew-resize; }
  .trim { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; }
  .trim .seltext { font-family: ui-monospace, monospace; font-size: 11.5px; font-weight: 700; color: var(--amber); }
  .pctrl .time { font-family: ui-monospace, monospace; font-size: 11.5px; color: var(--text-2); }
  .keys { border-collapse: collapse; width: 100%; }
  .keys td { padding: 3px 12px 3px 0; font-size: 12.5px; vertical-align: top; }
  kbd { font-family: ui-monospace, monospace; font-size: 11px; background: var(--bg-3); border: 1px solid var(--border); border-radius: 4px; padding: 1px 6px; white-space: nowrap; }
  .modal.settings { width: min(640px, 100%); }
  .settings-tabs { display: flex; gap: 4px; padding: 0 14px; border-bottom: 1px solid var(--border); background: var(--bg-1); }
  .settings-tabs button { padding: 8px 12px; background: transparent; border: none; color: var(--text-2); border-bottom: 2px solid transparent; cursor: pointer; font-size: 12.5px; font-weight: 600; margin-bottom: -1px; }
  .settings-tabs button.on { color: var(--text); border-bottom-color: var(--accent); }
  .settings-tabs button:hover { color: var(--text); }
  .srow { display: flex; align-items: center; gap: 12px; padding: 6px 0; font-size: 13px; }
  .srow .slabel { flex: 1; color: var(--text-2); }
  .srow input[type="number"] { width: 84px; padding: 3px 6px; background: var(--bg-2); color: var(--text); border: 1px solid var(--border); border-radius: 5px; }
  .srow input[type="text"], .srow input.field { flex: 0 1 260px; }
  .srow select { padding: 3px 6px; background: var(--bg-2); color: var(--text); border: 1px solid var(--border); border-radius: 5px; }
  .kbd-edit { font-family: ui-monospace, monospace; font-size: 11.5px; background: var(--bg-3); color: var(--text); border: 1px solid var(--border); border-radius: 5px; padding: 3px 9px; min-width: 116px; text-align: center; cursor: pointer; }
  .kbd-edit:hover { border-color: var(--accent); }
  .kbd-edit.capturing { background: var(--accent); color: #1a0e07; border-color: var(--accent); }
  .mfoot { display: flex; align-items: center; padding: 10px 16px; border-top: 1px solid var(--border); gap: 8px; background: var(--bg-1); }
</style>
