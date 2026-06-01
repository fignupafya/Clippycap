// Frontend mirror of the backend LinkerDefinition (app/linking/types.py) plus small UI helpers.
// The builder edits one of these objects and serialises it back to definition_json. Kept permissive
// (optional fields) so a preset or a hand-edited JSON round-trips without loss.

export type FieldType = 'int' | 'float' | 'string' | 'datetime' | 'duration' | 'bool';

export interface FieldSource {
  kind: 'capture' | 'metadata' | 'attr' | 'const';
  name?: string;      // capture
  key?: string;       // metadata
  attr?: string;      // attr
  value?: unknown;    // const
}
export interface Step {
  op: string;
  value?: unknown; field?: string; fmt?: string; unit?: string; sep?: string;
  index?: number; end?: number; group?: number;
}
export interface FieldDef {
  name: string; type: FieldType; source: FieldSource; steps?: Step[];
  date_format?: string | null; tz?: 'local' | 'utc';
}
export interface SideSpec {
  template?: string | null;
  template_target?: 'stem' | 'name' | 'path' | 'folder';
  template_anchored?: boolean; case_insensitive?: boolean;
  fields: FieldDef[];
}
export interface Ref { side: 'clip' | 'file'; field: string; }
export interface Condition {
  op: string; left: Ref; right?: Ref | null; start?: Ref | null; end?: Ref | null;
  tolerance?: number | null; offset?: number; slack?: number; threshold?: number;
  weight?: number; case_insensitive?: boolean;
}
export interface MatchSpec { combine: 'all' | 'any'; conditions: Condition[]; min_score?: number; }
export interface ResolveSpec {
  strategy: 'keep_all' | 'best_per_clip' | 'best_per_file' | 'best_overall' | 'quota';
  per_clip_max?: number | null; per_clip_min?: number; per_file_max?: number | null;
  quota?: number | null; relative_threshold?: number | null; absolute_floor?: number;
  contested?: 'drop' | 'cascade' | 'flag'; ambiguity_margin?: number;
  tiebreak?: string[]; one_per_group?: Ref | null; stable?: boolean;
}
export interface OpenAction { name: string; extensions: string[]; program: string; args: string[]; }
export interface ActionsSpec { open_with: OpenAction[]; }
export interface AssetScope {
  media_type?: string | null; path_under?: string | null;
  tags_all?: number[]; tags_any?: number[]; in_categories?: number[];
  min_duration_ms?: number | null; max_duration_ms?: number | null;
}
export interface TargetScope {
  directories: string[]; recursive: boolean; extensions: string[];
  ignore_globs?: string[]; min_size?: number | null; max_size?: number | null;
}
export interface Definition {
  schema_version: number;
  source: AssetScope; target: TargetScope;
  clip: SideSpec; file: SideSpec;
  match: MatchSpec; resolve: ResolveSpec; actions: ActionsSpec;
  label_template?: string;
}

export function blankDefinition(): Definition {
  return {
    schema_version: 1,
    source: { media_type: 'video' },
    target: { directories: [], recursive: true, extensions: [] },
    clip: { fields: [] },
    file: { fields: [] },
    match: { combine: 'all', conditions: [] },
    resolve: { strategy: 'best_per_clip', tiebreak: ['score'] },
    actions: { open_with: [] },
    label_template: '%name%',
  };
}

export function parseDefinition(json: string): Definition {
  const d = JSON.parse(json) as Partial<Definition>;
  return { ...blankDefinition(), ...d } as Definition;
}

// Plain-language option lists for the dropdowns.
export const FIELD_TYPES: { value: FieldType; label: string }[] = [
  { value: 'string', label: 'Text' },
  { value: 'int', label: 'Number' },
  { value: 'float', label: 'Number (decimal)' },
  { value: 'datetime', label: 'Date & time' },
  { value: 'duration', label: 'Length of time' },
  { value: 'bool', label: 'Yes / No' },
];

export const CONDITION_OPS: { value: string; label: string; needs: string[] }[] = [
  { value: 'equals', label: 'equals', needs: ['right'] },
  { value: 'not_equals', label: 'does not equal', needs: ['right'] },
  { value: 'within', label: 'is within … of', needs: ['right', 'tolerance'] },
  { value: 'interval_contains', label: 'falls inside the interval', needs: ['start', 'end', 'slack'] },
  { value: 'interval_overlap', label: 'overlaps the interval', needs: ['right', 'start', 'end'] },
  { value: 'lt', label: 'is less than', needs: ['right'] },
  { value: 'gt', label: 'is greater than', needs: ['right'] },
  { value: 'contains', label: 'contains', needs: ['right'] },
  { value: 'startswith', label: 'starts with', needs: ['right'] },
  { value: 'fuzzy', label: 'is almost the same text as', needs: ['right', 'threshold'] },
];

export const STEP_OPS: { value: string; label: string; arg?: 'value' | 'field' | 'unit' | 'sep+index' | 'fmt' }[] = [
  { value: 'trim', label: 'Trim spaces' },
  { value: 'lower', label: 'Make lowercase' },
  { value: 'upper', label: 'Make uppercase' },
  { value: 'keep_digits', label: 'Keep only digits' },
  { value: 'keep_letters', label: 'Keep only letters' },
  { value: 'to_number', label: 'Treat as number' },
  { value: 'add', label: 'Add…', arg: 'value' },
  { value: 'sub', label: 'Subtract…', arg: 'value' },
  { value: 'mul', label: 'Multiply by…', arg: 'value' },
  { value: 'div', label: 'Divide by…', arg: 'value' },
  { value: 'round', label: 'Round' },
  { value: 'split_take', label: 'Split & take part…', arg: 'sep+index' },
  { value: 'replace', label: 'Replace…', arg: 'value' },
  { value: 'concat', label: 'Join with field…', arg: 'field' },
  { value: 'parse_date', label: 'Read as date' },
  { value: 'date_add', label: 'Add time…', arg: 'unit' },
  { value: 'parse_duration', label: 'Read as length' },
];

export const ATTRS: { value: string; label: string }[] = [
  { value: 'mtime', label: 'Modified date' },
  { value: 'created', label: 'Created date' },
  { value: 'size', label: 'File size' },
  { value: 'name', label: 'File name' },
  { value: 'stem', label: 'Name (no extension)' },
  { value: 'ext', label: 'Extension' },
  { value: 'folder', label: 'Folder name' },
  { value: 'folder_index', label: 'Position in folder' },
];

export function commaList(values: string[]): string { return values.join(', '); }
export function parseCommaList(text: string): string[] {
  return text.split(',').map((s) => s.trim()).filter(Boolean);
}
