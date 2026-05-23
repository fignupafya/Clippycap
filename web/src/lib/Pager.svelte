<script lang="ts">
  // Numbered pager for the library grid. `page` is 1-based and bindable; the parent reloads the
  // grid whenever it changes. The grid only ever holds one page of cards, so the DOM stays small
  // and fast no matter how large the library grows.
  let { page = $bindable(), pageCount, total, pageSize }: {
    page: number; pageCount: number; total: number; pageSize: number;
  } = $props();

  // Page numbers to show: always the first and last, the current ±2, and an ellipsis for any
  // wider gap (a gap of exactly one page shows that page instead of a pointless "…").
  let items = $derived.by((): (number | 'gap')[] => {
    const want = new Set<number>([1, pageCount]);
    for (let p = page - 2; p <= page + 2; p += 1) {
      if (p >= 1 && p <= pageCount) want.add(p);
    }
    const out: (number | 'gap')[] = [];
    let prev = 0;
    for (const p of [...want].sort((a, b) => a - b)) {
      if (p - prev === 2) out.push(prev + 1);
      else if (p - prev > 2) out.push('gap');
      out.push(p);
      prev = p;
    }
    return out;
  });

  let rangeFrom = $derived(total === 0 ? 0 : (page - 1) * pageSize + 1);
  let rangeTo = $derived(Math.min(page * pageSize, total));
  const nf = new Intl.NumberFormat();

  function go(target: number): void {
    const clamped = Math.min(pageCount, Math.max(1, Math.round(target) || 1));
    if (clamped !== page) page = clamped;
  }
  function onGoInput(e: Event): void {
    const el = e.currentTarget as HTMLInputElement;
    const n = Number(el.value);
    if (el.value !== '' && Number.isFinite(n)) go(n);
    el.value = '';
  }
</script>

<nav class="pager" aria-label="Library pages">
  <span class="pager-count">{nf.format(rangeFrom)}–{nf.format(rangeTo)} of {nf.format(total)}</span>

  <div class="pager-ctrls">
    <button class="pgb" disabled={page <= 1} onclick={() => go(1)} aria-label="First page" title="First page">«</button>
    <button class="pgb" disabled={page <= 1} onclick={() => go(page - 1)} aria-label="Previous page" title="Previous page">‹</button>
    {#each items as it, i (i)}
      {#if it === 'gap'}
        <span class="pager-gap" aria-hidden="true">…</span>
      {:else}
        <button class="pgb num" class:on={it === page}
                aria-current={it === page ? 'page' : undefined}
                onclick={() => go(it)}>{it}</button>
      {/if}
    {/each}
    <button class="pgb" disabled={page >= pageCount} onclick={() => go(page + 1)} aria-label="Next page" title="Next page">›</button>
    <button class="pgb" disabled={page >= pageCount} onclick={() => go(pageCount)} aria-label="Last page" title="Last page">»</button>
  </div>

  <label class="pager-go">Go to
    <input type="number" min="1" max={pageCount} placeholder={String(page)}
           onchange={onGoInput} onkeydown={(e) => { if (e.key === 'Enter') onGoInput(e); }} />
  </label>
</nav>

<style>
  /* a fixed footer of <main>: always flush at the bottom of the window, whatever the page height */
  .pager {
    flex: none;
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    padding: 9px 14px;
    background: var(--bg); border-top: 1px solid var(--border);
  }
  .pager-count { font-size: 12px; color: var(--text-2); white-space: nowrap; }
  .pager-ctrls { display: flex; align-items: center; gap: 3px; flex-wrap: wrap; margin: 0 auto; }
  .pgb {
    min-width: 30px; height: 30px; padding: 0 7px;
    display: inline-flex; align-items: center; justify-content: center;
    border: 1px solid var(--border); border-radius: var(--r-sm);
    background: var(--bg-2); color: var(--text); font-size: 13px; font-weight: 600;
  }
  .pgb:hover:not(:disabled) { background: var(--bg-3); border-color: #3a4252; }
  .pgb:disabled { opacity: .38; cursor: default; }
  .pgb.on {
    background: var(--accent); border-color: var(--accent); color: #ffffff; cursor: default;
  }
  .pager-gap { min-width: 22px; text-align: center; color: var(--text-3); user-select: none; }
  .pager-go { font-size: 12px; color: var(--text-2); white-space: nowrap; display: flex; align-items: center; gap: 6px; }
  .pager-go input {
    width: 58px; background: var(--bg-2); color: var(--text);
    border: 1px solid var(--border); border-radius: var(--r-sm); padding: 5px 7px; outline: none;
  }
  .pager-go input:focus { border-color: var(--accent); }
</style>
