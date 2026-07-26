// @ts-check
import node from '@astrojs/node';
import react from '@astrojs/react';
import { defineConfig } from 'astro/config';

// SSR con el adapter de node (decisión D6: SEO primero). El comparador tiene
// que llegar renderizado en el HTML — con JavaScript deshabilitado la tabla
// sigue completa — y sólo los filtros, la selección y el combinador hidratan
// como islas.
export default defineConfig({
  output: 'server',
  adapter: node({ mode: 'standalone' }),
  integrations: [react()],
  site: process.env.PUBLIC_SITE_URL ?? 'https://centinelafinanciero.cloud',
  server: { host: '127.0.0.1', port: 3010 },
  vite: {
    // La API interna y su llave viven en el servidor. Un `import.meta.env`
    // sin prefijo `PUBLIC_` nunca llega al bundle del navegador, que es la
    // garantía de la que depende todo el BFF.
    ssr: { noExternal: ['@nanostores/react'] },
  },
});
