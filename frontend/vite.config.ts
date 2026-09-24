import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// In development the API runs on :8000 (uvicorn); in production FastAPI serves the built UI itself.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true } },
  },
  build: { outDir: 'dist', chunkSizeWarningLimit: 1500 },
});
