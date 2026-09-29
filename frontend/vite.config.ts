import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// In development the API runs on :8000 (uvicorn); in production FastAPI serves the built UI itself.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true } },
  },
  build: { outDir: 'dist', chunkSizeWarningLimit: 1500 },
  // Unit tests cover the UI's logic (event reducer, analytics, formatting); the Playwright E2E covers the pages.
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
    coverage: {
      provider: 'v8',
      include: ['src/state.ts', 'src/format.ts'],
      reporter: ['text', 'json-summary'],
      thresholds: { lines: 90, statements: 90, functions: 90, branches: 80 },
    },
  },
});
