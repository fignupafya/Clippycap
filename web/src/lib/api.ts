// Thin typed client for the Clippycap HTTP API. Paths are relative, so the same build works
// behind the backend (production) and behind Vite's dev proxy.

export interface AssetSummary {
  id: number; media_type: string; title: string; size_bytes: number;
  metadata: Record<string, unknown>; thumbnail_url: string; stream_url: string;
  tag_ids: number[]; note_count: number; reference_count: number; is_new: boolean;
  last_opened_at: string | null;
  metadata_pending: boolean;   // true until a scan's enrichment phase has read duration / resolution
}
export interface AssetPage { items: AssetSummary[]; total: number; offset: number; limit: number; }
export interface Note {
  id: number; asset_id: number; body: string;
  timestamp_ms: number | null; end_timestamp_ms?: number | null; tag_ids: number[];
}
export interface AssetDetail extends AssetSummary {
  paths: { path: string; present: boolean; volume_id: string | null }[];
  general_note: string | null; general_note_id: number | null; timestamped_notes: Note[];
  mentioned_assets: Record<string, string>;   // "@{id}"-mentioned clips in this asset's notes -> title
  mentioned_notes: Record<string, { clip_id: number; body: string; timestamp_ms: number }>;  // "@{clip#note}" -> note info
}
export interface Tag {
  id: number; name: string; color: string; icon: string | null; image_ref: string | null;
  description: string; sort_order: number; asset_count?: number;
  group_id: number | null;   // tag category; null => uncategorised
  has_page: boolean;         // this tag has its own page (notes + tagged clips)
  notes: string;             // markdown body shown on the tag's page
}
export interface TagGroup {
  id: number; name: string; color: string; sort_order: number; has_page: boolean;
}
export interface ReferenceType { id: number; name: string; reverse_name: string | null; color: string; sort_order: number; }
export interface ReferenceView {
  id: number; from_asset_id: number; to_asset_id: number;
  type_id: number | null; type_name: string | null; label: string; note: string;
  from_timestamp_ms: number | null; to_timestamp_ms: number | null;
  other_asset_id: number; other_asset_title: string; to_note_body: string | null;
}
export interface Source { id: number; path: string; recursive: boolean; enabled: boolean; media_types: string[]; last_scanned_at: string | null; }
export interface SavedView { id: number; name: string; filter_json: string; sort_key: string; sort_order: number; }
export interface Job { id: string; name: string; state: string; scanned: number; total: number | null; message: string; error: string | null; }
export interface Folder { path: string; count: number; }
export interface Health { name: string; ffmpeg: boolean; media_types: string[]; plugins: string[]; }
export interface EditingConfig {
  reencode: boolean; reencode_crf: number; reencode_preset: string;
  keep_original_backup: boolean; new_clip_name_template: string; excerpt_reference_type: string;
}
export interface PlayerConfig {
  speeds: number[]; default_speed: number;
  skip_seconds: number; skip_seconds_fine: number;
  pause_on_add_note: boolean; prefer_rvfc: boolean;
}
export interface UiConfig {
  theme: string; accent_color: string; grid_density: string; default_sort: string;
  locale: string; markdown_in_notes: boolean; page_size: number;
}
export interface AppConfig {
  editing: EditingConfig; player: PlayerConfig; keybindings: Record<string, string>;
  ui: UiConfig;
  [key: string]: unknown;
}
export interface FfmpegStatus {
  available: boolean; ffprobe_available: boolean;
  ffmpeg_path: string | null; ffprobe_path: string | null; version: string | null;
  enabled: boolean; configured_path: string | null;
  offer_install: boolean; can_install: boolean;
  installing: boolean; install_job_id: string | null;
}
type EditedAsset = { id: number; title: string };

interface AssetQuery {
  tags_all?: number[]; tags_any?: number[]; untagged?: boolean; text?: string;
  never_opened?: boolean; only_missing?: boolean; sort?: string; offset?: number; limit?: number;
  path_under?: string;       // folder filter: clips whose path is this folder or any descendant
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { 'content-type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* ignore */ }
    throw new Error(`${res.status}: ${detail}`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function qs(params: Record<string, unknown>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === false || v === '') continue;
    if (Array.isArray(v)) v.forEach((x) => sp.append(k, String(x)));
    else sp.append(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : '';
}

export const api = {
  getConfig: () => req<AppConfig>('GET', '/api/config'),
  updateConfig: (patch: { editing?: EditingConfig; player?: PlayerConfig; keybindings?: Record<string, string> }) =>
    req<AppConfig>('PUT', '/api/config', patch),
  getHealth: () => req<Health>('GET', '/api/health'),
  getFfmpegStatus: () => req<FfmpegStatus>('GET', '/api/ffmpeg'),
  installFfmpeg: () => req<{ job_id: string }>('POST', '/api/ffmpeg/install'),
  setFfmpegPath: (path: string) => req<FfmpegStatus>('POST', '/api/ffmpeg/path', { path }),
  resetFfmpegPath: () => req<FfmpegStatus>('POST', '/api/ffmpeg/auto'),
  dismissFfmpegPrompt: () => req<void>('POST', '/api/ffmpeg/dismiss-prompt'),
  listAssets: (q: AssetQuery = {}) => req<AssetPage>('GET', `/api/assets${qs(q as Record<string, unknown>)}`),
  getAsset: (id: number) => req<AssetDetail>('GET', `/api/assets/${id}`),
  markOpened: (id: number) => req<void>('POST', `/api/assets/${id}/opened`),
  deleteAsset: (id: number, deleteFiles = false) =>
    req<void>('DELETE', `/api/assets/${id}${deleteFiles ? '?delete_files=true' : ''}`),
  renameFile: (id: number, name: string) => req<{ id: number; title: string }>('POST', `/api/assets/${id}/rename-file`, { name }),
  applyTag: (assetId: number, tagId: number) => req<void>('POST', `/api/assets/${assetId}/tags/${tagId}`),
  unapplyTag: (assetId: number, tagId: number) => req<void>('DELETE', `/api/assets/${assetId}/tags/${tagId}`),
  setGeneralNote: (assetId: number, body: string) => req<Note>('PUT', `/api/assets/${assetId}/notes/general`, { body }),
  addTimestampNote: (assetId: number, timestamp_ms: number, body: string, end_timestamp_ms?: number) =>
    req<Note>('POST', `/api/assets/${assetId}/notes`, { timestamp_ms, body, end_timestamp_ms }),
  deleteNote: (id: number) => req<void>('DELETE', `/api/notes/${id}`),
  updateNote: (id: number, body: string) => req<Note>('PATCH', `/api/notes/${id}`, { body }),
  listReferenceTypes: () => req<ReferenceType[]>('GET', '/api/reference-types'),
  getReferences: (assetId: number) => req<{ outgoing: ReferenceView[]; incoming: ReferenceView[] }>('GET', `/api/assets/${assetId}/references`),
  addReference: (body: { from_asset_id: number; to_asset_id: number; type_id?: number | null; label?: string; note?: string; from_timestamp_ms?: number | null; to_timestamp_ms?: number | null }) =>
    req<{ id: number }>('POST', '/api/references', body),
  updateReference: (id: number, note: string) => req<void>('PATCH', `/api/references/${id}`, { note }),
  deleteReference: (id: number) => req<void>('DELETE', `/api/references/${id}`),
  listSavedViews: () => req<SavedView[]>('GET', '/api/saved-views'),
  createSavedView: (v: { name: string; filter_json: string; sort_key: string; sort_order?: number }) =>
    req<{ id: number }>('POST', '/api/saved-views', v),
  deleteSavedView: (id: number) => req<void>('DELETE', `/api/saved-views/${id}`),
  setNoteTags: (noteId: number, tag_ids: number[]) => req<void>('PUT', `/api/notes/${noteId}/tags`, { tag_ids }),
  retimeNote: (noteId: number, timestamp_ms: number, end_timestamp_ms?: number) =>
    req<Note>('PUT', `/api/notes/${noteId}/time`, { timestamp_ms, end_timestamp_ms }),
  trimAsset: (id: number, start_ms: number, end_ms: number) =>
    req<EditedAsset>('POST', `/api/assets/${id}/trim`, { start_ms, end_ms }),
  removeSegment: (id: number, start_ms: number, end_ms: number) =>
    req<EditedAsset>('POST', `/api/assets/${id}/remove-segment`, { start_ms, end_ms }),
  extractSegment: (id: number, start_ms: number, end_ms: number, remove_from_source: boolean) =>
    req<EditedAsset>('POST', `/api/assets/${id}/extract-segment`, { start_ms, end_ms, remove_from_source }),
  listTags: () => req<Tag[]>('GET', '/api/tags'),
  createTag: (t: { name: string; color: string; icon?: string | null; image_ref?: string | null; sort_order?: number; group_id?: number | null; has_page?: boolean; notes?: string }) =>
    req<Tag>('POST', '/api/tags', t),
  updateTag: (id: number, t: Omit<Tag, 'id' | 'asset_count'>) => req<Tag>('PUT', `/api/tags/${id}`, t),
  setTagNotes: (id: number, notes: string) => req<Tag>('PUT', `/api/tags/${id}/notes`, { notes }),
  deleteTag: (id: number) => req<void>('DELETE', `/api/tags/${id}`),
  // tag categories (groups) -- user-created; the library starts with none.
  listTagGroups: () => req<TagGroup[]>('GET', '/api/tag-groups'),
  createTagGroup: (g: { name: string; color?: string; has_page?: boolean }) => req<TagGroup>('POST', '/api/tag-groups', g),
  updateTagGroup: (id: number, g: { name: string; color: string; has_page: boolean; sort_order: number }) => req<TagGroup>('PUT', `/api/tag-groups/${id}`, g),
  deleteTagGroup: (id: number) => req<void>('DELETE', `/api/tag-groups/${id}`),
  reorderTagGroups: (ids: number[]) => req<void>('POST', '/api/tag-groups/reorder', { ids }),
  uploadTagImage: async (file: File): Promise<{ image_ref: string }> => {
    const ext = (file.name.split('.').pop() ?? '').toLowerCase();
    const res = await fetch(`/api/tag-images?ext=${encodeURIComponent(ext)}`, {
      method: 'POST',
      headers: file.type ? { 'content-type': file.type } : undefined,
      body: file,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail ?? detail; } catch { /* ignore */ }
      throw new Error(`${res.status}: ${detail}`);
    }
    return (await res.json()) as { image_ref: string };
  },
  listSources: () => req<Source[]>('GET', '/api/sources'),
  addSource: (path: string) => req<Source>('POST', '/api/sources', { path, recursive: true, media_types: [] }),
  scanAll: () => req<{ job_id: string }>('POST', '/api/scan'),
  // Fast, hashing-free re-sync: pick up files renamed / moved / deleted outside the app.
  reconcile: () => req<{ changed: boolean; renamed: number; vanished: number; restored: number }>('POST', '/api/reconcile'),
  getJob: (id: string) => req<Job>('GET', `/api/jobs/${id}`),
  listJobs: () => req<Job[]>('GET', '/api/jobs'),
  listFolders: () => req<Folder[]>('GET', '/api/folders'),
  // Every matching asset id (no pagination) -- powers the bulk-bar "select all matching" link.
  listAssetIds: (q: AssetQuery = {}) => req<number[]>('GET', `/api/assets/ids${qs(q as Record<string, unknown>)}`),
  // For each tag id, how many of `ids` have that tag -- pre-fills the bulk-tag modal.
  assetTagCounts: (ids: number[]) => req<Record<string, number>>('POST', '/api/assets/tag-counts', { ids }),
  // Bulk tag change. `replace_with` (when set) overrides any add/remove and replaces each asset's
  // tag set with EXACTLY that. Otherwise add/remove are applied as a diff over existing tags.
  bulkTags: (ids: number[], ops: { add?: number[]; remove?: number[]; replace_with?: number[] | null }) =>
    req<{ added: number; removed: number }>('POST', '/api/assets/bulk-tags', { ids, ...ops }),
};
