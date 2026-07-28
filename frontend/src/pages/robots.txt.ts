import type { APIRoute } from 'astro';
import { sitioPublico } from '~/lib/sitio';

export const prerender = false;

export const GET: APIRoute = ({ url }) => {
  const base = sitioPublico(url);

  // `/api/` es el BFF: son endpoints internos que sólo sirven a las islas de
  // este mismo sitio. Indexarlos no aporta nada y ensucia los resultados.
  const cuerpo = [
    'User-agent: *',
    'Allow: /',
    'Disallow: /api/',
    '',
    `Sitemap: ${base}/sitemap.xml`,
    '',
  ].join('\n');

  return new Response(cuerpo, {
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
  });
};
