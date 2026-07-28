/**
 * Sitemap generado en cada petición desde el catálogo real.
 *
 * No se usa `@astrojs/sitemap` porque este sitio es SSR y sus URLs
 * interesantes son las de las instituciones, que salen de la base y cambian
 * sin que nadie reconstruya el sitio. Un sitemap horneado en el build quedaría
 * obsoleto en cuanto la fase 8 añadiera una institución.
 */

import type { APIRoute } from 'astro';
import { api } from '~/lib/api';
import { sitioPublico } from '~/lib/sitio';

export const prerender = false;

const ESTATICAS: [ruta: string, prioridad: string, frecuencia: string][] = [
  ['/', '1.0', 'daily'],
  ['/calculadora', '0.8', 'weekly'],
  ['/metodologia', '0.5', 'monthly'],
  ['/aviso-legal', '0.2', 'yearly'],
  ['/privacidad', '0.2', 'yearly'],
];

export const GET: APIRoute = async ({ url }) => {
  // No se usa el `site` de la configuración: es un valor de build, y el dominio
  // se conoce al desplegar. Ver [sitio.ts](~/lib/sitio).
  const base = sitioPublico(url);

  let instituciones: string[] = [];
  try {
    const mercado = await api.comparador();
    // Sólo las que tienen algo publicable: una ficha sin tasas no aporta al
    // índice y compite consigo misma en la búsqueda.
    instituciones = [...new Set(mercado.filas.map((f) => f.institucion.slug))].sort();
  } catch (error) {
    console.error('[sitemap]', error);
  }

  const entradas = [
    ...ESTATICAS.map(
      ([ruta, prioridad, frecuencia]) =>
        `<url><loc>${base}${ruta}</loc><changefreq>${frecuencia}</changefreq><priority>${prioridad}</priority></url>`,
    ),
    ...instituciones.map(
      (slug) =>
        `<url><loc>${base}/institucion/${slug}</loc><changefreq>weekly</changefreq><priority>0.7</priority></url>`,
    ),
  ];

  return new Response(
    `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${entradas.join('\n')}\n</urlset>`,
    {
      headers: {
        'Content-Type': 'application/xml; charset=utf-8',
        'Cache-Control': 'public, max-age=3600',
      },
    },
  );
};
