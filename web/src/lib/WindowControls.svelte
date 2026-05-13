<script lang="ts">
  // The minimize / maximize / close cluster for the frameless pywebview window. Lives at the right
  // end of the app's top bar (App.svelte renders it only when `window.pywebview` exists). The buttons
  // call the Python `_WindowApi` exposed by pywebview as `window.pywebview.api`.
  interface WindowApi { minimize(): void; toggle_maximize(): void; close(): void }
  const api = (): WindowApi | undefined =>
    (window as unknown as { pywebview?: { api?: WindowApi } }).pywebview?.api;
</script>

<div class="wc">
  <button type="button" class="wc-btn" title="Minimize" aria-label="Minimize" onclick={() => api()?.minimize()}>
    <svg viewBox="0 0 12 12" width="11" height="11" aria-hidden="true"><rect x="2" y="5.5" width="8" height="1.1" fill="currentColor" /></svg>
  </button>
  <button type="button" class="wc-btn" title="Maximize" aria-label="Maximize" onclick={() => api()?.toggle_maximize()}>
    <svg viewBox="0 0 12 12" width="11" height="11" aria-hidden="true"><rect x="2.5" y="2.5" width="7" height="7" fill="none" stroke="currentColor" stroke-width="1.1" /></svg>
  </button>
  <button type="button" class="wc-btn wc-close" title="Close" aria-label="Close" onclick={() => api()?.close()}>
    <svg viewBox="0 0 12 12" width="11" height="11" aria-hidden="true"><path d="M3 3 L9 9 M9 3 L3 9" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" /></svg>
  </button>
</div>

<style>
  /* sits flush at the right edge of the top bar -- the negative margin "eats" the bar's right padding */
  .wc { display: flex; align-self: stretch; margin-right: -14px; flex: none; }
  .wc-btn {
    width: 46px; align-self: stretch; display: flex; align-items: center; justify-content: center;
    background: transparent; border: none; color: var(--text-2, #9aa4b3);
    transition: background 0.12s ease, color 0.12s ease;
  }
  .wc-btn:hover { background: rgba(255, 255, 255, 0.08); color: var(--text, #e7eaf0); }
  .wc-btn:active { background: rgba(255, 255, 255, 0.14); }
  .wc-close:hover { background: #c2342f; color: #fff; }
  .wc-close:active { background: #a82a26; }
</style>
