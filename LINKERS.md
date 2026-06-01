# Clippycap Linkers — design

A **Linker** is a user-defined rule that automatically attaches *companion files* to assets in the
library, and optionally exposes a per-file-type "Open with" action next to the always-present
"Reveal in folder". Linkers are created/edited on their own page, enabled from the main menu, run as
a background job, and are configured through a UI that stays a one-click preset for the common case
but reaches **full capability without ever requiring code**.

**This is a domain-neutral, general file-linking system — not a "demo" feature.** A TF2 player
attaching demos to clips is *one* recipe; equally first-class:
- a **film editor** linking each video take to the Word/PDF of its scene's screenplay,
- a **podcaster** linking an episode's audio to its transcript and show-notes,
- a **photographer** linking an edited JPEG to its source RAW and the Lightroom sidecar,
- a **3D artist** linking a render to its `.blend`/project file,
- anyone linking a recording to "the file that was open / written / shot at the same time".

**Audience: not programmers.** The hard requirement that drives every UX decision below is that a
non-technical user must be able to use the **entire** power of the engine — every extraction, every
transform, every match mode — and **never hit a wall where they think "oh, this isn't possible."**
Everything is possible, and it stays friendly. The way we reconcile those (§9): power is reached by
**stacking visual, by-example steps**, never by switching into a formula/code modality.

This document is the full design: the mental model, every layer, the data model, the execution
model, the UX, and an exhaustive edge-case catalog. It is the source of truth for §16 of
ARCHITECTURE.md once built.

---

## 1. The mental model — Extract → Transform → Join

The original ask ("attach the demo recorded when this clip happened") looks specific, but the real
problem is general and worth naming, because every linking scenario is an instance of it:

> Two data sources each **encode the same real-world event differently** — a different timestamp
> (start vs end), a different counter, a different string layout, the signal living in the filename
> on one side and in file metadata on the other. Linking = **define, per side, how to read that
> source into a common comparison space, then join the two spaces with a predicate.**

So a Linker is four user-defined things, plus scoping and output:

1. **Extract** — pull named, *typed* variables out of each side (filename template, a path segment,
   a metadata field, a file attribute, a constant).
2. **Transform** — compute derived values from those variables (combine two counters, subtract a
   duration from an end-time to get a start-time, lower-case a map name, change units). This is the
   layer that absorbs every "but the two sides number/format things differently" problem.
3. **Join** — a predicate (or several, AND/OR) over the transformed values that, for a *single*
   (clip, file) pair, proposes it as a **candidate** with a **score** (0..1): equal, within a
   tolerance, one interval contains the other, fuzzy string match, etc.
4. **Resolve** — look at *all* candidates together and decide which actually become links, under the
   user's cardinality rules. This is the **global, relative** layer (§6): "give this contested file
   to the closest clip", "keep only the best 10", "no file used twice". Matching is a *selection over
   a graph*, not a bag of independent yes/no decisions — naming this is what stops the feature
   breaking the moment candidates compete.

Everything else — which clips, which files, what the result row looks like, how to open it — hangs
off those four. Holding this model makes the whole feature coherent instead of a pile of special
cases. The pipeline is **Extract → Transform → Join (candidates) → Resolve (winners) → Attach.**

The same three layers express wildly different scenarios — which is the proof the model is right:

**Example A — TF2 demo by time-interval (different time encodings):**
- **Extract:** clip → `T = recorded_at` (already computed by the video media type: filename →
  ffprobe `creation_time` → mtime). demo → `start = file.created` (Windows birthtime), `end =
  file.mtime` (last write = recording stopped).
- **Transform:** none needed (all three are datetimes → normalized to epoch seconds).
- **Join:** `start − slack ≤ T ≤ end + slack` (interval containment): *"which demo was recording at
  the instant this clip was saved?"* The key insight — the demo is named at **start**, the ShadowPlay
  clip at **end (= the keypress)**, and the keypress falls **inside** the demo's recording interval.
  Containment is robust to the start≠end asymmetry, and handles one demo covering several clips.

**Example B — film editor: video take ↔ screenplay `.docx` by a shared scene code (different string
layouts):**
- **Extract:** video filename `Scene12_take3_final.mp4` → `scene = 12` (the number after "Scene").
  document filename `SCN-012 - Lakeside.docx` → `scene = 012` (the number after "SCN-").
- **Transform:** read both `scene` values **as a Number** (so `012` and `12` become equal —
  leading-zero normalization is just the "as Number" step, no code).
- **Join:** `video.scene == document.scene`. A second condition could AND a fuzzy title match for
  precision. Same engine, completely different domain, *zero* timestamps involved.

(Throughout this doc "clip" and "demo" are used for concreteness, but every mechanism is
asset↔file-general: read "clip" as *any asset*, "demo/companion" as *any file*.)

---

## 2. Anatomy of a Linker

```
Linker
├─ identity      name, description, color/icon, enabled, priority(sort_order), schema_version
├─ Source scope  which ASSETS this linker considers      (reuse AssetFilter + extras)
├─ Target scope  where COMPANION FILES live + which qualify
├─ Fields        named typed extractors, defined per side (clip fields / file fields)
├─ Computed      named expressions over Fields            (the Transform layer)
├─ Match         predicates over clip-values vs file-values, combined AND/OR  (the Join)
├─ Cardinality   best-only | all | exclusive; tiebreak; ambiguity policy
├─ Output        how the attachment row is labeled; what matched values to snapshot
└─ Actions       reveal (always) + 0..N "Open with" actions, by extension
```

---

## 3. Extraction layer — Fields

A **Field** is `(name, type, source, extraction, normalization?)`. Fields are defined separately for
the clip side and the file side (they can share names; the predicate references `clip.X` / `file.Y`).

### 3.1 Sources (where the raw string/value comes from)
- **filename** — the file's base name (with or without extension).
- **path** — the full path, or a chosen parent-folder segment (e.g. the map name is the folder).
- **metadata** — any field the app already stores: `duration`, `width`, `height`, `fps`,
  `recorded_at`, `size_bytes`, codec, plus any `metadata_json` JSON-path. (On the **file** side,
  rich media metadata only exists for files ffprobe understands; for a `.dem`/`.txt` the usable
  "metadata" is file attributes — see next.)
- **file attribute** — `mtime` (modified), `created` (Windows birthtime), `size`, and a weak
  **`folder_index`** (position when the folder is sorted by name/mtime — fragile, loud warning).
- **constant** — a fixed literal (offsets, unit factors, a default).

### 3.2 Extraction methods (how to get a value out of the source string)
- **whole value** — use the source as-is (cast to type).
- **template capture** — the `%name%` template (below). *This is the headline feature and works on
  any string source, not just filenames* — you can run a template over a weird `recorded_at` string
  the same way you run it over a filename.
- **regex** — a raw regex with named groups (escape hatch for power users).
- **split** — split on a delimiter, take the Nth part.

### 3.3 The filename template syntax
The user types a template that mirrors the filename, with `%name%` where a variable lives and
literal text everywhere else. Example from the user:

```
%number%-%number2%_%number3%.somehardcodedstuffhere.%number4%*%text%*%number5%.%text2%
```

Decoded:
| Token | Meaning |
|-------|---------|
| `%number%` `%number2%` … | named capture variables; **type is set per-variable in the UI** (the name is just a name) |
| `-` `_` `.somehardcodedstuffhere.` `.` | **literals** (regex-escaped, so `.` means a literal dot) |
| `*` | uncaptured wildcard (`.*?`, lazy) |

Semantics (well-defined so the user is never surprised):
- A token captures **up to the next literal/anchor**. Its regex is **type-constrained**: a numeric
  variable is `\d+` (or `\d+[.,]\d+` for float), a text variable is `.+?` (lazy), a date variable
  uses the chosen date format's token regex.
- Two adjacent tokens with no literal between them (`%a%%b%`) are ambiguous → the builder flags it
  and asks for a separator or a fixed width.
- A **repeated name** = backreference: it must match the *same* text both places (e.g. the map name
  appearing twice). Different value → no match.
- Matching is anchored to the whole base name by default; "match anywhere" is a toggle.
- Case sensitivity is a per-field normalization toggle (Windows names are case-insensitive; map
  strings often differ only in case).

### 3.4 Types & normalization
- Types: `int`, `float`, `string`, `datetime`, `duration`, `bool`.
- `datetime` fields declare a **parse format** (`%Y.%m.%d - %H.%M.%S` for ShadowPlay, etc.) and a
  **timezone assumption** (default = machine local; option = UTC) — because internally **every time
  value is converted to epoch seconds** before any numeric/interval predicate runs. That single
  normalization kills the whole class of timezone/DST bugs: deltas are computed in epoch space, not
  on wall-clock strings.
- Normalization options per field: trim, lower/upper, strip-leading-zeros, decimal-separator,
  collapse-whitespace, unicode-fold.
- A cast/parse failure makes the field **null** (not a crash) and is surfaced in the test panel as a
  per-field error — never silently dropped.

---

## 4. Transform layer — Computed fields

The reason the engine can align sources that disagree. **Internally** a computed field is
`name = expression`; **the user never types one** — they build it as a stack of visual, by-example
"steps" (§9.3), or by demonstrating the desired answer (Flash-Fill). The mini-language below is the
*internal representation and the optional expert escape hatch*, not the way a non-programmer reaches
these transforms. Every example here is reachable by stacking labeled steps with live preview:

- **two counters offset by one:** `key = clip.seq + 1` then join `key == file.seq`.
- **start from end:** `clipStart = clip.recorded_at - clip.duration` (datetime − duration).
- **normalize a key:** `mapKey = lower(trim(file.map))`.
- **unit fix:** `ms = clip.seconds * 1000`.
- **composite key:** `k = file.number1 + "-" + file.number2`.

### 4.1 The mini-language (sandboxed; no Python `eval`)
A tiny typed AST interpreter over a **whitelist** — never arbitrary code:
- literals (`12`, `3.5`, `"koth"`, `true`, `null`), field refs (`clip.x`, `file.y`)
- arithmetic `+ - * / // %`, unary `-`; comparison `== != < <= > >=`; logic `and or not`
- string: `lower upper trim len substr replace startswith endswith contains regex(s,pat,grp) pad`
- numeric: `int float abs round min max`
- datetime/duration: `parse_date(s,fmt)`, `epoch(dt)`, `format(dt,fmt)`, `truncate(dt,unit)`,
  `dt ± seconds`, `parse_duration`, `seconds(d)`
- string-fuzzy: `similarity(a,b)` → 0..1 (Levenshtein ratio)
- null-safety: any op touching null → null; `coalesce(a,b,…)`
- **deterministic only** — no `now()`/random inside a *match* expression (would make linking
  unstable across runs); allowed only in display labels.

The visual step-builder (§9.3) generates these from dropdown steps and from "show me the answer"
demonstrations, so a non-programmer reaches the full set without syntax; the raw box (field
autocomplete + live evaluation on the sample) is an *optional* expert convenience that gates no
capability the steps don't already provide.

---

## 5. Join layer — Match

One or more **predicates** combined with AND/OR (and groups). Each predicate is structured (the UI
builds it) with an expression escape hatch for the exotic. Predicate types:

| Predicate | Form | Use |
|-----------|------|-----|
| **equality** | `clip.X == file.Y` (post-normalize) | map name, sequence, exact key |
| **tolerance** | `\|clip.X − file.Y\| ≤ tol` (+ optional directional `offset`) | timestamps named ~together |
| **interval-containment** | `file.start ≤ clip.T ≤ file.end` (± slack) | **the demo case**; many-clips:1-demo |
| **interval-overlap** | `[clipS,clipE] ∩ [fileS,fileE] ≠ ∅` | both sides are intervals |
| **range** | `clip.X < file.Y`, `≥`, between | "demo started before the clip ended" |
| **string** | equals / contains / startswith / regex / **fuzzy ≥ θ** | near-equal map names |

A **match score** (0..1) is computed from the predicates (closeness within tolerance, fuzzy ratio;
equality = 1.0/binary; intervals = centred-ness). Conditions can be **weighted** ("time matters more
than name"); AND combines (weighted), OR takes the max. The score is what the **Resolve** layer (§6)
ranks candidates by, and what the "why" explanation quotes. The demo preset = a single
interval-containment predicate; a high-precision rule = `tolerance(time,10s) AND fuzzy(map)≥0.8`.
(Pure-equality rules produce ties — no gradient — so Resolution leans on the tiebreak keys, and the
UI says so honestly: *"these are tied; choosing by nearest time"*.)

---

## 6. Resolution — from candidates to winners (the global, *relative* layer)

The Join (§5) does **not** decide links; it proposes **candidates** — every plausible (clip, file)
pair, each with a score and its "why". Resolution is the equally-important second half: given the
**whole bipartite graph** of candidates, decide which edges become links, judging them **relative to
one another** under the user's cardinality rules. This is the layer the user explicitly asked for:
*"match relative to the other matches"*, *"if the rule yields 10, link these not those"*.

### 6.1 Why matching is global, not per-pair
"Link every pair that passes" or "keep each clip's best" breaks the instant candidates compete:
- two clips both want demo D → give D to **both** (reuse) or to the **closer** one and send the other
  to its next-best (exclusive)?
- a loose rule yields 40 candidates but only ~10 are real → keep the best **10 overall**?
- a clip's candidates score `[0.95, 0.93, 0.4, …]` → keep those **near its best**, drop the tail?

None is answerable from one pair in isolation. Resolution therefore works on the candidate graph as a
whole (per linker), broken into **connected components** (clips/files that share candidates).
Components are naturally small — matches cluster in time/key — which is also what keeps it fast (§10).

### 6.2 The relationship is M:N, shaped by two caps
Everything is a special case of "how many on each side":

| per clip | per file | relationship | typical case |
|---|---|---|---|
| ≤1 | any | **N : 1** | many clips ← one demo / one session config |
| any | ≤1 | **1 : N** | one clip ← its several artifacts (script + storyboard + notes) |
| ≤1 | ≤1 | **1 : 1** | each take ↔ its own script (an assignment problem) |
| any | any | **M : N** | keep everything that qualifies |
| **min ≥ 1** | – | **mandatory** | every take *must* have a script → unmatched = **error**, not silence |

Zero is first-class on both axes: a clip may legitimately get **no** file (unmatched bucket), a file
may match **nothing** (unused bucket) — never fabricated.

### 6.3 Resolution strategies (how winners are picked when caps bind)
Plain-named in the UI; these are the engine modes:
- **Keep all that qualify** — every candidate over the thresholds links (M:N, no competition).
- **Best per clip / per file (top-K)** — greedy: each node keeps its K highest, respecting its cap;
  the other side may be reused.
- **Best overall pairing** — when *both* sides are capped (1:1 / limited), the **global optimum**:
  the set of edges that **maximises total score with no node over its cap** (assignment /
  min-cost-max-flow). Order-independent, no double-booking, globally best — the strongest "relative".
- **Limited total (quota)** — keep only the best **N** matches the rule produces (global ranked cut):
  the user's literal "10 matches" case.
- **Relative threshold** — per clip, keep candidates **within Δ of that clip's own best**, plus an
  **absolute floor** so a weak best doesn't drag in noise.
- **One per group** — at most one match per **[day / map / folder / any field]**.

**Contested files (losers' fate):** when a capped file is wanted by several clips → winner by
tiebreak; losers **drop**, **cascade to next-best** (re-run selection on the residual graph), or
**flag**. **Tiebreaks** (equal scores): nearest-time / newest / smallest / name — user-ordered.

### 6.4 Ambiguity → the human arbitrates the handful that's unclear
When the top candidates sit within an "indistinguishable" margin, default = **flag, don't guess** —
the pair lands in a **"needs you"** bucket. One click resolves it, and that becomes a **pin** (a hard
constraint the resolver honours forever, §7). This is the escape valve that makes *any* amount of
resolution complexity tractable for a non-programmer: **the auto-resolver does its best; you
hand-arbitrate only the few it flags.** No algorithm to understand — ever.

### 6.5 Controlling it — simple (yet real) ↔ full, both friendly
Same philosophy as §9: plain language, tuned by **watching the preview**, never by configuring an
algorithm.

**Simple — three always-visible questions (already meaningful control):**
1. *How many files can one video have?* → None-or-one / Exactly one / **Any** (default)
2. *Can one file be shared by several videos?* → **Yes** / No
3. *When files compete for the same video?* → **Closest** / Best score / Keep all / Ask me

Those three alone pick between keep-all, best-only, exclusive-1:1, and flag — considerable control
with zero advanced concepts.

**Full — an "Advanced resolution" expander (more of the *same* vocabulary, not code):**
- per-clip `min..max` + top-K; per-file `min..max` + exclusive toggle
- strategy: keep-all / best-per-side / **best overall pairing** / **limited total N**
- relative threshold Δ + absolute floor
- contested losers: drop / cascade / flag
- one-per-**[group]**
- tiebreak order (drag to rank)
- unmatched policy: ignore / flag / **error (mandatory)**
- re-run stability: **keep existing links stable** vs **always re-optimise** (pins always sticky)

**The control loop is the preview, not the settings.** As any of these change, the live buckets
update — **matched / unmatched / needs-you / unused** — and a dedicated **Contested view** narrates
each competition in plain words:
> *"Demo D is wanted by clips #42, #51, #66. Policy = Closest → D goes to #42; #51 cascades to its
> next-best demo; #66 has none left → unmatched."*

The user tunes resolution by **watching contested cases resolve the way they want** and pinning the
rest — full power, no algorithm-talk.

---

## 7. Output & manual overrides

The result of a run is a set of **attachments** (`asset_attachments` rows): `asset_id, linker_id,
path, label, ext, kind, score, matched_values_json, status, origin, pinned, size, mtime, …`.

- **Re-run = diff, not rebuild:** add new matches, drop matches that no longer hold, update changed
  ones — but **never touch manual overrides**.
- **Manual override = a tombstone that survives re-runs.** The user can, on a clip, manually
  *pin* a file (force-link, even if the rule wouldn't) or *exclude* one (force-unlink a wrong auto
  match). Both are stored as `attachment_overrides(asset_id, path, linker_id, decision)` and the
  auto-matcher honors them forever (until the user clears them). Without this, every re-run would
  re-introduce a link the user just deleted — the single most important correctness rule of the
  whole feature.
- **Disable vs delete a linker:** *disable* stops matching and hides/greys its attachments but keeps
  the rows (re-enable restores instantly); *delete* removes its auto attachments (manual pins under
  it can be kept or cascaded — user prompt).
- **Missing files:** a linked file that vanishes → `status = missing` (greyed, with a "locate"
  affordance), reconciled on the next run; never auto-deletes the row (the drive might just be
  unmounted).

---

## 8. Actions — reveal & "Open with"

- **Reveal in folder** — always present. Windows `explorer /select,<path>`; opens the folder with
  the file selected.
- **Open (default)** — `os.startfile(path)` (the OS file association). Present unless disabled.
- **Named "Open with" actions** — 0..N per linker, keyed by **extension**: `{name, ext-glob,
  program, args_template}`, e.g. *"Open demo in HLAE"* → `C:\HLAE\hlae.exe -demo "%PATH%"`, or
  *"Open log in Notepad"* → `notepad "%PATH%"`. The user can attach any program to any extension —
  not our concern what they open with.

**Safety (this runs programs, so it matters):**
- The **program** comes from *linker config* (trusted — the user typed it); the **path** is *data*.
  We pass an **argument array**, never a shell string — so spaces / quotes / `%` / weird Unicode in
  filenames can't break out or inject. No `shell=True`. `%PATH%` is substituted as a single argv
  element.
- We never derive the command from the file's *name or contents* (injection). Program-not-found /
  nonzero-exit → toast, no crash.

---

## 9. UX — full power for non-programmers, with no walls

This is the section that makes or breaks the feature. The requirement: a non-technical user can reach
**every** capability of the extract→transform→join engine, and **never** meets a "you'd have to write
a formula/regex for that" dead end. We meet it with one governing rule:

> **Power is reached by stacking small, visual, by-example steps — never by switching into a
> code/formula modality.** A raw box exists for the rare expert, but it can do *nothing the visual
> steps can't*. Capability is never gated behind syntax.

### 9.1 Seven principles (the "why" behind every control)
1. **Program by demonstration, not by syntax.** The user works on *their real files*: paste a real
   name, click the parts, or type the *answer they want* — the system infers the rule. They never
   author an abstract pattern from a blank box.
2. **Every step shows its live result on the sample, inline.** Like a spreadsheet: control and result
   sit together; you are never editing blind. This is what lets non-programmers build confidence.
3. **Variables are visual objects you pick from a menu** (showing each one's current sample value),
   never typed references like `clip.recorded_at`. Inserting a variable = clicking a chip.
4. **Conditions read as plain sentences** with inline dropdowns. No boolean syntax.
5. **Jargon is invisible.** "regex", "epoch", "predicate", "timezone", "interval", "int/float" never
   appear in the default UI. Types are *Number / Text / Date & time / Length-of-time*; the engine
   silently handles tz/DST/epoch.
6. **No dead ends, no modality switch to reach power.** The "advanced" path is *more of the same
   visual vocabulary* (more steps, more condition rows), not a code editor. The optional raw box is a
   convenience for experts and for precise sharing — equivalent, not required.
7. **Failures teach, never dump.** No stack traces. "Couldn't find a number after the dash in this
   file's name" with the offending sample highlighted and a fix suggested.

### 9.2 The builder reads top-to-bottom like a story
The editor opens as a single vertical flow, plain language, with a **live preview docked alongside
the whole time**:

> **1. Which items do we attach things to?** → *Videos in `D:\Footage`* (folder/scope picker)
> **2. What files do we attach?** → *`.docx` files in `D:\Scripts`* (folder + file-kind picker)
> **3. How do we read each side?** → the *by-example fields* area (§9.3)
> **4. When do they go together?** → the *sentence-built conditions* (§9.4)
> **5. If several files fit one item?** → *Pick the closest ▾* · **One file for many items?** *Yes ▾*
> **6. Buttons on each linked file** → *Reveal* (always) · *+ Open with…*

Steps 3–4 are where all the power lives, and both are by-example/sentence — expandable, never code.

### 9.3 Reading values out of files — *by example* (the Extract + Transform layers, fused for the user)
The user doesn't see "fields" and "expressions" as separate concepts. They see: *"here's a value I
care about, and here's how to get it."* Two affordances, both visual:

**(a) Highlight-to-capture.** Paste/auto-pull a real filename; the builder **auto-tokenizes** it
(detects numbers, dates, common separators) and color-codes the guesses. The user confirms, renames a
part to human words ("Scene number"), drags a split point, or clicks a span and says "this is the
value." Each captured part gets a **plain type** (auto-detected, changeable: Number / Text / Date &
time / Length-of-time) with the consequence shown live ("as a Number, `012` = `12`").

**(b) Transform = a stack of visual "steps", each with input→output shown on the sample.** This is
what *replaces the formula box and removes the programmer ceiling.* The user adds steps from a
labeled palette; "seq + 1" is **pick the number → "Add" → 1**, not code:
- **Text:** Split by […] take part […] · Characters from–to · Replace […]→[…] · Trim · Upper/lower ·
  Keep only (letters / digits) · Find by example
- **Number:** Treat as number · Add / Subtract / Multiply / Divide (by a value *or another picked
  variable*) · Round · Absolute
- **Date & time:** Read as date (pick a format, or **Detect**) · Add / Subtract […] (sec/min/hr/day) ·
  Round to nearest […] · Take just the date / just the time
- **Combine:** Join [var] + [text] + [var] in order (drag to reorder)
- **If…then:** the rare branch, still built from dropdowns

**(c) The escape hatch that gates nothing — "Show me the answer" (Flash-Fill).** For a value too
fiddly to click out, the user types, for *one* sample, **what the result should be**; the system
proposes a step-stack that produces it, which they can inspect and tweak. Demonstrate the answer →
get the rule. (A literal raw regex/formula box is also there for experts, clearly marked optional.)

This fused, stepped, by-example approach is the entire answer to *"non-programmers must have full
capability with no walls"*: the user's own worked examples — `seq + 1`, `lower(trim(map))`, leading
zeros via "as Number", `recorded_at − duration` — are all **reached by stacking labeled steps and
watching the sample update**, with zero syntax.

### 9.4 When do two files go together — the sentence-built match (the Join layer)
Each condition is a row that reads as a sentence, every bracket a dropdown:

> *the video's* **[recording time ▾]** *is* **[within ▾]** **[10] [seconds ▾]** *of the document's*
> **[modified date ▾]**

> *the video's* **[Scene number ▾]** **[equals ▾]** *the document's* **[Scene number ▾]**

"**+ Add another condition**" with a friendly **Match when [ALL ▾] / [ANY] are true**. The match
modes (within-N, contains/interval, equals, fuzzy "almost the same text") are named in plain words;
the underlying predicate types of §5 never surface as jargon.

### 9.5 The preview is the primary UI (and the teacher)
Always docked while editing; for a join engine you build the rule *by watching it react*:
- **Sample read-out:** for a real item *and* a real file, every captured value after its steps, in a
  table, errors highlighted in plain language.
- **Whole-library dry run** (non-destructive — nothing is written): counts + examples for the
  buckets — **matched** (the file it picked + a one-sentence why), **unmatched items**, **needs you**
  (≥2 close candidates — ambiguous), **unused files** (in the target folder, matched nothing), and a
  **Contested view** (§6.5) that narrates each competition and how the current resolution policy
  settles it. Every row has **"why?" / "why not?"** pointing at the exact step or condition in plain
  words ("matched because the document was modified 4 s after the video — inside your 10-second
  window"; "not linked because demo D went to the closer clip #42").
- Edits re-run the preview instantly. The user *sees* the rule's behaviour — candidates **and** how
  they resolve against each other — before enabling it.

### 9.6 Presets, the Linkers page, the toggle
- **Preset gallery (L0):** varied, domain-spanning starter recipes — *TF2 Demo (ShadowPlay)*, *TF2
  Demo (OBS)*, *Screenplay/script sidecar (by scene code)*, *Transcript by name*, *RAW + edit by
  name*, *Second-source recording by time*. Pick one → it prefills every step; you set the one thing
  that's yours (your folder) → Save → Enable. Most users stop here; the rest tweak the pre-built
  steps (and thereby learn the vocabulary painlessly).
- **Linkers page:** list / create / edit / **clone** / import-export (shareable JSON recipes) /
  reorder-priority / run-now / per-linker stats (matched / unmatched / ambiguous).
- **Enable toggle on the main menu / sidebar** so linkers flip on/off without leaving the library.

---

## 10. Execution & performance

- **Background job** (`LinkerRunner`) in the existing `ThreadJobQueue`, same family as the scan
  enricher / reconciler. **Triggers:** on enable, on edit (re-diff), "Run now", after a library scan,
  and a cheap re-sync on window focus.
- **Incremental:** cache each target directory's listing keyed by `(path, size, mtime)`; only
  re-extract changed files; only re-match clips whose inputs changed or that a changed file could now
  hit. Idempotent; honors override tombstones.
- **Join (candidate-generation) strategy chosen from the predicate** (don't brute-force n×m):
  - equality / key → **hash join** (bucket files by key).
  - tolerance / interval → **sort-and-sweep** on epoch time (O(n log n)); a moving window of
    candidate files.
  - arbitrary expression → nested loop **with caps + a loud "this rule can't be optimized" note**.
- **Resolution (§6) is component-local.** Build the candidate graph, partition into connected
  components with **union-find** (clips/files sharing candidates). Because matches cluster in
  time/key, components are tiny → solve each independently and the global optimum stays cheap:
  - keep-all / greedy top-K → sort within the component.
  - best-overall (1:1 / capped) → **min-cost-max-flow / Hungarian** per component; small ⇒ fast.
  - a pathologically large component (loose rule) → greedy fallback + the visible "couldn't globally
    optimise this cluster" notice. Pins enter as **fixed edges**; excludes drop edges; then re-solve.
  - **Determinism:** canonical sort on tiebreak keys so re-runs and other machines reproduce links.
  - **Stable mode:** prior-accepted + pinned edges carry a stickiness bonus so a single new file
    fills unclaimed slots instead of reshuffling existing links; *re-optimise* mode shows a diff first.
- **Defer-until-enriched:** a predicate needing `duration`/`width` can't run while the clip is still
  `metadata_pending`. The runner skips those clips and **re-runs after the metadata enrichment pass**
  completes — the same self-healing handoff the enricher already does. (Surfaced as "N clips waiting
  on metadata".)
- **Unavailable target** (folder missing / drive unmounted / permission denied) → linker reports
  "source unavailable", leaves existing attachments intact, retries next trigger. Never wipes on a
  transient read failure.

---

## 11. Data model & config

**New tables (migration v11):**
```
linkers(
  id, name UNIQUE, description, color, enabled, sort_order,
  schema_version, definition_json,            -- the whole rule (scopes/fields/exprs/predicates/actions)
  created_at, updated_at )

asset_attachments(
  id, asset_id ->assets(CASCADE), linker_id ->linkers(SET NULL), path,
  label, ext, kind, score, matched_values_json,
  status,                -- linked | missing | ambiguous
  origin,                -- auto | manual
  pinned,                -- manual force-link
  size, mtime, created_at, last_verified_at,
  UNIQUE(asset_id, path) )                     -- dedupe across linkers

attachment_overrides(
  asset_id, path, linker_id, decision,         -- pin | exclude
  created_at, PRIMARY KEY(asset_id, path, linker_id) )

-- target-dir listing cache (mtime/size keyed) for incremental runs
linker_file_cache(linker_id, path, size, mtime, fields_json, PRIMARY KEY(linker_id, path))
```
Only what we query is relational; the rule itself is a **versioned JSON blob** (`definition_json` +
`schema_version`) so the rule language can evolve without a migration per tweak, and linkers
export/import as JSON (shareable; ties into the planned export feature).

**Config:** linkers live in the DB (user data), not `default.toml`. Global knobs (default slack,
ambiguity margin, fan-out cap, focus-resync on/off, the reveal command per-OS) go in `[linkers]` in
config; presets ship as built-in JSON templates.

**Ports/architecture fit:** a `Linker`/`Attachment` entity + repo, a `LinkerService` (CRUD + run +
preview), a `LinkerRunner` job, an `ExtractTransformJoin` engine module (pure, unit-testable in
isolation), API routes under `/api/linkers` + `/api/assets/{id}/attachments` + `/api/linkers/{id}/preview`
+ `/api/attachments/{id}/open`, a detail-view "Linked files" panel, a Linkers page, and a sidebar
enable toggle. The engine being pure (string/metadata in → match decisions out) means the entire
extract/transform/join matrix is testable without touching the filesystem.

---

## 12. Edge-case catalog

### Parsing
- Template matches nothing → clip/file excluded **and shown** in the preview as a parse failure (not
  silent).
- Wildcard/token greediness → defined lazy semantics; adjacent-token ambiguity flagged at build time.
- Type cast fails ("12a"→int) → field null + per-field error.
- Literals with regex-special chars (`.`, `*`, `(`) → escaped.
- Case-insensitive filenames; map "Lakeside" vs "lakeside" → normalization toggle.
- Unicode / spaces / emoji / RTL in names.
- Repeated token name → backreference (must be equal).
- Locale decimal `,` vs `.`; leading zeros (`07` vs `7`) → normalization.
- Huge / overflowing numbers.

### Time & timezone
- Mixed sources (naive-local filename, UTC ffprobe tag, local mtime) → **all → epoch seconds** before
  compare; deltas are DST-safe.
- Per-datetime-field tz assumption (local default / UTC option).
- DST fold (a 01:30 that happens twice) → epoch math + tolerance slack.
- Clock skew between GPU-capture and game processes → tolerance covers seconds.
- File copied/moved resets mtime/birthtime → filename-time more stable; surfaced as a source
  trade-off in the builder.
- Bogus far-past/future times (bad clock, 1904/1970 sentinels) → sanity-bounded (year 2000–2100),
  rejected like the existing recorded_at normaliser.
- Two clips in the same second → identical T; many:1 fine; 1:1 → ambiguous (tiebreak/ flag).

### Matching & cardinality
- No match → leave unlinked, list under "unmatched".
- Several linkers attach different files to one clip → coexist, namespaced by linker.
- Two linkers attach the **same** file to one clip → dedupe by `(asset, path)`.
- A file matches clips from two linkers → fine.
- Target folder overlaps the clip's own folder → exclude self / same-asset / by-extension.
- Target is itself a managed asset (clip↔clip) → allowed but overlaps `references`; default presets
  steer to non-asset files.
- Loose rule → fan-out cap + "too loose" flag.
- n×m blow-up → strategy selection (hash/sweep) + caps for un-optimizable expressions.

### Resolution (relative / global)
- Clip with **0** candidates → unmatched (policy: ignore / flag / **error if `min ≥ 1`**).
- File with **0** candidates → unused bucket (surfaced, not an error).
- Same pair proposed via two predicates/paths → **dedupe the edge** before resolving.
- Two on-disk copies of the same file → distinct path edges; optional content-hash to fuse them.
- Score ties at a quota/cap boundary (#10 vs #11 tie) → deterministic tiebreak, or soft-cap
  keep-both, or flag — user-chosen; default deterministic.
- Multiple equally-optimal global assignments → **canonical deterministic pick** (stable sort on
  tiebreak keys) so re-runs/other machines reproduce the same links.
- **Pin violates a cap** → the pin wins; the optimiser treats it as a **fixed edge** and solves
  around it. **Exclude** removes a wanted edge → re-optimise without it.
- Giant/loose candidate graph → global optimum infeasible → component-partition; if a single
  component is still huge → fall back to greedy + a visible *"couldn't globally optimise this
  cluster, used best-per-clip"* notice (no silent downgrade).
- **Incremental reshuffle** — adding one file could change a global assignment. *Stable* mode keeps
  existing links sticky (new file fills only unclaimed slots); *re-optimise* mode may reshuffle but
  **shows the diff before applying**. Pins/manual always sticky.
- Metadata-pending clips → resolve their component **provisionally**, re-resolve after enrichment.
- Relative-threshold with a weak best (best = 0.3) → the absolute floor stops noise sneaking in.
- Cross-linker: same file→clip from two linkers → **independent per-linker resolution**; dedupe only
  at the attachment row `(asset, path)`.
- Quota vs per-node caps — defined order: per-node caps first, then the global quota over survivors
  (configurable).
- "One per group" with an in-group tie → tiebreak, surfaced.
- **Mandatory** (`min ≥ 1`) unsatisfiable for some clips → those clips listed as **errors**, never
  silently dropped.
- All-ties rule (pure equality, no gradient) → resolution is *entirely* tiebreak-driven; the UI says
  so and lets the user pick the tiebreak that encodes their real preference.

### Filesystem
- Target dir missing / drive unmounted / permission denied → "source unavailable", keep links,
  retry.
- Network / slow drive → background + cache + timeout.
- Linked file deleted → `missing`; renamed → re-match re-links (or optional content-hash identity to
  survive renames).
- Symlinks / junctions → follow option; loop-safe (walker already guards).
- > 260-char paths → extended-length path handling.
- Case-only rename; OneDrive/placeholder files that hydrate on stat (note the stall).

### Metadata
- Clip still `metadata_pending` → predicate needing duration/res defers → re-run post-enrichment.
- Non-video targets (`.dem`, `.txt`) have no ffprobe metadata → their "metadata" is file attrs +
  filename; be honest in the UI about what's available per file type. (ffprobe *is* offered for
  targets it understands.)
- Null metadata field on some clips → null-safe predicate → no match, surfaced.

### Config / UX
- Invalid expression → live validation, no runner crash.
- Disable keeps rows; delete removes auto rows (pins prompt).
- Edit after links exist → diff add/remove/update, overrides untouched.
- Clone-as-template; import/export JSON; `schema_version` for forward-compat.

### Open-with
- Program path with spaces → arg array.
- Filename with spaces/quotes/`%` → safe single argv element.
- Program missing / nonzero exit → toast.
- Never build the command from file name/content (injection); program = trusted config, path = data;
  no `shell=True`.
- Multiple actions per extension (open in A / B); reveal cross-OS (`explorer /select,` ·
  `open -R` · `xdg-open`).

### Absurd-but-supported
- `clipDuration == fileDuration` (link two equal-length videos by metadata).
- match on a hash embedded in the name.
- one target folder, mixed extensions, different open-with per ext.
- weak `folder_index` order matching for files with no timestamp and no useful name (loud "fragile"
  warning).
- demo seq = clip seq **+1** (different counters) → expression offset — the user's literal example.
- `tolerance(time,10s) AND fuzzy(map)≥0.8` — belt-and-suspenders precision.

---

## 13. Phased delivery

1. **Engine + schema (headless, fully tested).** `asset_attachments` / `linkers` / `overrides`
   migration; the pure, domain-neutral **extract→transform→join→resolve** engine (every
   step/transform/predicate **and** every resolution strategy — keep-all / greedy / best-overall /
   quota / relative-threshold / cascade, component-partitioned — unit-tested without the filesystem);
   `LinkerRunner` job; `LinkerService`; API. Ship with **two** starter presets wired end-to-end to
   prove generality — *interval-by-time, N:1* (TF2 demo) **and** *equal-key-by-name, 1:1* (screenplay
   /sidecar, exercising best-overall assignment) — both as `definition_json`, not special-cased code.
   A linked file shows in the detail view with Reveal/Open.
2. **Linkers page + the top-to-bottom builder + enable toggle.** Preset gallery, the story-flow
   builder (scope → files → read each side → when they match → **cardinality/resolution** → actions),
   the **three simple resolution questions** + Advanced-resolution expander, and the docked
   test/preview harness (sample read-out + dry-run buckets + **Contested view** + plain-language
   why/why-not).
3. **By-example mastery:** highlight-to-capture auto-tokenizer, the **visual transform-step palette**
   (text/number/date/combine/if), and **"show me the answer" (Flash-Fill)** inference — the no-code
   path to the full transform set. (Raw regex/formula box ships here too, marked optional/expert.)
4. **Open-with actions** (per-extension programs), **import/export** recipes, **clone**, manual
   pin/exclude UI, missing-file locate.
5. **Scale & polish:** hash/sweep strategy selection, incremental cache, fan-out caps, focus-resync,
   defer-until-enriched wiring, perf pass on a synthetic 10k-item / 50k-file library.

Each phase is independently shippable and leaves the app green (ruff / mypy --strict / pytest /
svelte-check).

---

## 14. Recommended defaults (open decisions, pre-decided so building isn't blocked)

- Default match mode for the *time-interval* preset: **interval-containment** (created→start,
  mtime→end), slack **10 s**. For the *by-name* preset: **equal key**, read "as Number" so leading
  zeros normalize.
- Ambiguity policy default: **flag** (don't auto-pick when two candidates are within the margin) →
  the "needs you" bucket; one-click arbitration becomes a sticky pin.
- Resolution defaults: per-clip **Any**, per-file **shared = Yes**, competition **Closest** →
  effectively *keep-all where uncomplicated, nearest-wins where contested*. The *by-name 1:1* preset
  flips to per-clip **Exactly one** + per-file **No** + **best-overall pairing**. Re-run = **stable
  mode** (no surprise reshuffles); contested losers **cascade** to next-best.
- Determinism: component-partitioned resolution with a **canonical tiebreak sort**, reproducible
  across runs and machines.
- Identity of a linked file: **path + (size, mtime)**; optional content-hash opt-in for
  rename-survival (off by default — companion files can be large).
- Time comparisons: **always epoch seconds**; datetime fields default to **machine-local** tz.
- "Open with": **off by default** (only Reveal + OS-default Open); the user adds programs explicitly.
- Linker definitions: **DB-stored JSON blob**, `schema_version = 1`, export/import as JSON.
- Add a ShadowPlay filename pattern (`%Y.%m.%d - %H.%M.%S`) to the video media type's
  `recorded_at` patterns so the clip side reads a reliable time from the name, not just mtime.
