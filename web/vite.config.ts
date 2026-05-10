import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';

// During development run the Python backend on a fixed port:
//   set CLIPPYCAP__SERVER__PORT=8765 && python -m clippycap run --no-browser
// then `npm run dev` here serves the UI on :5173 and proxies the API/media to :8765.
// For production: `npm run build` emits ./dist, which the backend serves at "/".
const BACKEND = process.env.CLIPPYCAP_DEV_BACKEND ?? 'http://127.0.0.1:8765';

export default defineConfig({
  plugins: [svelte()],
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/media': { target: BACKEND, changeOrigin: true },
      '/thumbnails': { target: BACKEND, changeOrigin: true },
      '/plugins': { target: BACKEND, changeOrigin: true },
    },
  },
});
