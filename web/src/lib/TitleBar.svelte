<script lang="ts">
  // Custom frameless-window title bar -- only mounted when running inside the pywebview shell
  // (App.svelte checks `window.pywebview`). The buttons call the Python `_WindowApi` exposed as
  // `window.pywebview.api`; the `.pywebview-drag-region` element makes the left part drag the window.
  import logo from '../assets/clippycap-logo.png';

  interface WindowApi { minimize(): void; toggle_maximize(): void; close(): void }
  const api = (): WindowApi | undefined =>
    (window as unknown as { pywebview?: { api?: WindowApi } }).pywebview?.api;
</script>

<div class="tb">
  <div class="tb-drag pywebview-drag-region">
    <img src={logo} alt="" class="tb-logo" draggable="false" />
    <span class="tb-name">Clippycap</span>
  </div>
  <div class="tb-controls">
    <button type="button" class="tb-btn" title="Minimize" aria-label="Minimize" onclick={() => api()?.minimize()}>
      <svg viewBox="0 0 12 12" width="11" height="11" aria-hidden="true"><rect x="2" y="5.5" width="8" height="1.1" fill="currentColor" /></svg>
    </button>
    <button type="button" class="tb-btn" title="Maximize" aria-label="Maximize" onclick={() => api()?.toggle_maximize()}>
      <svg viewBox="0 0 12 12" width="11" height="11" aria-hidden="true"><rect x="2.5" y="2.5" width="7" height="7" fill="none" stroke="currentColor" stroke-width="1.1" /></svg>
    </button>
    <button type="button" class="tb-btn tb-close" title="Close" aria-label="Close" onclick={() => api()?.close()}>
      <svg viewBox="0 0 12 12" width="11" height="11" aria-hidden="true"><path d="M3 3 L9 9 M9 3 L3 9" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" /></svg>
    </button>
  </div>
</div>

<style>
  .tb {
    display: flex; align-items: stretch; height: 34px; flex-shrink: 0;
    background: #0e1014; border-bottom: 1px solid var(--border, #2a313d);
    user-select: none; -webkit-user-select: none;
  }
  .tb-drag {
    flex: 1; min-width: 0; display: flex; align-items: center; gap: 8px; padding-left: 11px;
  }
  .tb-logo { width: 17px; height: 17px; object-fit: contain; flex-shrink: 0; }
  .tb-name {
    font-size: 12px; font-weight: 600; letter-spacing: 0.02em; white-space: nowrap;
    color: var(--text-2, #9aa4b3);
  }
  .tb-controls { display: flex; flex-shrink: 0; }
  .tb-btn {
    width: 44px; height: 34px; display: flex; align-items: center; justify-content: center;
    background: transparent; border: none; color: var(--text-2, #9aa4b3);
    transition: background 0.12s ease, color 0.12s ease;
  }
  .tb-btn:hover { background: rgba(255, 255, 255, 0.08); color: var(--text, #e7eaf0); }
  .tb-btn:active { background: rgba(255, 255, 255, 0.14); }
  .tb-close:hover { background: #c2342f; color: #fff; }
  .tb-close:active { background: #a82a26; }
</style>
