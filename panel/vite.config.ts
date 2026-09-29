import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// Geliştirmede /api istekleri lokal admin API'ye (8001) yönlendirilir.
// Production'da aynı yönlendirmeyi nginx yapar (panel/nginx.conf).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8001",
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  // assetsInlineLimit: 0 → fontlar data: URI olarak gömülmez; nginx CSP (font-src 'self') ile uyumlu
  build: { sourcemap: false, chunkSizeWarningLimit: 1200, assetsInlineLimit: 0 },
});
