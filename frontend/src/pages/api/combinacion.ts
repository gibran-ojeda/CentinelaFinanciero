/**
 * BFF de la calculadora de combinación.
 *
 * La isla del combinador llama aquí, no a la API interna. Este endpoint corre
 * en el servidor de Astro, añade la `X-API-Key` y reenvía — el navegador
 * nunca ve ni la llave ni el puerto 8010.
 */

import type { APIRoute } from 'astro';
import { ApiError, api } from '~/lib/api';
import type { ItemCombinacion } from '~/lib/tipos';

export const prerender = false;

interface Peticion {
  monto_total?: string;
  horizonte_dias?: number;
  items?: ItemCombinacion[];
}

export const POST: APIRoute = async ({ request }) => {
  let cuerpo: Peticion;
  try {
    cuerpo = (await request.json()) as Peticion;
  } catch {
    return json({ error: 'Cuerpo inválido' }, 400);
  }

  if (!cuerpo.monto_total || !cuerpo.horizonte_dias || !cuerpo.items?.length) {
    return json({ error: 'Faltan monto, horizonte o instrumentos' }, 400);
  }

  try {
    return json(
      await api.combinacion({
        monto_total: cuerpo.monto_total,
        horizonte_dias: cuerpo.horizonte_dias,
        items: cuerpo.items,
      }),
    );
  } catch (error) {
    return desdeApi(error);
  }
};

export function json(datos: unknown, status = 200): Response {
  return new Response(JSON.stringify(datos), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/**
 * Traduce un fallo de la API interna a algo que la isla pueda mostrar.
 *
 * Nunca se reenvía el cuerpo del error tal cual: puede contener rutas,
 * nombres de tabla o el detalle de un 500, y nada de eso le sirve al usuario
 * ni debería salir del servidor.
 */
export function desdeApi(error: unknown): Response {
  if (error instanceof ApiError) {
    if (error.status === 404) {
      return json({ error: 'Alguno de los instrumentos ya no está disponible.' }, 404);
    }
    if (error.status === 504) {
      return json({ error: 'El servicio tardó demasiado. Vuelve a intentarlo.' }, 504);
    }
    console.error('[bff]', error.status, error.ruta, error.message);
    return json({ error: 'No se pudo calcular ahora mismo.' }, 502);
  }
  console.error('[bff] error inesperado', error);
  return json({ error: 'No se pudo calcular ahora mismo.' }, 502);
}
