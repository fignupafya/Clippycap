<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { api } from './lib/api';
  import type { AppConfig, AssetDetail, AssetSummary, EditingConfig, FfmpegStatus, Folder, Job, Note, PlayerConfig, ReferenceView, SavedView, Source, Tag, TagGroup } from './lib/api';
  import WindowControls from './lib/WindowControls.svelte';
  import Pager from './lib/Pager.svelte';
  import logoUrl from './assets/clippycap-logo.png';

  type Quick = 'all' | 'untagged' | 'new';
  type EditKind = 'trim' | 'remove' | 'extract' | 'cut';
  interface FolderNode { path: string; name: string; count: number; children: FolderNode[]; }

  // Build a tree from the flat folder list /api/folders returns. Each Folder.path is fully
  // qualified, so a folder's parent is just the parent path lookup; folders with no parent
  // in the map become tree roots (typically the configured source roots).
  function folderBasename(p: string): string {
    const m = p.match(/[\\/]([^\\/]+)$/);
    return m ? m[1] : p;
  }
  function folderParent(p: string): string | null {
    const m = p.match(/^(.+)[\\/][^\\/]+$/);
    return m ? m[1] : null;
  }
  function buildFolderTree(flat: Folder[]): FolderNode[] {
    const map = new Map<string, FolderNode>();
    for (const { path, count } of flat) {
      map.set(path, { path, name: folderBasename(path), count, children: [] });
    }
    const roots: FolderNode[] = [];
    for (const node of map.values()) {
      const p = folderParent(node.path);
      const parent = p !== null ? map.get(p) : null;
      if (parent) parent.children.push(node); else roots.push(node);
    }
    const sortRec = (arr: FolderNode[]): void => {
      arr.sort((a, b) => a.path.localeCompare(b.path));
      arr.forEach((n) => sortRec(n.children));
    };
    sortRec(roots);
    return roots;
  }

  let tags = $state<Tag[]>([]);
  let tagById = $derived(new Map(tags.map((t) => [t.id, t])));
  let tagGroups = $state<TagGroup[]>([]);
  let tagGroupById = $derived(new Map(tagGroups.map((g) => [g.id, g])));
  let clipTagSearch = $state('');            // search box in the clip detail's tag picker
  let tagPageId = $state<number | null>(null);   // open a tag's page (dossier): its notes + its clips
  let tagPageNotes = $state('');                 // editable notes body on the open tag page (autosaves)
  let tagPageNotesTimer: ReturnType<typeof setTimeout> | null = null;
  let tagPage = $derived(tagPageId != null ? (tagById.get(tagPageId) ?? null) : null);
  let categoryPageId = $state<number | null>(null);  // open a category's hub page (directory of its tags)
  let categoryPage = $derived(categoryPageId != null ? (tagGroupById.get(categoryPageId) ?? null) : null);
  let categoryPageNotes = $state('');                // editable notes body on the open category page
  let categoryPageNotesTimer: ReturnType<typeof setTimeout> | null = null;
  let sources = $state<Source[]>([]);

  // Group a list of tags into category sections for the picker / sidebar. Categories come in their
  // own sort order; uncategorised tags fall into a trailing `null` section. `query` narrows by name.
  function groupTags(list: Tag[], query: string): { group: TagGroup | null; tags: Tag[] }[] {
    const q = query.trim().toLowerCase();
    const match = (t: Tag) => !q || t.name.toLowerCase().includes(q);
    const sections: { group: TagGroup | null; tags: Tag[] }[] = [];
    for (const g of tagGroups) {
      const inGroup = list.filter((t) => t.group_id === g.id && match(t));
      if (inGroup.length) sections.push({ group: g, tags: inGroup });
    }
    const uncategorised = list.filter((t) => t.group_id == null && match(t));
    if (uncategorised.length) sections.push({ group: null, tags: uncategorised });
    return sections;
  }
  let savedViews = $state<SavedView[]>([]);
  let assets = $state<AssetSummary[]>([]);
  let total = $state(0);
  let page = $state(1);                              // 1-based; the library grid is paginated
  let pageSize = $state(100);                        // overwritten from [ui].page_size once config loads
  let pageCount = $derived(Math.max(1, Math.ceil(total / pageSize)));
  let gridScroll = $state<HTMLElement>();            // the scrolling grid pane (reset to top per page)
  let loading = $state(false);
  let error = $state<string | null>(null);
  // in-app dialogs (replace window.confirm / window.prompt) and toasts (replace window.alert)
  let dialog = $state<{ kind: 'confirm' | 'prompt'; message: string; detail: string; value: string; placeholder: string; okLabel: string; danger: boolean; multiline: boolean; mentions: boolean; done: (r: string | boolean | null) => void } | null>(null);
  let dlgInputEl = $state<HTMLInputElement | HTMLTextAreaElement>();
  let toasts = $state<{ id: number; text: string; kind: 'info' | 'error' | 'success'; sticky: boolean }[]>([]);
  let toastSeq = 0;
  const toastTimers = new Map<number, ReturnType<typeof setTimeout>>();

  let quick = $state<Quick>('all');
  let filterTagIds = $state<number[]>([]);
  let searchText = $state('');
  let appliedText = $state('');
  let sort = $state('recorded_desc');
  let folders = $state<Folder[]>([]);                       // flat /api/folders response
  let folderFilter = $state<string | null>(null);            // narrow the grid to clips under this folder
  let expandedFolders = $state<Set<string>>(new Set());      // which tree nodes are open
  let folderTree = $derived(buildFolderTree(folders));
  let selected = $state<Set<number>>(new Set());     // selected asset ids (grid multi-select)
  let lastSelectedId = $state<number | null>(null);  // anchor for shift-range select
  let bulkBusy = $state(false);
  // bulk-tag modal state (Add/Remove (diff) and Replace (override) modes)
  let showBulkTagModal = $state(false);
  let bulkTagMode = $state<'modify' | 'replace'>('modify');
  let bulkTagSearch = $state('');
  let bulkTagAdd = $state<Set<number>>(new Set());           // tag ids queued to add (modify mode)
  let bulkTagRemove = $state<Set<number>>(new Set());        // tag ids queued to remove (modify mode)
  let bulkTagReplaceSet = $state<Set<number>>(new Set());    // checked tags for Replace mode
  let bulkTagCounts = $state<Map<number, number>>(new Map()); // tag id -> # of selected that have it
  let bulkTagModalSize = $state(0);                          // selected.size when the modal opened
  let filteredBulkTags = $derived(tags.filter((t) => t.name.toLowerCase().includes(bulkTagSearch.toLowerCase())));
  let canApplyBulkTag = $derived(bulkTagMode === 'replace' || bulkTagAdd.size > 0 || bulkTagRemove.size > 0);

  let detail = $state<AssetDetail | null>(null);
  let refs = $state<{ outgoing: ReferenceView[]; incoming: ReferenceView[] }>({ outgoing: [], incoming: [] });
  let addingRef = $state(false);
  let refClipQuery = $state('');
  let refPickedId = $state<number | null>(null);
  let refPickedTitle = $state('');
  let refPickedNoteMs = $state<number | null>(null);   // a moment in the picked clip (null = the whole clip)
  let refPickedClipNotes = $state<{ id: number; body: string; timestamp_ms: number }[] | null>(null);  // null = loading
  let refDesc = $state('');
  let editingRefId = $state<number | null>(null);
  let refDescDraft = $state('');
  let refDescEl = $state<HTMLTextAreaElement>();
  let editingTitle = $state(false);
  let titleDraft = $state('');
  let titleInputEl = $state<HTMLInputElement>();
  let mention = $state<{ el: HTMLTextAreaElement; queryStart: number; query: string; top: number; left: number; set: (s: string) => void; kind: 'clip' | 'note'; clipId: number | null } | null>(null);
  let mentionPopup = $state<{ id: number; title: string; top: number; left: number; body: string | null; time: number | null } | null>(null);
  let mentionIndex = $state(0);
  let mentionMatches = $derived(mention ? assets.filter((a) => a.title.toLowerCase().includes(mention!.query)).slice(0, 8) : []);
  let mentionNotes = $state<{ clipId: number; list: { id: number; body: string; timestamp_ms: number }[] } | null>(null);
  let mentionNoteMatches = $derived(
    mention?.kind === 'note' && mentionNotes?.clipId === mention.clipId
      ? mentionNotes.list.filter((n) => n.body.toLowerCase().includes(mention!.query)) : []
  );
  let glowNoteId = $state<number | null>(null);
  let pendingSeekMs = $state<number | null>(null);
  let editingGeneralNote = $state(false);
  let generalNoteEl = $state<HTMLTextAreaElement>();
  let editNoteBodyId = $state<number | null>(null);
  let noteBodyDraft = $state('');
  let scanJob = $state<{ scanned: number; total: number | null; message: string } | null>(null);
  let nativeWindow = $state(false);   // true when running inside the pywebview shell -> show our own title bar
  let showTags = $state(false);
  let showKeys = $state(false);
  let showSettings = $state(false);
  let settingsTab = $state<'editing' | 'player' | 'keys' | 'ffmpeg'>('editing');
  let pendingEditing = $state<EditingConfig | null>(null);
  let pendingPlayer = $state<PlayerConfig | null>(null);
  let pendingKeybindings = $state<Record<string, string> | null>(null);
  let capturingAction = $state<string | null>(null);
  let savingSettings = $state(false);
  let settingsError = $state<string | null>(null);
  let ffmpegStatus = $state<FfmpegStatus | null>(null);
  let ffmpegInstalling = $state(false);
  let ffmpegInstallMsg = $state('');
  let newTagName = $state('');
  let newTagColor = $state('#56c271');
  let tagIcon = $state('');
  let editingTagId = $state<number | null>(null);
  let tagImageRef = $state<string | null>(null);
  let newTagGroupId = $state<number | null>(null);   // category chosen in the tag create/edit form
  let newTagHasPage = $state(false);                 // "give this tag its own page" toggle
  let newGroupName = $state('');                     // "new category" input in the tag manager
  let newGroupParentId = $state<number | null>(null);  // optional parent for a new category
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

  // An auto-dismissing toast. Returns the id so the caller can replace / re-target it via toastUpdate.
  function toast(text: string, kind: 'info' | 'error' | 'success' = 'info'): number {
    const id = ++toastSeq;
    toasts = [...toasts, { id, text, kind, sticky: false }];
    toastTimers.set(id, setTimeout(() => toastDismiss(id), kind === 'error' ? 6000 : 3500));
    return id;
  }
  // A non-dismissing toast (a small spinner is shown next to its text while the work is in progress);
  // use toastUpdate() to flip it to a normal success / error toast when the work completes.
  function toastSticky(text: string, kind: 'info' | 'error' | 'success' = 'info'): number {
    const id = ++toastSeq;
    toasts = [...toasts, { id, text, kind, sticky: true }];
    return id;
  }
  function toastUpdate(id: number, text: string, kind: 'info' | 'error' | 'success' = 'info', sticky = false): void {
    const existing = toastTimers.get(id);
    if (existing) { clearTimeout(existing); toastTimers.delete(id); }
    toasts = toasts.map((t) => t.id === id ? { ...t, text, kind, sticky } : t);
    if (!sticky) toastTimers.set(id, setTimeout(() => toastDismiss(id), kind === 'error' ? 6000 : 3500));
  }
  function toastDismiss(id: number): void {
    const existing = toastTimers.get(id);
    if (existing) { clearTimeout(existing); toastTimers.delete(id); }
    toasts = toasts.filter((t) => t.id !== id);
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
  async function loadTags() {
    try { [tags, tagGroups] = await Promise.all([api.listTags(), api.listTagGroups()]); }
    catch (e) { error = String(e); }
  }
  async function loadSources() { try { sources = await api.listSources(); } catch (e) { error = String(e); } }
  async function loadFolders() { try { folders = await api.listFolders(); } catch { /* best effort */ } }
  // Fetch the current page of the grid. The grid only ever holds `pageSize` cards, so the DOM (and
  // the thumbnail load) stays bounded however large the library is. Used for plain refreshes too
  // (after a tag/note/edit change) -- it does NOT scroll; `reloadGrid` is the query/page entry point.
  async function loadAssets() {
    loading = true; error = null;
    try {
      const resp = await api.listAssets({
        tags_all: filterTagIds, untagged: quick === 'untagged', never_opened: quick === 'new',
        text: appliedText.trim() || undefined, sort,
        path_under: folderFilter ?? undefined,
        offset: (page - 1) * pageSize, limit: pageSize,
      });
      // A deletion can leave `page` past the last page (a query change already resets it to 1).
      // Clamp and bail -- changing `page` re-triggers the effect, which reloads the valid page.
      const lastPage = Math.max(1, Math.ceil(resp.total / pageSize));
      if (page > lastPage) { page = lastPage; return; }
      assets = resp.items; total = resp.total;
      if (selected.size > 0) {
        const vis = new Set(assets.map((a) => a.id));
        const kept = [...selected].filter((id) => vis.has(id));
        if (kept.length !== selected.size) selected = new Set(kept);
      }
    } catch (e) { error = String(e); } finally { loading = false; }
  }

  // Load the grid for a query/page change and return the pane to the top (so a new page starts at
  // its first card). Plain post-mutation refreshes call loadAssets directly and keep the scroll.
  async function reloadGrid() {
    await loadAssets();
    gridScroll?.scrollTo({ top: 0 });
  }

  // Detect files renamed / moved / deleted outside the app -- a cheap, hashing-free server-side
  // re-sync. Runs on mount (catches changes made while the app was closed) and whenever the window
  // regains focus. `reconciling` serialises overlapping triggers; it never blocks or freezes the UI.
  let reconciling = false;
  async function reconcileLibrary() {
    if (reconciling) return;
    reconciling = true;
    try {
      const r = await api.reconcile();
      if (r.changed) await loadAssets();          // refresh in place -- no scroll jump
    } catch { /* best-effort; a manual Scan always re-syncs */ }
    finally { reconciling = false; }
  }

  onMount(() => {
    // pywebview injects `window.pywebview` (sometimes before our JS runs, sometimes after via the
    // `pywebviewready` event); when present, render our custom frameless-window title bar.
    if ((window as unknown as { pywebview?: unknown }).pywebview) nativeWindow = true;
    else window.addEventListener('pywebviewready', () => { nativeWindow = true; }, { once: true });

    // Drag and resize on a frameless pywebview window. Capture phase + stopImmediatePropagation
    // pre-empts pywebview's own bubble-phase drag handler (which would otherwise also fire and
    // fight us).
    //
    // - Drag is handed off to the OS via WM_NCLBUTTONDOWN(HTCAPTION) -- that's what gives us Aero
    //   Snap (drag-to-edge -> half-screen preview), proper drag cursor, double-click-to-maximize.
    // - Resize is JS-driven (mousemove -> set_window_bounds) because Windows' modal SIZE loop won't
    //   engage on a window without WS_THICKFRAME, and adding that flag would paint a visible gray
    //   border on our otherwise-frameless form. Throttled to one rAF tick (~60 Hz) so we don't
    //   flood the IPC channel.
    type WinApi = {
      start_drag(): void;
      get_window_bounds(): Promise<[number, number, number, number]>;
      set_window_bounds(l: number, t: number, w: number, h: number): void;
    };
    const MIN_W = 800, MIN_H = 600;
    function onCaptureMouseDown(e: MouseEvent) {
      if (e.button !== 0) return;
      const api = (window as unknown as { pywebview?: { api?: WinApi } }).pywebview?.api;
      if (!api) return;                                    // browser / --app mode -- let it be
      const target = e.target as HTMLElement | null;
      if (!target) return;

      const edge = target.closest('.resize-edge') as HTMLElement | null;
      if (edge && edge.dataset.dir) {
        e.preventDefault(); e.stopImmediatePropagation();
        const dir = parseInt(edge.dataset.dir, 10);
        const isLeft = dir === 10 || dir === 13 || dir === 16;
        const isRight = dir === 11 || dir === 14 || dir === 17;
        const isTop = dir === 12 || dir === 13 || dir === 14;
        const isBottom = dir === 15 || dir === 16 || dir === 17;
        const startX = e.screenX, startY = e.screenY;
        void api.get_window_bounds().then(([l0, t0, w0, h0]) => {
          let pending: { l: number; t: number; w: number; h: number } | null = null;
          let rafQueued = false;
          const flush = () => {
            rafQueued = false;
            if (pending) { api.set_window_bounds(pending.l, pending.t, pending.w, pending.h); pending = null; }
          };
          const onMove = (ev: MouseEvent) => {
            const dx = ev.screenX - startX, dy = ev.screenY - startY;
            let l = l0, t = t0, w = w0, h = h0;
            if (isRight) w = Math.max(MIN_W, w0 + dx);
            if (isLeft) {
              const nw = Math.max(MIN_W, w0 - dx);
              l = l0 + (w0 - nw); w = nw;
            }
            if (isBottom) h = Math.max(MIN_H, h0 + dy);
            if (isTop) {
              const nh = Math.max(MIN_H, h0 - dy);
              t = t0 + (h0 - nh); h = nh;
            }
            pending = { l, t, w, h };
            if (!rafQueued) { rafQueued = true; requestAnimationFrame(flush); }
          };
          const onUp = () => {
            window.removeEventListener('mousemove', onMove, true);
            window.removeEventListener('mouseup', onUp, true);
            if (pending) flush();
          };
          window.addEventListener('mousemove', onMove, true);
          window.addEventListener('mouseup', onUp, true);
        });
        return;
      }
      if (target.closest('.pywebview-drag-region')) {
        e.preventDefault(); e.stopImmediatePropagation();
        api.start_drag();
      }
    }
    document.addEventListener('mousedown', onCaptureMouseDown, { capture: true });
    // (no cleanup -- App.svelte is the root component, never unmounts during the app's lifetime)
    void loadTags(); void loadSources(); void loadFolders();
    void watchLibraryJobs();   // pick up any scan / enrichment / identity-upgrade job running at startup
    void api.getConfig().then((c) => {
      cfg = c;
      const ps = c.ui?.page_size;
      if (typeof ps === 'number' && ps >= 1) pageSize = ps;
    }).catch(() => { /* fall back to FALLBACK_KEYS */ });
    void api.getHealth().then((h) => { editingAvailable = !!h.ffmpeg; }).catch(() => { /* keep true */ });
    void refreshFfmpegStatus(true);   // and prompt to install ffmpeg the first time, if it's missing
    void api.listSavedViews().then((v) => { savedViews = v; }).catch(() => { /* none */ });
    void syncFromHash();   // open the clip in the URL hash, if any

    // Keep the library in sync with the filesystem: once now (files may have changed while the app
    // was closed), then every time the window regains focus / is un-minimised.
    void reconcileLibrary();
    window.addEventListener('focus', () => void reconcileLibrary());
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') void reconcileLibrary();
    });
  });
  // A new query (filter / sort / search) jumps the grid back to page 1. It tracks those four
  // signals only; `untrack` keeps the `page = 1` write from making this depend on `page` itself,
  // so paging within a query does not re-trigger it.
  $effect(() => {
    void [quick, filterTagIds, appliedText, sort, folderFilter];
    untrack(() => { page = 1; });
  });
  // Reload + scroll-to-top whenever the query or the page changes (deps listed explicitly so the
  // tracking is robust regardless of the async load).
  $effect(() => { void [quick, filterTagIds, appliedText, sort, page, pageSize, folderFilter]; void reloadGrid(); });
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
  // The recording time, shown under the (file-name) title on each card. `recorded_at` is stored as
  // a canonical 'YYYY-MM-DDTHH:MM:SS' local string, so a plain slice formats it -- no Date parsing.
  function recordedAt(meta: Record<string, unknown>): string {
    const ra = meta['recorded_at'];
    return typeof ra === 'string' && ra.length >= 16 ? ra.slice(0, 16).replace('T', ' ') : '';
  }
  function toggleTagFilter(id: number) {
    quick = 'all';
    filterTagIds = filterTagIds.includes(id) ? filterTagIds.filter((x) => x !== id) : [...filterTagIds, id];
  }
  function selectFolder(path: string | null) {
    folderFilter = path;
    if (path !== null) quick = 'all';                       // a folder + a quick view would conflict
  }
  function toggleFolderExpand(path: string) {
    const next = new Set(expandedFolders);
    if (next.has(path)) next.delete(path); else next.add(path);
    expandedFolders = next;
  }
  function clearSelection() { selected = new Set(); lastSelectedId = null; }
  function selectAllVisible() { selected = new Set(assets.map((a) => a.id)); lastSelectedId = assets.at(-1)?.id ?? null; }
  async function selectAllMatching() {
    try {
      const ids = await api.listAssetIds({
        tags_all: filterTagIds, untagged: quick === 'untagged', never_opened: quick === 'new',
        text: appliedText.trim() || undefined, sort, path_under: folderFilter ?? undefined,
      });
      selected = new Set(ids);
      lastSelectedId = ids[ids.length - 1] ?? null;
    } catch (e) { toast(String(e), 'error'); }
  }
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
    const tid = toastSticky(`${label}: ${ids.length} clip${ids.length === 1 ? '' : 's'}…`);
    try {
      const results = await Promise.allSettled(ids.map(fn));
      const failed = results.filter((r) => r.status === 'rejected').length;
      await loadAssets(); await loadTags(); await refreshDetail();
      if (failed > 0) toastUpdate(tid, `${label}: ${ids.length - failed} ok, ${failed} failed`, 'error');
      else toastUpdate(tid, `${label}: ${ids.length} done`, 'success');
    } finally { bulkBusy = false; }
  }
  async function openBulkTagModal() {
    if (selected.size === 0) return;
    const ids = [...selected];
    bulkTagAdd = new Set();
    bulkTagRemove = new Set();
    bulkTagReplaceSet = new Set();
    bulkTagSearch = '';
    bulkTagMode = 'modify';
    bulkTagModalSize = ids.length;
    try {
      const raw = await api.assetTagCounts(ids);
      const counts = new Map<number, number>();
      for (const [k, v] of Object.entries(raw)) counts.set(Number(k), v);
      bulkTagCounts = counts;
      // Pre-fill the Replace-mode checkbox set with tags that ALL selected clips already have
      // -- so opening the modal and hitting Apply with no changes is a no-op (no surprise wipe).
      bulkTagReplaceSet = new Set(
        [...counts.entries()].filter(([, c]) => c === ids.length).map(([id]) => id),
      );
    } catch (e) { toast(String(e), 'error'); return; }
    showBulkTagModal = true;
  }
  function closeBulkTagModal() { showBulkTagModal = false; }
  function toggleBulkTagAdd(id: number) {
    const next = new Set(bulkTagAdd);
    if (next.has(id)) { next.delete(id); }
    else {
      next.add(id);
      if (bulkTagRemove.has(id)) { const rm = new Set(bulkTagRemove); rm.delete(id); bulkTagRemove = rm; }
    }
    bulkTagAdd = next;
  }
  function toggleBulkTagRemove(id: number) {
    const next = new Set(bulkTagRemove);
    if (next.has(id)) { next.delete(id); }
    else {
      next.add(id);
      if (bulkTagAdd.has(id)) { const ad = new Set(bulkTagAdd); ad.delete(id); bulkTagAdd = ad; }
    }
    bulkTagRemove = next;
  }
  function toggleBulkTagReplace(id: number, checked: boolean) {
    const next = new Set(bulkTagReplaceSet);
    if (checked) next.add(id); else next.delete(id);
    bulkTagReplaceSet = next;
  }
  async function applyBulkTag() {
    const ids = [...selected];
    if (ids.length === 0 || bulkBusy) return;
    bulkBusy = true;
    const tid = toastSticky(`Applying tag changes to ${ids.length} clip${ids.length === 1 ? '' : 's'}…`);
    try {
      const ops = bulkTagMode === 'replace'
        ? { replace_with: [...bulkTagReplaceSet] }
        : { add: [...bulkTagAdd], remove: [...bulkTagRemove] };
      const result = await api.bulkTags(ids, ops);
      closeBulkTagModal();
      await loadAssets(); await loadTags(); await refreshDetail();
      const total = result.added + result.removed;
      if (total === 0) toastUpdate(tid, 'No tag changes were needed', 'info');
      else toastUpdate(tid, `Tags updated: +${result.added} added · −${result.removed} removed`, 'success');
    } catch (e) {
      toastUpdate(tid, `Tag update failed: ${e}`, 'error');
    } finally { bulkBusy = false; }
  }
  async function bulkDelete() {
    const n = selected.size;
    if (n === 0 || !await confirmDialog(`Delete ${n} clip${n === 1 ? '' : 's'}?`, { detail: 'This removes them AND their files on disk — there is no undo.', okLabel: `Delete ${n}`, danger: true })) return;
    await bulkRun('deleted', (id) => api.deleteAsset(id, true));
  }
  async function loadRefs() { if (!detail) { refs = { outgoing: [], incoming: [] }; return; } try { refs = await api.getReferences(detail.id); } catch { /* ignore */ } }
  // routing via the URL hash (#clip/<id>) so the browser's back/forward work
  function setHash(h: string) { if (location.hash.replace(/^#/, '') !== h) location.hash = h; else void syncFromHash(); }
  function navTo(id: number | null) { setHash(id == null ? '' : 'clip/' + id); }
  function navToNote(clipId: number, noteId: number) { setHash(`clip/${clipId}/n/${noteId}`); }
  function navToAt(clipId: number, ms: number) { setHash(`clip/${clipId}/at/${ms}`); }
  function focusNote(noteId: number) {
    const n = detail?.timestamped_notes.find((x) => x.id === noteId);
    if (!n) return;
    glowNoteId = noteId;
    setTimeout(() => { if (glowNoteId === noteId) glowNoteId = null; }, 1700);
    if (videoEl && videoEl.readyState >= 1) seek(n.timestamp_ms ?? 0); else pendingSeekMs = n.timestamp_ms ?? 0;
    setTimeout(() => document.querySelector('.tsn.glow')?.scrollIntoView({ block: 'center', behavior: 'smooth' }), 80);
  }
  async function syncFromHash() {
    mention = null; mentionPopup = null; tagDropdownNoteId = null; editNoteBodyId = null;
    editingGeneralNote = false; editingTitle = false; addingRef = false;
    const m = /^#?clip\/(\d+)(?:\/n\/(\d+)|\/at\/(\d+))?$/.exec(location.hash);
    if (m) {
      const id = Number(m[1]);
      const noteTarget = m[2] ? Number(m[2]) : null;
      const atTarget = m[3] ? Number(m[3]) : null;
      if (detail?.id !== id) {
        const want = location.hash;
        try {
          const d = await api.getAsset(id);
          if (location.hash !== want) return;               // a faster navigation won the race
          detail = d; void api.markOpened(id); await loadRefs();
        } catch (e) { detail = null; refs = { outgoing: [], incoming: [] }; toast(String(e), 'error'); return; }
      }
      if (noteTarget != null) focusNote(noteTarget);
      else if (atTarget != null) {
        const n = detail?.timestamped_notes.find((x) => x.timestamp_ms === atTarget);
        if (n) focusNote(n.id);
        else if (videoEl && videoEl.readyState >= 1) seek(atTarget); else pendingSeekMs = atTarget;
      }
    } else if (detail !== null) {
      detail = null; refs = { outgoing: [], incoming: [] }; void loadAssets();
    }
  }
  function openDetail(a: AssetSummary) { navTo(a.id); }
  function openClip(id: number) { navTo(id); }
  function closeDetail() { navTo(null); }
  async function refreshDetail() { if (detail) { detail = await api.getAsset(detail.id); await loadRefs(); } }
  function resetRefAdd() { addingRef = false; refPickedId = null; refPickedTitle = ''; refPickedNoteMs = null; refPickedClipNotes = null; refDesc = ''; refClipQuery = ''; }
  async function pickRefClip(a: AssetSummary) {
    refPickedId = a.id; refPickedTitle = a.title; refPickedNoteMs = null; refPickedClipNotes = null;
    try { const d = await api.getAsset(a.id); if (refPickedId === a.id) refPickedClipNotes = d.timestamped_notes.map((n) => ({ id: n.id, body: n.body, timestamp_ms: n.timestamp_ms ?? 0 })); }
    catch { if (refPickedId === a.id) refPickedClipNotes = []; }
  }
  async function addRef() {
    if (!detail || refPickedId === null) return;
    try {
      await api.addReference({ from_asset_id: detail.id, to_asset_id: refPickedId, note: refDesc.trim() || undefined, to_timestamp_ms: refPickedNoteMs ?? undefined });
      resetRefAdd(); await loadRefs();
    } catch (e) { toast(String(e), 'error'); }
  }
  async function deleteRef(id: number) { try { await api.deleteReference(id); await loadRefs(); } catch (e) { toast(String(e), 'error'); } }
  function startEditRefDesc(r: ReferenceView) { editingRefId = r.id; refDescDraft = r.note || r.label || ''; setTimeout(() => refDescEl?.focus(), 0); }
  async function saveRefDesc(refId: number) {
    editingRefId = null;
    try { await api.updateReference(refId, refDescDraft.trim()); await loadRefs(); } catch (e) { toast(String(e), 'error'); }
  }
  function applySavedView(v: SavedView) {
    let f: { quick?: string; tag_ids?: number[]; text?: string } = {};
    try { f = JSON.parse(v.filter_json); } catch { /* fall back to defaults */ }
    quick = (f.quick === 'new' || f.quick === 'untagged') ? f.quick : 'all';
    filterTagIds = (f.tag_ids ?? []).filter((id) => tags.some((t) => t.id === id));
    appliedText = f.text ?? ''; searchText = appliedText; sort = v.sort_key;
  }
  async function saveCurrentView() {
    const name = await promptDialog('Save the current view', { placeholder: 'view name', okLabel: 'Save' });
    if (name == null || !name.trim()) return;
    try {
      await api.createSavedView({ name: name.trim(), filter_json: JSON.stringify({ quick, tag_ids: filterTagIds, text: appliedText }), sort_key: sort, sort_order: savedViews.length });
      savedViews = await api.listSavedViews();
    } catch (e) { toast(String(e), 'error'); }
  }
  async function removeSavedView(id: number) {
    try { await api.deleteSavedView(id); savedViews = await api.listSavedViews(); } catch (e) { toast(String(e), 'error'); }
  }
  function startEditTitle() { if (!detail) return; titleDraft = detail.title; editingTitle = true; setTimeout(() => titleInputEl?.focus(), 0); }
  async function saveTitle() {
    if (!detail || !editingTitle) return;
    editingTitle = false;
    const t = titleDraft.trim();
    if (!t || t === detail.title) return;
    // The clip's name IS its file name -- renaming here renames the file on disk (extension kept).
    if (videoEl) { videoEl.pause(); videoEl.removeAttribute('src'); videoEl.load(); }   // release the file
    try {
      await new Promise<void>((r) => setTimeout(r, 250));   // let the backend close its stream handle
      await api.renameFile(detail.id, t);
      await refreshDetail(); await loadAssets();
    } catch (e) { toast(String(e), 'error'); } finally { videoVersion += 1; }
  }
  async function deleteCurrentClip() {
    if (!detail) return;
    if (!await confirmDialog(`Delete “${detail.title}”?`, { detail: 'This removes the clip AND its file on disk — there is no undo.', okLabel: 'Delete clip', danger: true })) return;
    try { await api.deleteAsset(detail.id, true); closeDetail(); await loadTags(); } catch (e) { toast(String(e), 'error'); }
  }
  function gotoSibling(delta: number) {
    if (!detail) return;
    const cur = detail;
    const next = assets[assets.findIndex((a) => a.id === cur.id) + delta];
    if (next) void openDetail(next);
  }

  async function addSourcePrompt() {
    // Native pywebview window -> real OS folder picker. Browser fallback -> the text-input prompt
    // (browsers don't expose a folder-path API to JS for security; ``webkitdirectory`` returns the
    // files but never the absolute folder path we need).
    let path: string | null = null;
    if (nativeWindow) {
      try { path = (await (window as unknown as { pywebview: { api: { pick_folder: () => Promise<string | null> } } }).pywebview.api.pick_folder()) ?? null; }
      catch (e) { toast('Folder picker failed: ' + String(e), 'error'); return; }
    } else {
      path = await promptDialog('Add a source folder', { placeholder: 'e.g. D:\\Clips', okLabel: 'Add' });
    }
    if (!path) return;
    try { await api.addSource(path); await loadSources(); } catch (e) { toast(String(e), 'error'); }
  }
  // ---- scanning: a scan runs as a background job -- discovery streams the clips into the grid,
  // then enrichment fills in their durations. `watchLibraryJobs` polls whatever scan / enrichment /
  // identity-upgrade job is active and live-refreshes the grid, so the library fills in
  // progressively while the user already browses. -------------------------------------------------
  let scanWatching = false;          // at most one watcher loop runs at a time
  let scanWatchRestart = false;      // a watchLibraryJobs() call arriving mid-loop -> poll once more
  const SCAN_JOB_RE = /^(scan|enrich|upgrade)/;

  function activeLibraryJob(jobs: Job[]): Job | null {
    const active = jobs.filter((j) => (j.state === 'running' || j.state === 'pending') && SCAN_JOB_RE.test(j.name));
    return active.find((j) => j.state === 'running') ?? active[0] ?? null;   // prefer the running one
  }
  function scanLabel(j: { scanned: number; total: number | null; message: string }): string {
    const head = j.message || 'Scanning…';
    if (j.total && j.total > 0) return `${head} ${j.scanned} / ${j.total}`;
    return j.scanned > 0 ? `${head} ${j.scanned}` : head;
  }
  async function watchLibraryJobs() {
    if (scanWatching) { scanWatchRestart = true; return; }    // already watching -> ask it to re-poll
    scanWatching = true;
    try {
      do {
        scanWatchRestart = false;
        let sawActivity = false;
        for (;;) {
          let jobs: Job[];
          try { jobs = await api.listJobs(); } catch { break; }
          const job = activeLibraryJob(jobs);
          if (job === null) break;
          sawActivity = true;
          scanJob = { scanned: job.scanned, total: job.total, message: job.message };
          await loadAssets();                                  // stream clips / durations into the grid
          await new Promise((r) => setTimeout(r, 900));
        }
        scanJob = null;
        if (sawActivity) { await loadAssets(); await loadTags(); await loadFolders(); }
      } while (scanWatchRestart);
    } finally {
      scanWatching = false;
    }
  }
  function scanAll() {
    void (async () => {
      try { await api.scanAll(); }
      catch (e) { toast(String(e), 'error'); return; }
      void watchLibraryJobs();
    })();
  }
  // ---- ffmpeg: status / on-demand install / point at an existing build ----
  async function refreshFfmpegStatus(offerIfMissing = false) {
    try {
      const s = await api.getFfmpegStatus();
      ffmpegStatus = s;
      editingAvailable = s.available;
      if (offerIfMissing && s.offer_install && !ffmpegInstalling) void offerFfmpegInstall();
    } catch { /* ignore -- the app works without ffmpeg */ }
  }
  async function offerFfmpegInstall() {
    const yes = await confirmDialog('FFmpeg isn’t installed', {
      detail: 'Clippycap uses FFmpeg to make clip thumbnails and to trim / cut clips. Without it, '
        + 'thumbnails are captured in the browser and trimming is disabled. Download and install a '
        + 'static build now (~80 MB)? You can always do this later from Settings → FFmpeg.',
      okLabel: 'Install FFmpeg',
    });
    if (yes) { void startFfmpegInstall(); return; }
    try { await api.dismissFfmpegPrompt(); } catch { /* ignore */ }
    if (ffmpegStatus) ffmpegStatus = { ...ffmpegStatus, offer_install: false };
    toast('OK — you can install FFmpeg later from Settings → FFmpeg.');
  }
  function startFfmpegInstall() {
    if (ffmpegInstalling) return;
    ffmpegInstalling = true; ffmpegInstallMsg = 'Starting…';
    void (async () => {
      try {
        const { job_id } = await api.installFfmpeg();
        const tick = async () => {
          const j = await api.getJob(job_id);
          if (j.state === 'pending' || j.state === 'running') {
            ffmpegInstallMsg = j.total && j.scanned ? `${Math.min(100, Math.round((100 * j.scanned) / j.total))}%`
              : (j.message || 'Downloading…');
            setTimeout(() => void tick(), 500);
          } else if (j.state === 'done') {
            ffmpegInstalling = false; ffmpegInstallMsg = '';
            await refreshFfmpegStatus(false);
            toast('FFmpeg installed. Reopen a clip to see generated thumbnails.', 'success');
          } else {
            ffmpegInstalling = false; ffmpegInstallMsg = '';
            toast('FFmpeg install failed: ' + (j.error || 'unknown error'), 'error');
          }
        };
        await tick();
      } catch (e) { ffmpegInstalling = false; ffmpegInstallMsg = ''; toast('FFmpeg install failed: ' + String(e), 'error'); }
    })();
  }
  async function pickFfmpegPath() {
    // Native window -> real OS file picker (the backend accepts a path to ffmpeg.exe directly OR
    // to the folder that contains it, so we let the user pick the .exe -- the most common case --
    // with a filter, and fall back to the text-input prompt in the browser).
    let p: string | null = null;
    if (nativeWindow) {
      try {
        p = (await (window as unknown as { pywebview: { api: { pick_file: (t?: string[]) => Promise<string | null> } } })
          .pywebview.api.pick_file(['Executable (*.exe)', 'All files (*.*)'])) ?? null;
      } catch (e) { toast('File picker failed: ' + String(e), 'error'); return; }
    } else {
      p = await promptDialog('Path to ffmpeg.exe — or to the folder that contains ffmpeg.exe and ffprobe.exe',
        { placeholder: 'C:\\ffmpeg\\bin', okLabel: 'Use this' });
    }
    if (p === null) return;
    try {
      ffmpegStatus = await api.setFfmpegPath(p);
      editingAvailable = ffmpegStatus.available;
      toast(ffmpegStatus.available ? 'Using that FFmpeg now.' : 'Saved, but FFmpeg still isn’t available there.',
        ffmpegStatus.available ? 'success' : 'error');
    } catch (e) { toast(String(e), 'error'); }
  }
  async function resetFfmpegToAuto() {
    try { ffmpegStatus = await api.resetFfmpegPath(); editingAvailable = ffmpegStatus.available; toast('Back to auto-detect.'); }
    catch (e) { toast(String(e), 'error'); }
  }

  function startEditTag(t: Tag) { editingTagId = t.id; newTagName = t.name; newTagColor = t.color; tagIcon = t.icon ?? ''; tagImageRef = t.image_ref; newTagGroupId = t.group_id; newTagHasPage = t.has_page; }
  function cancelEditTag() { editingTagId = null; newTagName = ''; tagIcon = ''; tagImageRef = null; newTagGroupId = null; newTagHasPage = false; }
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
        await api.createTag({ name, color: newTagColor, icon, image_ref: tagImageRef, sort_order: tags.length, group_id: newTagGroupId, has_page: newTagHasPage });
      } else {
        const orig = tags.find((t) => t.id === editingTagId);
        await api.updateTag(editingTagId, { name, color: newTagColor, icon, image_ref: tagImageRef, description: orig?.description ?? '', sort_order: orig?.sort_order ?? 0, group_id: newTagGroupId, has_page: newTagHasPage, notes: orig?.notes ?? '' });
      }
      cancelEditTag(); await loadTags(); await loadAssets(); await refreshDetail();
    } catch (e) { toast(String(e), 'error'); }
  }
  async function deleteTag(id: number) {
    if (!await confirmDialog('Delete this tag everywhere?', { detail: 'It is removed from every clip and note.', okLabel: 'Delete', danger: true })) return;
    try { await api.deleteTag(id); if (editingTagId === id) cancelEditTag(); await loadTags(); await loadAssets(); await refreshDetail(); } catch (e) { toast(String(e), 'error'); }
  }
  async function createCategory() {
    const name = newGroupName.trim();
    if (!name) return;
    try { await api.createTagGroup({ name, parent_id: newGroupParentId }); newGroupName = ''; newGroupParentId = null; await loadTags(); } catch (e) { toast(String(e), 'error'); }
  }
  async function deleteCategory(g: TagGroup) {
    if (!await confirmDialog(`Delete category “${g.name}”?`, { detail: 'Its tags are kept — they just become uncategorised.', okLabel: 'Delete', danger: true })) return;
    try { await api.deleteTagGroup(g.id); await loadTags(); } catch (e) { toast(String(e), 'error'); }
  }
  async function toggleGroupPage(g: TagGroup, on: boolean) {
    try { await api.updateTagGroup(g.id, { name: g.name, color: g.color, has_page: on, sort_order: g.sort_order, parent_id: g.parent_id }); await loadTags(); }
    catch (e) { toast(String(e), 'error'); }
  }
  // Tag page (dossier): the notes write-up + the clips carrying the tag (the grid, filtered to it).
  function openTagPage(t: Tag) {
    closeDetail(); showTags = false; categoryPageId = null;
    tagPageId = t.id; tagPageNotes = t.notes;
    quick = 'all'; filterTagIds = [t.id]; folderFilter = null; page = 1;
  }
  function closeTagPage() { tagPageId = null; filterTagIds = []; }
  function onTagPageNotesInput() {
    if (tagPageNotesTimer) clearTimeout(tagPageNotesTimer);
    const id = tagPageId;
    const body = tagPageNotes;
    tagPageNotesTimer = setTimeout(() => {
      if (id != null) void api.setTagNotes(id, body).then(() => loadTags()).catch((e) => toast(String(e), 'error'));
    }, 600);
  }
  // Category page (hub): editable notes + a directory of its sub-categories and tags.
  function openCategoryPage(g: TagGroup) {
    closeDetail(); showTags = false; tagPageId = null;
    categoryPageId = g.id; categoryPageNotes = g.notes;
  }
  function closeCategoryPage() { categoryPageId = null; }
  function onCategoryPageNotesInput() {
    if (categoryPageNotesTimer) clearTimeout(categoryPageNotesTimer);
    const id = categoryPageId;
    const body = categoryPageNotes;
    categoryPageNotesTimer = setTimeout(() => {
      if (id != null) void api.setTagGroupNotes(id, body).then(() => loadTags()).catch((e) => toast(String(e), 'error'));
    }, 600);
  }
  // Categories that have a page, as a parent->children tree for the sidebar navigator.
  function rootPageCategories(): TagGroup[] {
    return tagGroups.filter((g) => g.has_page && (g.parent_id == null || !tagGroups.some((p) => p.id === g.parent_id && p.has_page)));
  }
  function childPageCategories(parentId: number): TagGroup[] {
    return tagGroups.filter((g) => g.has_page && g.parent_id === parentId);
  }
  async function applyTag(tagId: number) {
    if (!detail) return;
    try { await api.applyTag(detail.id, tagId); await refreshDetail(); await loadTags(); } catch (e) { toast(String(e), 'error'); }
  }
  async function unapplyTag(tagId: number) {
    if (!detail) return;
    try { await api.unapplyTag(detail.id, tagId); await refreshDetail(); await loadTags(); } catch (e) { toast(String(e), 'error'); }
  }
  // A small fixed palette so a freshly-created tag still gets a stable, distinct colour (the user
  // can recolour it later in the tag manager). Seeded by name so the same nick keeps the same hue.
  const TAG_PALETTE = ['#ef5b5b', '#f0a64f', '#e8c84d', '#56c271', '#4fb6f0', '#7c8cf0', '#b78bf0', '#f06bb0', '#4fd2c2', '#f08a4f'];
  function pickTagColor(seed: string): string {
    let h = 0;
    for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
    return TAG_PALETTE[h % TAG_PALETTE.length];
  }
  async function createTagInline(name: string) {
    if (!detail) return;
    const clean = name.trim();
    if (!clean) return;
    try {
      const t = await api.createTag({ name: clean, color: pickTagColor(clean) });
      await api.applyTag(detail.id, t.id);
      clipTagSearch = '';
      await refreshDetail(); await loadTags();
    } catch (e) { toast(String(e), 'error'); }
  }
  // Enter in the picker search applies the best match (exact name first, else first prefix/contains
  // match); if nothing matches, it creates the tag on the fly. Esc clears + blurs.
  function onClipTagSearchKey(e: KeyboardEvent, appliedIds: number[]) {
    if (e.key === 'Escape') { clipTagSearch = ''; (e.target as HTMLInputElement | null)?.blur(); return; }
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const q = clipTagSearch.trim();
    if (!q) return;
    const avail = tags.filter((t) => !appliedIds.includes(t.id) && t.name.toLowerCase().includes(q.toLowerCase()));
    const target = avail.find((t) => t.name.toLowerCase() === q.toLowerCase()) ?? avail[0];
    if (target) { void applyTag(target.id); clipTagSearch = ''; } else { void createTagInline(q); }
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
    const before = el.value.slice(0, caret);
    const c = caretCoords(el);
    const noteM = /@\{(\d+)\}@([\w-]*)$/.exec(before);     // "@" right after a clip token -> pick a note in it
    if (noteM) {
      const clipId = Number(noteM[1]);
      mention = { el, queryStart: caret - noteM[0].length, query: noteM[2].toLowerCase(), top: c.top, left: c.left, set, kind: 'note', clipId };
      mentionIndex = 0; void loadMentionNotes(clipId);
      return;
    }
    const clipM = /(?:^|[\s(])@([\w-]*)$/.exec(before);
    if (clipM) {
      mention = { el, queryStart: caret - clipM[1].length - 1, query: clipM[1].toLowerCase(), top: c.top, left: c.left, set, kind: 'clip', clipId: null };
      mentionIndex = 0;
      return;
    }
    mention = null;
  }
  async function loadMentionNotes(clipId: number) {
    if (mentionNotes?.clipId === clipId) return;
    mentionNotes = { clipId, list: [] };
    try {
      const d = await api.getAsset(clipId);
      if (mention?.kind === 'note' && mention.clipId === clipId) {
        mentionNotes = { clipId, list: d.timestamped_notes.map((n) => ({ id: n.id, body: n.body, timestamp_ms: n.timestamp_ms ?? 0 })) };
      }
    } catch { /* ignore */ }
  }
  function onMentionKey(e: KeyboardEvent): boolean {
    if (!mention) return false;
    const len = mention.kind === 'note' ? mentionNoteMatches.length : mentionMatches.length;
    if (e.key === 'ArrowDown') { e.preventDefault(); mentionIndex = Math.min(mentionIndex + 1, Math.max(0, len - 1)); return true; }
    if (e.key === 'ArrowUp') { e.preventDefault(); mentionIndex = Math.max(0, mentionIndex - 1); return true; }
    if (e.key === 'Enter') {
      e.preventDefault();
      const i = Math.min(mentionIndex, len - 1);
      if (mention.kind === 'note') { const n = mentionNoteMatches[i]; if (n) pickNoteMention(n.id); }
      else { const c0 = mentionMatches[i]; if (c0) pickClipMention(c0.id); }
      return true;
    }
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeMention(); return true; }
    return false;
  }
  function _applyMention(token: string, consumedLen: number) {
    if (!mention) return;
    const { el, queryStart, set } = mention;
    const newText = el.value.slice(0, queryStart) + token + el.value.slice(queryStart + consumedLen);
    const newCaret = queryStart + token.length;
    mention = null;
    set(newText);
    setTimeout(() => { el.focus(); el.setSelectionRange(newCaret, newCaret); }, 0);
  }
  function pickClipMention(clipId: number) {
    if (!mention || mention.kind !== 'clip') return;
    _applyMention(`@{${clipId}}`, 1 + mention.query.length);   // no trailing space: type @ again for a moment
  }
  function pickNoteMention(noteId: number) {
    if (!mention || mention.kind !== 'note' || mention.clipId == null) return;
    _applyMention(`@{${mention.clipId}#${noteId}} `, `@{${mention.clipId}}@${mention.query}`.length);
  }
  function closeMention() { mention = null; }
  function splitMentions(body: string): { text: string; id: number | null; noteId: number | null }[] {
    const out: { text: string; id: number | null; noteId: number | null }[] = [];
    const re = /@\{(\d+)(?:#(\d+))?\}/g;
    let last = 0, m: RegExpExecArray | null;
    while ((m = re.exec(body)) !== null) {
      if (m.index > last) out.push({ text: body.slice(last, m.index), id: null, noteId: null });
      const id = Number(m[1]);
      out.push({ text: detail?.mentioned_assets?.[String(id)] ?? `clip #${id}`, id, noteId: m[2] ? Number(m[2]) : null });
      last = m.index + m[0].length;
    }
    if (last < body.length || out.length === 0) out.push({ text: body.slice(last), id: null, noteId: null });
    return out;
  }
  function showMentionPopup(e: MouseEvent, id: number, title: string, body?: string, time?: number) {
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    mentionPopup = { id, title, top: r.bottom + 4, left: Math.max(8, r.left), body: body ?? null, time: time ?? null };
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
      // Paused: snap the timeline playhead to the displayed frame's *exact* PTS. ``frameNudge``
      // adds ~half a frame to ``currentTime`` so a stored-as-integer-ms note still seeks solidly
      // INTO its frame; using that nudged ``currentTime`` for the playhead would visually push it
      // ~half a frame past a note marker at the same frame, which looks like the bar is between
      // two frames. With this snap, white playhead and yellow note marker land on the same pixel.
      if (videoEl?.paused) curMs = Math.round(meta.mediaTime * 1000);
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
    const labels: Record<EditKind, [string, string, string]> = {
      trim:    ['Trimming…',             'Trimmed',             'Trim failed'],
      remove:  ['Removing segment…',     'Segment removed',     'Remove failed'],
      extract: ['Saving as a new clip…', 'Saved as a new clip', 'Save failed'],
      cut:     ['Cutting to a new clip…','Cut to a new clip',   'Cut failed'],
    };
    const [progressLabel, doneLabel, failLabel] = labels[kind];
    const tid = toastSticky(progressLabel);
    const touchesSource = kind !== 'extract';                          // 'extract' only writes a new file
    if (touchesSource && videoEl) { videoEl.pause(); videoEl.removeAttribute('src'); videoEl.load(); }  // release the file
    try {
      if (touchesSource) await new Promise<void>((r) => setTimeout(r, 250));   // let the backend close its stream handle
      let madeTitle = '';
      if (kind === 'trim') await api.trimAsset(id, s, o);
      else if (kind === 'remove') await api.removeSegment(id, s, o);
      else { const made = await api.extractSegment(id, s, o, kind === 'cut'); madeTitle = made.title; }
      toastUpdate(tid, madeTitle ? `${doneLabel}: ${madeTitle}` : doneLabel, 'success');
      clearSel();
      await refreshDetail();
      await loadAssets();
    } catch (e) {
      toastUpdate(tid, `${failLabel}: ${e}`, 'error');
    } finally { videoVersion += 1; busy = false; }
  }

  function openSettings() {
    if (!cfg) return;
    pendingEditing = { ...cfg.editing };
    pendingPlayer = { ...cfg.player };
    pendingKeybindings = { ...cfg.keybindings };
    capturingAction = null; settingsError = null; settingsTab = 'editing';
    showSettings = true;
    void refreshFfmpegStatus(false);
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

<svelte:window onkeydown={onKey} onpointermove={onPointerMove} onpointerup={endDrag} onpointercancel={endDrag} onpointerdown={onWindowPointerDown} onhashchange={syncFromHash} />

{#if nativeWindow}
  <!-- Invisible strips at each window edge / corner. The capture-phase mousedown handler turns a
       click on one of these into a native WM_NCLBUTTONDOWN(HT…), so the OS handles the resize -- including
       the proper cursors, the dotted preview, and Aero Snap. Hit-test codes: 12=top, 15=bottom, 10=left,
       11=right, 13=top-left, 14=top-right, 16=bottom-left, 17=bottom-right. -->
  <div class="resize-edge n"  data-dir="12"></div>
  <div class="resize-edge s"  data-dir="15"></div>
  <div class="resize-edge w"  data-dir="10"></div>
  <div class="resize-edge e"  data-dir="11"></div>
  <div class="resize-edge nw" data-dir="13"></div>
  <div class="resize-edge ne" data-dir="14"></div>
  <div class="resize-edge sw" data-dir="16"></div>
  <div class="resize-edge se" data-dir="17"></div>
{/if}

<div class="app">
  <header class="topbar">
    <div class="brand pywebview-drag-region">
      <img src={logoUrl} class="logo" alt="" draggable="false" />
      <span class="brand-name">Clippycap</span>
    </div>
    <input class="field search" placeholder="Search clips & notes — press Enter" bind:value={searchText}
           onkeydown={(e) => { if (e.key === 'Enter') appliedText = searchText; }} />
    <select class="field sort" bind:value={sort}>
      <option value="recorded_desc">Recorded (newest)</option>
      <option value="recorded_asc">Recorded (oldest)</option>
      <option value="added_desc">Added (newest)</option>
      <option value="duration_desc">Duration</option>
      <option value="title_asc">Title</option>
    </select>
    <div class="topbar-fill pywebview-drag-region"></div>
    <button class="btn sm" onclick={scanAll} disabled={scanJob !== null}>{scanJob ? scanLabel(scanJob) : 'Scan'}</button>
    <button class="btn sm" onclick={() => (showTags = true)}>Tags</button>
    <button class="btn sm" onclick={openSettings} disabled={!cfg} title="Settings">⚙ Settings</button>
    {#if nativeWindow}<WindowControls />{/if}
  </header>

  <div class="body">
    <aside>
      <nav>
        <button class="nav" class:on={quick === 'all' && filterTagIds.length === 0} onclick={() => { quick = 'all'; filterTagIds = []; }}>All clips <span class="ct">{total}</span></button>
        <button class="nav" class:on={quick === 'new'} onclick={() => { quick = 'new'; filterTagIds = []; }}>New (unopened)</button>
        <button class="nav" class:on={quick === 'untagged'} onclick={() => { quick = 'untagged'; filterTagIds = []; }}>Untagged</button>
      </nav>
      {#if savedViews.length > 0}
        <div class="sec-title">Saved views</div>
        {#each savedViews as v (v.id)}
          <div class="savedview-row">
            <button class="nav" onclick={() => applySavedView(v)} title={v.name}>★ {v.name}</button>
            <button class="x" onclick={() => removeSavedView(v.id)} title="remove this view">×</button>
          </div>
        {/each}
      {/if}
      <button class="btn sm" style:margin-top="6px" onclick={saveCurrentView} title="save the current filter / search as a view">💾 Save current view</button>
      {#if folderTree.length > 0}
        <div class="sec-title">Folders {#if folderFilter}<button class="sec-clear" onclick={() => selectFolder(null)} title="back to all clips">× clear</button>{/if}</div>
        <div class="folder-tree">
          {#each folderTree as f (f.path)}
            {@render folderNode(f, 0)}
          {/each}
        </div>
      {/if}
      {#if rootPageCategories().length}
        <div class="sec-title">Categories</div>
        {#each rootPageCategories() as g (g.id)}{@render catNavNode(g, 0)}{/each}
      {/if}
      <div class="sec-title">Tags</div>
      {#each groupTags(tags, '') as section (section.group?.id ?? 'uncat')}
        {#if section.group}
          {@const g = section.group}
          {#if g.has_page}
            <button class="cat-sub cat-sub-link" style:--c={g.color || 'var(--text-3)'} onclick={() => openCategoryPage(g)} title="open this category's hub page">{g.name} ↗</button>
          {:else}
            <div class="cat-sub" style:--c={g.color || 'var(--text-3)'}>{g.name}</div>
          {/if}
        {/if}
        <div class="tagcloud" style:margin-bottom="6px">
          {#each section.tags as t (t.id)}
            <button class="tagchip" class:on={filterTagIds.includes(t.id)} style:--c={t.color} style:background={filterTagIds.includes(t.id) ? t.color : ''} onclick={() => toggleTagFilter(t.id)}>
              {@render tagFace(t)} {t.name} <span class="n">{t.asset_count ?? 0}</span>
            </button>
          {/each}
        </div>
      {/each}
      {#if tags.length === 0}<span class="faint" style:font-size="12px">No tags yet — create some via “Tags”.</span>{/if}
      <div class="sec-title">Sources</div>
      {#each sources as s (s.id)}<div class="src" title={s.path}>{s.path}</div>{/each}
      <button class="btn sm" style:margin-top="6px" onclick={addSourcePrompt}>+ Add source folder</button>
    </aside>

    <main>
      <div class="lib-scroll" bind:this={gridScroll}>
      {#if error}<div class="err">{error}</div>{/if}
      {#if tagPage}
        <!-- Tag page (dossier): notes write-up; the grid below is filtered to this tag's clips. -->
        <div class="tagpage" style:--c={tagPage.color}>
          <div class="tagpage-head">
            <button class="btn sm" onclick={closeTagPage}>← Library</button>
            <span class="tagpage-title">{@render tagFace(tagPage)} {tagPage.name}</span>
            <span class="faint" style:font-size="12px">{total} clip{total === 1 ? '' : 's'}</span>
          </div>
          <textarea class="field tagpage-notes" rows="3"
                    placeholder="Notes about “{tagPage.name}” — evaluation, patterns, @mention example clips…"
                    bind:value={tagPageNotes} oninput={onTagPageNotesInput}></textarea>
        </div>
      {/if}
      {#if categoryPage}
        {@const cat = categoryPage}
        <!-- Category page (hub): editable notes + sub-categories + the category's tags. -->
        <div class="tagpage" style:--c={cat.color || 'var(--accent)'}>
          <div class="tagpage-head">
            <button class="btn sm" onclick={closeCategoryPage}>← Library</button>
            {#if cat.parent_id != null}
              {@const parent = tagGroupById.get(cat.parent_id)}
              {#if parent}<button class="btn sm" onclick={() => openCategoryPage(parent)} title="parent category">↑ {parent.name}</button>{/if}
            {/if}
            <span class="tagpage-title">📁 {cat.name}</span>
            <span class="faint" style:font-size="12px">{tags.filter((t) => t.group_id === cat.id).length} tags</span>
          </div>
          <textarea class="field tagpage-notes" rows="3"
                    placeholder="Notes about “{cat.name}” — what this category is, what to look for…"
                    bind:value={categoryPageNotes} oninput={onCategoryPageNotesInput}></textarea>
          {#if childPageCategories(cat.id).length}
            <div class="tagpage-sub">Sub-categories</div>
            <div class="tagcloud">
              {#each childPageCategories(cat.id) as g (g.id)}
                <button class="tagchip" style:--c={g.color || 'var(--text-3)'} onclick={() => openCategoryPage(g)}>📁 {g.name} ↗</button>
              {/each}
            </div>
          {/if}
          <div class="tagpage-sub">Tags</div>
          <div class="tagcloud">
            {#each tags.filter((t) => t.group_id === cat.id) as t (t.id)}
              <button class="tagchip" style:--c={t.color} onclick={() => (t.has_page ? openTagPage(t) : (closeCategoryPage(), toggleTagFilter(t.id)))}>{@render tagFace(t)} {t.name} <span class="n">{t.asset_count ?? 0}</span></button>
            {/each}
            {#if tags.filter((t) => t.group_id === cat.id).length === 0}<span class="faint" style:font-size="12px">No tags in this category yet — assign one in the Tags manager.</span>{/if}
          </div>
        </div>
      {/if}
      {#if selected.size > 0}
        <div class="bulkbar">
          <b>{selected.size}</b> selected
          <button class="btn sm" onclick={selectAllVisible}>all visible ({assets.length})</button>
          {#if total > assets.length && selected.size < total}<button class="btn sm" onclick={selectAllMatching}>all {total} matching</button>{/if}
          <button class="btn sm" onclick={clearSelection}>clear</button>
          <span style:flex="1"></span>
          <button class="btn sm" disabled={bulkBusy || tags.length === 0} onclick={openBulkTagModal} title={tags.length === 0 ? 'create tags first via the Tags modal' : 'open the bulk-tag editor (Add/Remove or Replace)'}>🏷 Edit tags…</button>
          <button class="btn sm" disabled={bulkBusy} onclick={bulkDelete}>🗑 delete {selected.size}</button>
        </div>
      {/if}
      {#if folderFilter}
        <div class="folder-crumb">
          <span style:font-size="14px">📁</span>
          <span class="folder-crumb-path" title={folderFilter}>{folderFilter}</span>
          <span style:flex="1"></span>
          <button class="btn sm" onclick={() => selectFolder(null)}>← Back to all clips</button>
        </div>
      {/if}
      <div class="bar"><b>{total}</b> clips{loading ? ' · loading…' : ''}{filterTagIds.length ? ' · filtered by ' + filterTagIds.length + ' tag(s)' : ''}{pageCount > 1 ? ` · page ${page} / ${pageCount}` : ''}</div>
      <div class="grid">
        {#each assets as a (a.id)}
          <button class="card" class:sel={selected.has(a.id)} onclick={(e) => cardClick(a, e)}>
            <div class="thumb">
              <img src={a.thumbnail_url} alt="" onerror={hideBrokenImg} />
              <span class="film">▶</span>
              <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
              <span class="selbox" class:on={selected.has(a.id)} onclick={(e) => { e.stopPropagation(); toggleSelect(a); }} title="select (Ctrl-click / Shift-click range)">{selected.has(a.id) ? '✓' : ''}</span>
              {#if durationOf(a.metadata)}<span class="dur">{durationOf(a.metadata)}</span>
              {:else if a.metadata_pending}<span class="dur pending" title="reading clip details…">…</span>{/if}
              {#if a.is_new}<span class="badge bnew">new</span>{/if}
              {#if a.note_count}<span class="badge">📝 {a.note_count}</span>{/if}
            </div>
            <div class="cb">
              <div class="ct" title={a.title}>{a.title}</div>
              {#if recordedAt(a.metadata)}<div class="cdate">{recordedAt(a.metadata)}</div>{/if}
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
      {#if assets.length === 0 && !loading}
        <div class="grid-empty">{total === 0 ? 'No clips yet — add a source folder and Scan.' : 'Nothing on this page.'}</div>
      {/if}
      </div>
      {#if pageCount > 1}
        <Pager bind:page {pageCount} {total} {pageSize} />
      {/if}
    </main>
  </div>
</div>

{#if detail}
  {@const d = detail}
  <div class="overlay">
    <div class="otop">
      <button class="btn sm" onclick={closeDetail}>← Library</button>
      <button class="btn sm" onclick={() => history.back()} title="Back to the previously viewed clip">↶ Back</button>
      <button class="btn sm" onclick={() => gotoSibling(-1)} title="Previous clip ({binding('prev_asset')})">‹</button>
      <button class="btn sm" onclick={() => gotoSibling(1)} title="Next clip ({binding('next_asset')})">›</button>
      {#if editingTitle}
        <input class="field" style:max-width="320px" bind:value={titleDraft} bind:this={titleInputEl}
               onkeydown={(e) => { if (e.key === 'Enter') saveTitle(); else if (e.key === 'Escape') editingTitle = false; }} onblur={saveTitle} />
      {:else}
        <button class="otitle" onclick={startEditTitle} title="rename this clip (renames the file on disk; the extension is kept)">{d.title} <span class="faint" style:font-size="11px">✎</span></button>
      {/if}
      <span class="otop-fill pywebview-drag-region"></span>
      <button class="btn sm" onclick={deleteCurrentClip} title="Delete this clip and its file from disk">🗑 Delete clip</button>
      <button class="btn sm" onclick={() => (showKeys = true)} title="Keyboard shortcuts">⌨ Keys</button>
      {#if nativeWindow}<WindowControls />{/if}
    </div>
    <div class="obody">
      <div class="player">
        <video bind:this={videoEl} src={`${d.stream_url}?v=${videoVersion}`} controls
               onpointerdown={deflectVideoFocus} onfocusin={deflectVideoFocus}
               ontimeupdate={() => {
                 if (!videoEl) return;
                 // Paused (after a seek or step): use the rvfc-supplied frame PTS so the playhead
                 // lands on the frame, not the half-frame-nudged ``currentTime``. Playing: just
                 // follow ``currentTime`` -- it advances smoothly between frames.
                 curMs = (videoEl.paused && exactFrameTime > 0)
                   ? Math.round(exactFrameTime * 1000)
                   : Math.floor(videoEl.currentTime * 1000);
               }}
               onloadedmetadata={() => { if (videoEl && Number.isFinite(videoEl.duration)) durMs = Math.floor(videoEl.duration * 1000); if (pendingSeekMs != null) { seek(pendingSeekMs); pendingSeekMs = null; } }}></video>
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
        <!-- Applied tags stay in their own row on top (unchanged), each in its tag colour. -->
        <div class="tagcloud">
          {#each d.tag_ids as id (id)}
            {@const t = tagById.get(id)}
            {#if t}<span class="pill" style:background={t.color}>{@render tagFace(t)} {t.name} <button class="x" onclick={() => unapplyTag(t.id)}>×</button></span>{/if}
          {/each}
          {#if d.tag_ids.length === 0}<span class="faint">no tags</span>{/if}
        </div>
        <!-- Picker: search-first, every tag shows its colour, grouped by category, create-on-the-fly. -->
        <div class="tagpicker">
          <input class="field tagpicker-search" placeholder="Search or create a tag…"
                 bind:value={clipTagSearch}
                 onkeydown={(e) => onClipTagSearchKey(e, d.tag_ids)} />
          {#each groupTags(tags.filter((t) => !d.tag_ids.includes(t.id)), clipTagSearch) as section (section.group?.id ?? 'uncat')}
            {#if section.group}<div class="tagpicker-cat" style:--c={section.group.color || 'var(--text-3)'}>{section.group.name}</div>{/if}
            <div class="tagcloud">
              {#each section.tags as t (t.id)}
                <button class="tagchip" style:--c={t.color} onclick={() => { applyTag(t.id); clipTagSearch=''; }}>＋ {@render tagFace(t)} {t.name}</button>
              {/each}
            </div>
          {/each}
          {#if clipTagSearch.trim() && !tags.some((t) => t.name.toLowerCase() === clipTagSearch.trim().toLowerCase())}
            <button class="tagchip create" onclick={() => createTagInline(clipTagSearch)}>＋ Create “{clipTagSearch.trim()}”</button>
          {/if}
        </div>
        <h4>General note</h4>
        {#if editingGeneralNote}
          <textarea class="field" rows="5" bind:value={generalNoteText} bind:this={generalNoteEl}
                    oninput={(e) => onMentionInput(e, (s) => { generalNoteText = s; })}
                    onkeydown={(e) => { if (onMentionKey(e)) return; if (e.key === 'Escape') { editingGeneralNote = false; void saveGeneralNote(); } }}
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
          <div class="tsn" class:glow={glowNoteId === n.id}>
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
                        onkeydown={(e) => { if (onMentionKey(e)) return; if (e.key === 'Escape') editNoteBodyId = null; else if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void saveNoteBody(n.id); } }}></textarea>
              <button class="tsnbtn" onclick={() => saveNoteBody(n.id)} title="save">✔</button>
              <button class="x" onclick={() => (editNoteBodyId = null)} title="cancel">×</button>
            {:else}
              <span class="body">{@render noteBody(n.body)}</span>
              <button class="tsnbtn" onclick={() => startEditNoteBody(n)} title="edit the text — type @ to link a clip">📝</button>
              <button class="tsnbtn" onclick={() => retimeNote(n)} title="move to the current playhead ({fmt(curMs)})">↻</button>
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
        <h4>This clip's references</h4>
        {#each refs.outgoing as r (r.id)}{@render refCard(r, true)}{/each}
        {#if refs.outgoing.length === 0 && !addingRef}<span class="faint">none yet — add one below, or @-mention a clip in a note.</span>{/if}
        {#if addingRef}
          <div class="refadd">
            {#if refPickedId === null}
              <input class="field" style:width="100%" placeholder="search clips to reference…" bind:value={refClipQuery} />
              <div class="refadd-list">
                {#each assets.filter((a) => a.id !== d.id && a.title.toLowerCase().includes(refClipQuery.toLowerCase())).slice(0, 14) as a (a.id)}
                  <button class="refadd-item" onclick={() => pickRefClip(a)}><img src={a.thumbnail_url} alt="" onerror={hideBrokenImg} />{a.title}</button>
                {/each}
                {#if assets.filter((a) => a.id !== d.id && a.title.toLowerCase().includes(refClipQuery.toLowerCase())).length === 0}<div class="faint" style:padding="6px 8px" style:font-size="12px">no matching clips loaded</div>{/if}
              </div>
              <button class="btn sm" onclick={resetRefAdd}>Cancel</button>
            {:else}
              <div class="ref-picked"><img src="/thumbnails/{refPickedId}" alt="" onerror={hideBrokenImg} /><b>{refPickedTitle}</b><button class="x" onclick={() => { refPickedId = null; refPickedNoteMs = null; refPickedClipNotes = null; }} title="pick a different clip">change</button></div>
              {#if refPickedClipNotes === null}
                <div class="faint" style:width="100%" style:font-size="11.5px">loading the clip's moments…</div>
              {:else if refPickedClipNotes.length > 0}
                <div class="ref-moment-pick">
                  <button class="ref-moment-opt" class:on={refPickedNoteMs === null} onclick={() => (refPickedNoteMs = null)}>— whole clip —</button>
                  {#each refPickedClipNotes as n (n.id)}
                    <button class="ref-moment-opt" class:on={refPickedNoteMs === n.timestamp_ms} onclick={() => (refPickedNoteMs = n.timestamp_ms)}><span class="ts-badge">{fmt(n.timestamp_ms)}</span> {n.body || '(no text)'}</button>
                  {/each}
                </div>
              {/if}
              <textarea class="field" rows="2" placeholder="description (optional)" bind:value={refDesc}></textarea>
              <div class="refadd-actions">
                <button class="btn sm primary" onclick={addRef}>Add reference</button>
                <button class="btn sm" onclick={resetRefAdd}>Cancel</button>
              </div>
            {/if}
          </div>
        {:else}
          <button class="btn sm" style:margin-top="6px" onclick={() => { resetRefAdd(); addingRef = true; }}>+ Add a reference</button>
        {/if}
        <h4>Clips referencing this one</h4>
        {#each refs.incoming as r (r.id)}{@render refCard(r, false)}{/each}
        {#if refs.incoming.length === 0}<span class="faint">nothing references this clip yet.</span>{/if}
        <h4>File</h4>
        {#each d.paths as p (p.path)}<div class="src" class:miss={!p.present} title={p.path}>{p.present ? '✓' : '✗'} {p.path}</div>{/each}
        <div class="faint" style:font-size="11px" style:margin-top="6px">Rename via the clip's title above — it renames the file itself.</div>
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

{#snippet folderNode(f: FolderNode, depth: number)}
  <div class="folder-row" style:padding-left={(depth * 14) + 'px'}>
    {#if f.children.length > 0}
      <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
      <span class="folder-expand" role="button" tabindex="0" onclick={() => toggleFolderExpand(f.path)}>{expandedFolders.has(f.path) ? '▾' : '▸'}</span>
    {:else}
      <span class="folder-expand-spacer"></span>
    {/if}
    <button class="folder-name" class:on={folderFilter === f.path} onclick={() => selectFolder(f.path)} title={f.path}>
      📁 {f.name} <span class="ct">{f.count}</span>
    </button>
  </div>
  {#if expandedFolders.has(f.path) && f.children.length > 0}
    {#each f.children as child (child.path)}
      {@render folderNode(child, depth + 1)}
    {/each}
  {/if}
{/snippet}
{#snippet tagFace(t: Tag)}{#if t.image_ref}<img class="tagimg" src="/api/tag-images/{t.image_ref}" alt="" />{:else if t.icon}{t.icon}{/if}{/snippet}
{#snippet catNavNode(g: TagGroup, depth: number)}<button class="catnav" class:on={categoryPageId === g.id} style:padding-left={6 + depth * 14 + 'px'} style:--c={g.color || 'var(--text-3)'} onclick={() => openCategoryPage(g)}>📁 {g.name}</button>{#each childPageCategories(g.id) as c (c.id)}{@render catNavNode(c, depth + 1)}{/each}{/snippet}
{#snippet noteBody(body: string)}{#each splitMentions(body) as seg}{#if seg.id != null}{@const nm = seg.noteId != null ? detail?.mentioned_notes?.[String(seg.noteId)] : undefined}<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions --><span class="mention" role="link" tabindex="0" onclick={() => (seg.noteId != null ? navToNote(seg.id ?? 0, seg.noteId) : navTo(seg.id ?? 0))} onmouseenter={(e) => showMentionPopup(e, seg.id ?? 0, seg.text, nm?.body, nm?.timestamp_ms)} onmouseleave={hideMentionPopup}>@{seg.text}{#if nm}<span class="ts-badge" style:margin-left="4px">{fmt(nm.timestamp_ms)}</span>{/if}</span>{:else}{seg.text}{/if}{/each}{/snippet}
{#snippet refCard(r: ReferenceView, outgoing: boolean)}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="ref-card" onclick={() => { if (editingRefId !== r.id) { if (outgoing && r.to_timestamp_ms != null) navToAt(r.other_asset_id, r.to_timestamp_ms); else openClip(r.other_asset_id); } }} title="open this clip">
    <img src="/thumbnails/{r.other_asset_id}" alt="" onerror={hideBrokenImg} />
    <div class="ref-card-body">
      <div class="ref-card-title">{r.other_asset_title}{#if r.to_timestamp_ms != null}<span class="ts-badge" style:margin-left="6px">{fmt(r.to_timestamp_ms)}</span>{/if}</div>
      {#if r.to_note_body !== null && r.to_note_body.trim()}<div class="ref-card-desc"><span class="faint" style:font-size="10.5px">note: </span>{r.to_note_body.trim()}</div>{/if}
      {#if editingRefId === r.id}
        <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
        <div onclick={(e) => e.stopPropagation()}>
          <textarea class="field ref-desc-edit" rows="2" bind:value={refDescDraft} bind:this={refDescEl} placeholder="description"
                    onkeydown={(e) => { e.stopPropagation(); if (e.key === 'Escape') editingRefId = null; else if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void saveRefDesc(r.id); } }}></textarea>
          <div class="ref-desc-actions">
            <button class="btn sm primary" onclick={() => saveRefDesc(r.id)}>Save</button>
            <button class="btn sm" onclick={() => (editingRefId = null)}>Cancel</button>
          </div>
        </div>
      {:else if (r.note || r.label).trim()}
        <div class="ref-card-desc">{(r.note || r.label).trim()}</div>
      {/if}
    </div>
    <div class="ref-card-actions">
      <button class="x edit" onclick={(e) => { e.stopPropagation(); startEditRefDesc(r); }} title="edit the description">✎</button>
      <button class="x" onclick={(e) => { e.stopPropagation(); deleteRef(r.id); }} title="remove this reference">×</button>
    </div>
  </div>
{/snippet}
{#if showSettings && cfg}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="modal-bg" onclick={(e) => { if (e.target === e.currentTarget) closeSettings(); }}>
    <div class="modal settings">
      <div class="mtop"><h3>Settings</h3><span style:flex="1"></span><button class="btn sm" onclick={closeSettings}>Close</button></div>
      <div class="settings-tabs">
        <button class:on={settingsTab === 'editing'} onclick={() => (settingsTab = 'editing')}>Editing</button>
        <button class:on={settingsTab === 'player'} onclick={() => (settingsTab = 'player')}>Player</button>
        <button class:on={settingsTab === 'keys'} onclick={() => (settingsTab = 'keys')}>Keyboard</button>
        <button class:on={settingsTab === 'ffmpeg'} onclick={() => { settingsTab = 'ffmpeg'; void refreshFfmpegStatus(false); }}>FFmpeg</button>
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
          <label class="srow"><span class="slabel">Reference label for extracted clips <span class="faint" style:font-size="11px">("save / cut to new clip")</span></span>
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
        {:else if settingsTab === 'ffmpeg'}
          {#if ffmpegStatus}
            <div class="srow"><span class="slabel">Status</span>
              <span>{ffmpegStatus.available ? '✓ Available' : '✗ Not found'}{ffmpegStatus.available && ffmpegStatus.version ? ` — ${ffmpegStatus.version}` : ''}</span></div>
            {#if ffmpegStatus.ffmpeg_path}
              <div class="srow"><span class="slabel">ffmpeg</span>
                <span class="faint" style:font-size="12px" style:word-break="break-all">{ffmpegStatus.ffmpeg_path}{ffmpegStatus.configured_path ? '  (set here)' : ''}</span></div>
            {/if}
            {#if ffmpegStatus.ffprobe_path}
              <div class="srow"><span class="slabel">ffprobe</span>
                <span class="faint" style:font-size="12px" style:word-break="break-all">{ffmpegStatus.ffprobe_path}</span></div>
            {:else}
              <div class="srow"><span class="slabel">ffprobe</span><span class="faint" style:font-size="12px">— not found (video metadata falls back to the browser)</span></div>
            {/if}
            {#if !ffmpegStatus.enabled}
              <p class="faint" style:font-size="12px" style:margin-top="8px">FFmpeg is turned off in the configuration ([media.ffmpeg].enabled = false). Enable it in <code>local.toml</code> to use it.</p>
            {/if}
            <div class="srow" style:margin-top="10px"><span class="slabel"></span>
              <span style:display="flex" style:gap="8px" style:flex-wrap="wrap">
                {#if !ffmpegStatus.available && ffmpegStatus.can_install}
                  <button class="btn sm primary" onclick={startFfmpegInstall} disabled={ffmpegInstalling}>{ffmpegInstalling ? (ffmpegInstallMsg || 'Installing…') : 'Download & install FFmpeg'}</button>
                {:else if ffmpegInstalling}
                  <button class="btn sm" disabled>{ffmpegInstallMsg || 'Installing…'}</button>
                {/if}
                <button class="btn sm" onclick={pickFfmpegPath} disabled={ffmpegInstalling}>Use FFmpeg from another folder…</button>
                {#if ffmpegStatus.configured_path}
                  <button class="btn sm" onclick={resetFfmpegToAuto} disabled={ffmpegInstalling}>Back to auto-detect</button>
                {/if}
              </span></div>
            {#if !ffmpegStatus.can_install && !ffmpegStatus.available}
              <p class="faint" style:font-size="12px" style:margin-top="8px">Automatic install is Windows-only. On Linux/macOS, install ffmpeg with your package manager (e.g. <code>apt install ffmpeg</code> / <code>brew install ffmpeg</code>) and reopen Clippycap — or use the button above to point at it.</p>
            {/if}
          {:else}
            <p class="faint">Loading…</p>
          {/if}
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
        <!-- Categories: optional, user-created dimensions. Empty by default; the section only matters
             once you add one. Deleting a category keeps its tags (they become uncategorised). -->
        <div class="cat-mgr">
          <div class="cat-head"><b>Categories</b> <span class="faint" style:font-size="11px">optional — group tags into dimensions (e.g. players, maps)</span></div>
          {#each tagGroups as g (g.id)}
            <div class="trow"><span class="sw" style:background={g.color || 'var(--text-3)'}></span> {#if g.has_page}<button class="namelink" onclick={() => openCategoryPage(g)} title="open this category's hub page">{g.name} ↗</button>{:else}<b>{g.name}</b>{/if} <span class="faint" style:font-size="11px">{tags.filter((t) => t.group_id === g.id).length} tags</span><span style:flex="1"></span><label class="chk" title="give this category its own hub page"><input type="checkbox" checked={g.has_page} onchange={(e) => toggleGroupPage(g, (e.currentTarget as HTMLInputElement).checked)} /> page</label><button class="x" onclick={() => deleteCategory(g)} title="delete category (its tags become uncategorised)">🗑</button></div>
          {/each}
          <div class="trow">
            <input class="field" style:max-width="160px" placeholder="new category name" bind:value={newGroupName} onkeydown={(e) => { if (e.key === 'Enter') void createCategory(); }} />
            <select class="field" style:max-width="150px" bind:value={newGroupParentId} title="optional: nest under another category">
              <option value={null}>— top level —</option>
              {#each tagGroups as g (g.id)}<option value={g.id}>under: {g.name}</option>{/each}
            </select>
            <button class="btn sm" onclick={createCategory}>+ Category</button>
          </div>
        </div>
        <div class="cat-divider"></div>
        {#each tags as t (t.id)}
          <div class="trow" class:editing={editingTagId === t.id}><span class="sw" style:background={t.color}></span> {#if t.has_page}<button class="namelink" onclick={() => openTagPage(t)} title="open this tag's page">{@render tagFace(t)} {t.name} ↗</button>{:else}<b>{@render tagFace(t)} {t.name}</b>{/if} {#if t.group_id != null && tagGroupById.get(t.group_id)}<span class="cat-badge">{tagGroupById.get(t.group_id)?.name}</span>{/if} <span class="faint" style:font-size="11px">{t.asset_count ?? 0} clips</span><span style:flex="1"></span><button class="x" onclick={() => startEditTag(t)} title="edit">✎</button><button class="x" onclick={() => deleteTag(t.id)} title="delete">🗑</button></div>
        {/each}
        <div class="trow" style:margin-top="8px" style:flex-wrap="wrap">
          <input class="field" style:max-width="146px" placeholder={editingTagId === null ? 'new tag name' : 'name'} bind:value={newTagName} />
          <input type="color" bind:value={newTagColor} title="colour" />
          <input class="field" style:max-width="54px" placeholder="icon" maxlength="8" bind:value={tagIcon} disabled={tagImageRef !== null} title="an emoji shown before the name" />
          <label class="btn sm" title="upload an image to use as the icon (a copy is kept by the app)">{uploadingImg ? '…' : '📷'}<input type="file" accept="image/png,image/jpeg,image/webp,image/gif" onchange={pickTagImage} style:display="none" /></label>
          {#if tagImageRef}<img class="tagimg tagimg-prev" src="/api/tag-images/{tagImageRef}" alt="" /><button class="x" onclick={removeTagImage} title="remove the image">×</button>{/if}
          <select class="field" style:max-width="130px" bind:value={newTagGroupId} title="category">
            <option value={null}>— no category —</option>
            {#each tagGroups as g (g.id)}<option value={g.id}>{g.name}</option>{/each}
          </select>
          <label class="chk" title="give this tag its own page (notes + the clips that carry it)"><input type="checkbox" bind:checked={newTagHasPage} /> page</label>
          <button class="btn sm primary" onclick={saveTag}>{editingTagId === null ? '+ Create' : '💾 Save'}</button>
          {#if editingTagId !== null}<button class="btn sm" onclick={cancelEditTag}>Cancel</button>{/if}
        </div>
        <p class="faint" style:margin-top="10px" style:font-size="12px">Categories are optional and yours — group tags into dimensions (players, maps…) so the picker stays tidy. A tag (or a category) can have its own page: a notes write-up plus every clip that carries it. Click ✎ to edit a tag; 🗑 deletes it everywhere.</p>
      </div>
    </div>
  </div>
{/if}

{#if showBulkTagModal}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="modal-bg" onclick={(e) => { if (e.target === e.currentTarget) closeBulkTagModal(); }}>
    <div class="modal bulk-tag-modal">
      <div class="mtop"><h3>Tag {bulkTagModalSize} clip{bulkTagModalSize === 1 ? '' : 's'}</h3><span style:flex="1"></span><button class="btn sm" onclick={closeBulkTagModal}>Close</button></div>
      <div class="settings-tabs">
        <button class:on={bulkTagMode === 'modify'} onclick={() => (bulkTagMode = 'modify')}>Add / Remove (diff)</button>
        <button class:on={bulkTagMode === 'replace'} onclick={() => (bulkTagMode = 'replace')}>Replace (override)</button>
      </div>
      <div class="mbody">
        {#if bulkTagMode === 'modify'}
          <p class="faint" style:font-size="12px" style:margin-bottom="8px">
            Per-tag <kbd>+</kbd> / <kbd>−</kbd> queue additions and removals. Tags you leave alone stay exactly as they are on each clip.
          </p>
        {:else}
          <p class="faint" style:font-size="12px" style:margin-bottom="8px">
            The checked tags become the EXACT tag set on each selected clip. Any other tags currently on those clips are removed.
          </p>
        {/if}
        <input class="field" placeholder="search tags…" bind:value={bulkTagSearch} style:width="100%" style:margin-bottom="10px" />
        <div class="bulk-tag-list">
          {#if filteredBulkTags.length === 0}
            <p class="faint" style:font-size="12px" style:padding="12px 0" style:text-align="center">{tags.length === 0 ? 'No tags yet — create some in the Tags modal first.' : 'No tags match your search.'}</p>
          {/if}
          {#each filteredBulkTags as t (t.id)}
            {@const have = bulkTagCounts.get(t.id) ?? 0}
            {#if bulkTagMode === 'modify'}
              {@const queuedAdd = bulkTagAdd.has(t.id)}
              {@const queuedRemove = bulkTagRemove.has(t.id)}
              {@const missing = bulkTagModalSize - have}
              <div class="bulk-tag-row" class:queued-add={queuedAdd} class:queued-remove={queuedRemove}>
                <span class="pill" style:background={t.color}>{@render tagFace(t)} {t.name}</span>
                <span class="bulk-tag-counts" title="clips currently with this tag, out of selected">{have}/{bulkTagModalSize}</span>
                <span class="bulk-tag-preview">
                  {#if queuedAdd}<span class="bulk-tag-preview-add">+{missing}</span>
                  {:else if queuedRemove}<span class="bulk-tag-preview-remove">−{have}</span>{/if}
                </span>
                <button class="bulk-tag-plus" class:on={queuedAdd} disabled={missing === 0} onclick={() => toggleBulkTagAdd(t.id)} title={missing === 0 ? 'all selected clips already have this tag' : `queue + add to ${missing} clip${missing === 1 ? '' : 's'} that don\'t have it`}>+</button>
                <button class="bulk-tag-minus" class:on={queuedRemove} disabled={have === 0} onclick={() => toggleBulkTagRemove(t.id)} title={have === 0 ? 'no selected clip has this tag' : `queue − remove from ${have} clip${have === 1 ? '' : 's'} that have it`}>−</button>
              </div>
            {:else}
              {@const checked = bulkTagReplaceSet.has(t.id)}
              {@const indeterminate = !checked && have > 0 && have < bulkTagModalSize}
              <label class="bulk-tag-replace-row">
                <input type="checkbox" {checked} indeterminate={indeterminate} onchange={(e) => toggleBulkTagReplace(t.id, (e.currentTarget as HTMLInputElement).checked)} />
                <span class="pill" style:background={t.color}>{@render tagFace(t)} {t.name}</span>
                <span class="bulk-tag-counts" title="clips currently with this tag, out of selected">{have}/{bulkTagModalSize}</span>
              </label>
            {/if}
          {/each}
        </div>
      </div>
      <div class="mfoot">
        <span class="faint" style:font-size="11.5px">
          {#if bulkTagMode === 'modify'}
            {bulkTagAdd.size} to add · {bulkTagRemove.size} to remove
          {:else}
            target set: {bulkTagReplaceSet.size} tag{bulkTagReplaceSet.size === 1 ? '' : 's'}
          {/if}
        </span>
        <span style:flex="1"></span>
        <button class="btn sm" onclick={closeBulkTagModal} disabled={bulkBusy}>Cancel</button>
        <button class="btn sm primary" onclick={applyBulkTag} disabled={bulkBusy || !canApplyBulkTag}>{bulkBusy ? 'Applying…' : 'Apply'}</button>
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
                    onkeydown={(e) => { if (onMentionKey(e)) return; if (e.key === 'Escape') { e.stopPropagation(); dialogCancel(); } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); dialogOk(); } }}></textarea>
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
      <div class="toast {t.kind}" class:sticky={t.sticky} onclick={() => toastDismiss(t.id)} title="dismiss">
        {#if t.sticky && t.kind === 'info'}<span class="spinner" aria-hidden="true"></span>{/if}{t.text}
      </div>
    {/each}
  </div>
{/if}
{#if mention}
  <div class="mention-dropdown" style:top={mention.top + 'px'} style:left={mention.left + 'px'}>
    {#if mention.kind === 'clip'}
      {#each mentionMatches as a, i (a.id)}
        <button class="mention-item" class:on={i === mentionIndex} onmousemove={() => (mentionIndex = i)} onmousedown={(e) => { e.preventDefault(); pickClipMention(a.id); }}><img src={a.thumbnail_url} alt="" onerror={hideBrokenImg} /><span>{a.title}</span></button>
      {/each}
      {#if mentionMatches.length === 0}<div class="faint" style:padding="6px 8px" style:font-size="12px">no matching clips</div>{/if}
    {:else}
      <div class="mention-head">a moment in that clip — pick one, or Esc to keep just the clip</div>
      {#if mentionNotes?.clipId !== mention.clipId}<div class="faint" style:padding="6px 8px" style:font-size="12px">loading…</div>{/if}
      {#each mentionNoteMatches as n, i (n.id)}
        <button class="mention-item" class:on={i === mentionIndex} onmousemove={() => (mentionIndex = i)} onmousedown={(e) => { e.preventDefault(); pickNoteMention(n.id); }}><span class="ts-badge">{fmt(n.timestamp_ms)}</span><span>{n.body || '(no text)'}</span></button>
      {/each}
      {#if mentionNotes?.clipId === mention.clipId && mentionNoteMatches.length === 0}<div class="faint" style:padding="6px 8px" style:font-size="12px">no timestamped notes in that clip</div>{/if}
    {/if}
  </div>
{/if}
{#if mentionPopup}
  <div class="mention-popup" style:top={mentionPopup.top + 'px'} style:left={mentionPopup.left + 'px'}>
    <img src="/thumbnails/{mentionPopup.id}" alt="" onerror={hideBrokenImg} />
    <div class="mention-popup-title">{mentionPopup.title}</div>
    {#if mentionPopup.body !== null || mentionPopup.time !== null}
      <div class="mention-popup-note">{#if mentionPopup.time !== null}<span class="ts-badge">{fmt(mentionPopup.time)}</span> {/if}{mentionPopup.body || '(note)'}</div>
    {/if}
  </div>
{/if}

<style>
  .app { display: flex; flex-direction: column; height: 100%; }
  header.topbar { display: flex; align-items: center; gap: 12px; padding: 0 14px; height: 60px; background: var(--bg-1); border-bottom: 1px solid var(--border); flex: none; }
  .brand { display: flex; align-items: center; gap: 10px; white-space: nowrap; }
  .brand-name { font-size: 16px; font-weight: 700; letter-spacing: 0.01em; color: var(--text); }
  .logo { width: 44px; height: 44px; object-fit: contain; flex: none; filter: drop-shadow(0 2px 4px rgba(0,0,0,0.55)); }
  .topbar-fill { flex: 1; min-width: 12px; align-self: stretch; }
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
  .tagchip { --c: var(--text-3); padding: 3px 9px; border-radius: 999px; font-size: 11.5px; font-weight: 600; background: var(--bg-3); border: 1px solid var(--border); border-left: 4px solid var(--c); color: var(--text-2); cursor: pointer; }
  .tagchip:hover { color: var(--text); border-color: color-mix(in srgb, var(--c) 60%, var(--border)); }
  .tagchip.create { border-left-color: var(--accent); color: var(--accent); }
  /* clip-detail tag picker: search box + category sub-headers */
  .tagpicker { margin-top: 6px; display: flex; flex-direction: column; gap: 6px; }
  .tagpicker-search { font-size: 12px; padding: 5px 8px; }
  .tagpicker-cat { --c: var(--text-3); font-size: 10px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; color: color-mix(in srgb, var(--c) 75%, var(--text-3)); border-left: 3px solid var(--c); padding-left: 6px; margin-top: 2px; }
  .tagchip:hover { color: var(--text); }
  /* tag manager: category management + badges */
  .cat-mgr { display: flex; flex-direction: column; gap: 4px; }
  .cat-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 2px; }
  .cat-divider { height: 1px; background: var(--border); margin: 10px 0; }
  .cat-badge { font-size: 10px; font-weight: 600; padding: 1px 6px; border-radius: 999px; background: var(--bg-3); border: 1px solid var(--border); color: var(--text-3); }
  .cat-badge.page { padding: 1px 4px; }
  .cat-sub { --c: var(--text-3); font-size: 10px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; color: color-mix(in srgb, var(--c) 70%, var(--text-3)); border-left: 3px solid var(--c); padding-left: 6px; margin: 4px 0 3px; }
  .cat-sub-link { display: block; width: 100%; text-align: left; background: transparent; border: none; border-left: 3px solid var(--c); cursor: pointer; }
  .cat-sub-link:hover { color: var(--accent); }
  .namelink { background: transparent; border: none; padding: 0; font-weight: 700; font-size: inherit; color: var(--text); cursor: pointer; }
  .namelink:hover { color: var(--accent); }
  /* tag/category page: a header banner above the (filtered) library grid */
  .tagpage { --c: var(--accent); border: 1px solid var(--border); border-left: 4px solid var(--c); border-radius: 8px; background: var(--bg-2); padding: 10px 12px; margin-bottom: 10px; display: flex; flex-direction: column; gap: 8px; }
  .tagpage-head { display: flex; align-items: center; gap: 10px; }
  .tagpage-title { font-size: 16px; font-weight: 700; display: inline-flex; align-items: center; gap: 6px; }
  .tagpage-notes { resize: vertical; font-size: 13px; }
  .tagpage-sub { font-size: 10px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; color: var(--text-3); margin-top: 2px; }
  .catnav { --c: var(--text-3); display: block; width: 100%; text-align: left; background: transparent; border: none; border-left: 3px solid var(--c); padding: 3px 6px; font-size: 12px; color: var(--text-2); cursor: pointer; border-radius: 0 4px 4px 0; }
  .catnav:hover { color: var(--text); background: var(--bg-2); }
  .catnav.on { color: var(--accent); background: var(--bg-2); }
  .chk { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; color: var(--text-3); cursor: pointer; }
  .chk input { accent-color: var(--accent); }
  .tagchip.on { color: #f7f9fb; border-color: transparent; text-shadow: -1px -1px 0 rgba(0,0,0,.72), 1px -1px 0 rgba(0,0,0,.72), -1px 1px 0 rgba(0,0,0,.72), 1px 1px 0 rgba(0,0,0,.72); }
  .tagchip .n { font-family: ui-monospace, monospace; font-size: 10px; opacity: .6; }
  .src { font-family: ui-monospace, monospace; font-size: 11px; color: var(--text-2); padding: 3px 0; word-break: break-all; }
  .src.miss { color: var(--text-3); text-decoration: line-through; }
  .ref-card { display: flex; gap: 10px; padding: 8px; background: var(--bg-2); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 6px; cursor: pointer; transition: border-color .1s; align-items: flex-start; }
  .ref-card:hover { border-color: #3a4350; }
  .ref-card > img { width: 92px; aspect-ratio: 16/9; object-fit: cover; border-radius: 5px; flex: none; background: #11141a; }
  .ref-card-body { flex: 1; min-width: 0; }
  .ref-card-title { font-size: 12.5px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ref-card-desc { font-size: 11.5px; color: var(--text-2); margin-top: 4px; white-space: pre-wrap; word-break: break-word; }
  .ref-card-actions { display: flex; flex-direction: column; gap: 2px; flex: none; }
  .ref-card-actions .x { font-size: 15px; line-height: 1; }
  .ref-card-actions .x.edit:hover { color: var(--accent); }
  .ref-desc-edit { width: 100%; box-sizing: border-box; font-size: 12px; resize: vertical; margin-top: 4px; }
  .ref-desc-actions { display: flex; gap: 6px; margin-top: 5px; }
  .savedview-row { display: flex; align-items: center; }
  .savedview-row .nav { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .savedview-row .x { flex: none; }
  .ref-picked { display: flex; align-items: center; gap: 8px; width: 100%; font-size: 12.5px; }
  .ref-picked img { width: 52px; aspect-ratio: 16/9; object-fit: cover; border-radius: 4px; flex: none; background: #11141a; }
  .ref-picked b { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .refadd-actions { display: flex; gap: 6px; width: 100%; }
  .ref-moment-pick { width: 100%; max-height: 160px; overflow-y: auto; display: flex; flex-direction: column; gap: 2px; }
  .ref-moment-opt { display: flex; align-items: center; gap: 6px; width: 100%; text-align: left; padding: 4px 6px; border-radius: 5px; font-size: 11.5px; color: var(--text-2); background: transparent; border: 1px solid transparent; cursor: pointer; }
  .ref-moment-opt:hover { background: var(--bg-3); color: var(--text); }
  .ref-moment-opt.on { background: var(--accent-soft); border-color: var(--accent); color: var(--text); }
  .refadd { margin-top: 6px; padding: 8px; background: var(--bg-2); border: 1px solid var(--border); border-radius: 7px; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .refadd textarea { width: 100%; box-sizing: border-box; resize: vertical; font-size: 12px; }
  .refadd input.field { flex: 1; min-width: 110px; }
  .refadd select { padding: 3px 6px; background: var(--bg-1); color: var(--text); border: 1px solid var(--border); border-radius: 5px; }
  .refadd-list { width: 100%; max-height: 210px; overflow-y: auto; display: flex; flex-direction: column; gap: 2px; }
  .refadd-item { display: flex; align-items: center; gap: 8px; width: 100%; text-align: left; padding: 4px 6px; border-radius: 5px; font-size: 12px; color: var(--text); background: transparent; border: none; cursor: pointer; }
  .refadd-item:hover { background: var(--bg-3); }
  .refadd-item img { width: 40px; height: 24px; object-fit: cover; border-radius: 3px; flex: none; background: #11141a; }
  /* main is a flex column: the grid scrolls, the pager is a fixed footer flush at the bottom */
  main { flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0; }
  .lib-scroll { flex: 1; overflow-y: auto; padding: 14px; min-height: 0; }
  .bar { color: var(--text-2); margin-bottom: 10px; }
  .err { background: #4a1d1d; border: 1px solid #6b2b2b; border-radius: 7px; padding: 8px 11px; margin-bottom: 10px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 14px; }
  .grid-empty { padding: 48px 0; text-align: center; color: var(--text-3); font-size: 13px; }
  .card { text-align: left; background: var(--bg-1); border: 1px solid var(--border); border-radius: var(--r); overflow: hidden; transition: .12s; }
  .card:hover { border-color: #3a4350; transform: translateY(-2px); }
  .thumb { position: relative; aspect-ratio: 16/9; background: linear-gradient(135deg, #1c2027, #11141a); display: grid; place-items: center; overflow: hidden; }
  .thumb img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
  .thumb .film { font-size: 28px; color: #39424f; }
  .thumb .dur { position: absolute; right: 6px; bottom: 6px; background: rgba(0,0,0,.72); padding: 1px 5px; border-radius: 4px; font-size: 11px; font-family: ui-monospace, monospace; }
  .thumb .dur.pending { color: var(--text-3); letter-spacing: 2px; }   /* "…" placeholder until enrichment fills the duration */
  .thumb .badge { position: absolute; left: 6px; bottom: 6px; background: rgba(0,0,0,.66); padding: 1px 6px; border-radius: 5px; font-size: 10.5px; font-weight: 700; }
  .thumb .badge.bnew { top: 6px; bottom: auto; background: var(--amber); color: #0e1116; }
  .cb { padding: 8px 9px; }
  .cb .ct { font-size: 12.5px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cb .cdate { font-size: 11px; color: var(--text-3); margin-top: 1px; }
  .cb .tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
  .pill { color: #f7f9fb; text-shadow: -1px -1px 0 rgba(0,0,0,.72), 1px -1px 0 rgba(0,0,0,.72), -1px 1px 0 rgba(0,0,0,.72), 1px 1px 0 rgba(0,0,0,.72); }
  .pill .x { font-weight: 800; }
  .overlay { position: fixed; inset: 0; background: var(--bg); display: flex; flex-direction: column; z-index: 50; }
  .otop { display: flex; align-items: center; gap: 12px; padding: 0 14px; height: 60px; background: var(--bg-1); border-bottom: 1px solid var(--border); flex: none; }
  .otop-fill { flex: 1; min-width: 12px; align-self: stretch; }
  .otitle { font-weight: 700; font-size: 14.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
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
  .tsn .body { flex: 1; font-size: 13px; word-break: break-word; white-space: pre-wrap; }
  .tsnbtn { background: transparent; border: none; padding: 0 3px; color: var(--text-3); cursor: pointer; transition: color 0.12s ease; font: inherit; }
  .tsnbtn:hover { color: var(--accent); }
  .note-display { white-space: pre-wrap; word-break: break-word; font-size: 13px; min-height: 44px; padding: 7px 9px; background: var(--bg-2); border: 1px solid var(--border); border-radius: 7px; cursor: text; line-height: 1.45; }
  .note-display:hover { border-color: #3a4350; }
  .mention { color: var(--accent); cursor: pointer; text-decoration: underline; font-weight: 600; }
  .mention:hover { color: #a78bfa; }
  .notebody-edit { flex: 1; min-width: 110px; font-size: 12px; resize: vertical; }
  .mention-dropdown { position: fixed; z-index: 70; width: 240px; max-height: 240px; overflow-y: auto; background: var(--bg-1); border: 1px solid var(--border); border-radius: 8px; box-shadow: 0 6px 22px rgba(0,0,0,.5); padding: 4px; }
  .mention-item { display: flex; align-items: center; gap: 8px; width: 100%; text-align: left; padding: 4px 6px; border-radius: 5px; font-size: 12px; color: var(--text); background: transparent; border: none; cursor: pointer; }
  .mention-item:hover, .mention-item.on { background: var(--bg-3); }
  .mention-item img { width: 38px; height: 22px; object-fit: cover; border-radius: 3px; flex: none; background: #11141a; }
  .mention-item span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mention-popup { position: fixed; z-index: 75; pointer-events: none; background: var(--bg-1); border: 1px solid var(--border); border-radius: 8px; box-shadow: 0 6px 22px rgba(0,0,0,.55); padding: 6px; width: 200px; }
  .mention-popup img { width: 100%; aspect-ratio: 16/9; object-fit: cover; border-radius: 5px; background: #11141a; display: block; }
  .mention-popup-title { font-size: 12px; font-weight: 600; margin-top: 5px; word-break: break-word; }
  .mention-popup-note { font-size: 11px; color: var(--text-2); margin-top: 5px; padding-top: 5px; border-top: 1px solid var(--border); white-space: pre-wrap; word-break: break-word; max-height: 64px; overflow: hidden; }
  .mention-head { font-size: 10.5px; color: var(--text-3); padding: 4px 8px 2px; }
  .ts-badge { font-family: ui-monospace, monospace; font-size: 10px; font-weight: 700; color: var(--amber); background: rgba(240,179,79,.14); border: 1px solid rgba(240,179,79,.3); padding: 0 4px; border-radius: 4px; flex: none; white-space: nowrap; }
  @keyframes noteglow { 0% { box-shadow: 0 0 0 0 rgba(124,92,245,0); } 22% { box-shadow: 0 0 0 4px rgba(124,92,245,.7); } 100% { box-shadow: 0 0 0 0 rgba(124,92,245,0); } }
  .tsn.glow { animation: noteglow 1.6s ease; }
  .toasts { position: fixed; bottom: 16px; right: 16px; z-index: 90; display: flex; flex-direction: column; gap: 8px; max-width: 360px; }
  .toast { background: var(--bg-1); border: 1px solid var(--border); border-left: 3px solid var(--text-3); border-radius: 8px; padding: 9px 13px; font-size: 12.5px; box-shadow: 0 6px 22px rgba(0,0,0,.5); cursor: pointer; animation: toastin .18s ease; word-break: break-word; line-height: 1.4; }
  .toast.error { border-left-color: #ef5b5b; }
  .toast.success { border-left-color: #56c271; }
  .toast.info { border-left-color: var(--accent); }
  .toast.sticky { cursor: default; }   /* sticky toasts hold until the work completes (then update or dismiss) */
  .toast .spinner { display: inline-block; width: 10px; height: 10px; border: 2px solid rgba(255,255,255,.18);
                    border-top-color: rgba(255,255,255,.65); border-radius: 50%; animation: spin .7s linear infinite;
                    vertical-align: -1px; margin-right: 7px; }
  @keyframes spin { to { transform: rotate(360deg); } }
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
  .selbox.on { background: var(--accent); border-color: var(--accent); color: #ffffff; }
  .bulkbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 7px 11px; margin-bottom: 10px; background: var(--accent-soft); border: 1px solid var(--accent); border-radius: 8px; font-size: 13px; }
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
  .timeline .sel { position: absolute; top: 0; bottom: 0; background: rgba(124,92,245,.22); border-left: 2px solid var(--accent); border-right: 2px solid var(--accent); }
  .timeline .nbar { position: absolute; top: 3px; bottom: 3px; background: rgba(240,179,79,.55); border-radius: 2px; }
  .timeline .ntick { position: absolute; top: 0; bottom: 0; width: 2px; margin-left: -1px; background: var(--amber); }
  .timeline .playhead { position: absolute; top: -2px; bottom: -2px; width: 2px; margin-left: -1px; background: #fff; box-shadow: 0 0 5px rgba(255,255,255,.7); pointer-events: none; }
  .timeline { touch-action: none; }
  .timeline .handle { position: absolute; top: 0; bottom: 0; width: 10px; margin-left: -5px; background: var(--accent); border-radius: 3px; cursor: ew-resize; z-index: 2; box-shadow: 0 0 0 1px rgba(0,0,0,.45); display: grid; place-items: center; touch-action: none; }
  .timeline .handle::after { content: ''; width: 2px; height: 60%; background: rgba(0,0,0,.55); border-radius: 1px; }
  .timeline .handle:hover { background: #a78bfa; }
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
  .kbd-edit.capturing { background: var(--accent); color: #ffffff; border-color: var(--accent); }
  .mfoot { display: flex; align-items: center; padding: 10px 16px; border-top: 1px solid var(--border); gap: 8px; background: var(--bg-1); }
  /* Invisible strips at the window's 4 edges + 4 corners; the capture-phase mousedown listener turns
     a click on one of these into a native Win32 resize (via WM_NCLBUTTONDOWN). z-index very high so
     they sit above the topbar / WindowControls; corners are 6 px so they take the *very corner* only
     and don't eat clicks on the min/max/close buttons. */
  .resize-edge { position: fixed; z-index: 9999; }
  .resize-edge.n  { top: 0;    left: 6px; right: 6px;  height: 4px; cursor: ns-resize; }
  .resize-edge.s  { bottom: 0; left: 6px; right: 6px;  height: 4px; cursor: ns-resize; }
  .resize-edge.w  { top: 6px;  bottom: 6px; left: 0;   width: 4px;  cursor: ew-resize; }
  .resize-edge.e  { top: 6px;  bottom: 6px; right: 0;  width: 4px;  cursor: ew-resize; }
  .resize-edge.nw { top: 0;    left: 0;    width: 6px;  height: 6px; cursor: nwse-resize; }
  .resize-edge.ne { top: 0;    right: 0;   width: 6px;  height: 6px; cursor: nesw-resize; }
  .resize-edge.sw { bottom: 0; left: 0;    width: 6px;  height: 6px; cursor: nesw-resize; }
  .resize-edge.se { bottom: 0; right: 0;   width: 6px;  height: 6px; cursor: nwse-resize; }
  /* --- folder tree (sidebar) + folder-filter breadcrumb -------------------------------------- */
  .sec-clear { background: transparent; border: none; color: var(--text-3); font-size: 11px; font-weight: 700; cursor: pointer; margin-left: 6px; padding: 0 4px; }
  .sec-clear:hover { color: #ef5b5b; }
  .folder-tree { display: flex; flex-direction: column; gap: 0; max-height: 40vh; overflow-y: auto; margin: 0 0 4px; }
  .folder-row { display: flex; align-items: center; gap: 2px; min-width: 0; }
  .folder-expand { width: 14px; flex: none; cursor: pointer; color: var(--text-3); font-size: 9px; padding: 0 2px; user-select: none; line-height: 1; }
  .folder-expand:hover { color: var(--text); }
  .folder-expand-spacer { width: 14px; flex: none; }
  .folder-name { flex: 1; min-width: 0; text-align: left; padding: 3px 6px; border-radius: 5px; font-size: 12px; color: var(--text-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; background: transparent; border: none; cursor: pointer; }
  .folder-name:hover { background: var(--bg-2); color: var(--text); }
  .folder-name.on { background: var(--accent-soft); color: var(--text); font-weight: 600; }
  .folder-name .ct { font-family: ui-monospace, monospace; font-size: 10.5px; color: var(--text-3); margin-left: 6px; font-weight: normal; }
  .folder-crumb { display: flex; align-items: center; gap: 10px; padding: 7px 11px; margin-bottom: 10px; background: var(--accent-soft); border: 1px solid var(--accent); border-radius: 8px; font-size: 12.5px; }
  .folder-crumb-path { font-family: ui-monospace, monospace; font-size: 11.5px; flex: 0 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); min-width: 0; }
  /* --- bulk-tag modal (Add/Remove diff + Replace) ------------------------------------------- */
  .modal.bulk-tag-modal { width: min(620px, 100%); }
  .bulk-tag-list { display: flex; flex-direction: column; gap: 4px; max-height: 52vh; overflow-y: auto; padding-right: 4px; }
  .bulk-tag-row { display: flex; align-items: center; gap: 8px; padding: 5px 8px; border-radius: 6px; background: var(--bg-2); border: 1px solid var(--border); transition: border-color .12s, background .12s; }
  .bulk-tag-row.queued-add { border-color: #56c271; background: rgba(86,194,113,.08); }
  .bulk-tag-row.queued-remove { border-color: #ef5b5b; background: rgba(239,91,91,.08); }
  .bulk-tag-row .pill { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bulk-tag-counts { font-family: ui-monospace, monospace; font-size: 10.5px; color: var(--text-3); white-space: nowrap; flex: none; min-width: 44px; text-align: right; }
  .bulk-tag-preview { font-family: ui-monospace, monospace; font-size: 10.5px; font-weight: 700; min-width: 38px; flex: none; text-align: right; }
  .bulk-tag-preview-add { color: #56c271; }
  .bulk-tag-preview-remove { color: #ef5b5b; }
  .bulk-tag-plus, .bulk-tag-minus { width: 28px; height: 26px; border-radius: 5px; border: 1px solid var(--border); background: var(--bg-1); color: var(--text-2); cursor: pointer; font-weight: 700; padding: 0; line-height: 1; font-size: 14px; flex: none; transition: background .12s, color .12s, border-color .12s; }
  .bulk-tag-plus:hover:not(:disabled) { background: rgba(86,194,113,.18); color: #56c271; border-color: #56c271; }
  .bulk-tag-minus:hover:not(:disabled) { background: rgba(239,91,91,.18); color: #ef5b5b; border-color: #ef5b5b; }
  .bulk-tag-plus.on { background: #56c271; color: #0e1116; border-color: #56c271; }
  .bulk-tag-minus.on { background: #ef5b5b; color: #fff; border-color: #ef5b5b; }
  .bulk-tag-plus:disabled, .bulk-tag-minus:disabled { opacity: .35; cursor: default; }
  .bulk-tag-replace-row { display: flex; align-items: center; gap: 8px; padding: 5px 8px; border-radius: 6px; background: var(--bg-2); border: 1px solid var(--border); cursor: pointer; }
  .bulk-tag-replace-row:hover { border-color: #3a4350; }
  .bulk-tag-replace-row .pill { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bulk-tag-replace-row input[type="checkbox"] { width: 16px; height: 16px; cursor: pointer; flex: none; accent-color: var(--accent); }
</style>
