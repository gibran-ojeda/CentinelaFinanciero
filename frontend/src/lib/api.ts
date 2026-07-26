/**
 * Cliente de la API interna. **Sólo servidor.**
 *
 * El navegador nunca habla con la API: habla con este servicio, que añade la
 * `X-API-Key` y reenvía. La garantía no es una convención sino mecánica —
 * `API_READ_KEY` no lleva prefijo `PUBLIC_`, así que Astro no puede incluirla
 * en el bundle del cliente ni por accidente.
 *
 * Importar este módulo desde una isla React rompe el build, y eso es
 * deliberado: es la única forma de que la llave no acabe en el navegador por
 * un import descuidado dentro de seis meses.
 */

import type {
  DetalleInstitucion,
  FiltrosMercado,
  ItemCombinacion,
  RespuestaCombinacion,
  RespuestaComparador,
  RespuestaFrescura,
} from './tipos';

const BASE = import.meta.env.API_BASE_URL ?? 'http://127.0.0.1:8010';
const LLAVE = import.meta.env.API_READ_KEY ?? '';
const TIMEOUT_MS = Number(import.meta.env.API_TIMEOUT_SECONDS ?? 10) * 1000;

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly ruta: string,
    mensaje: string,
  ) {
    super(mensaje);
    this.name = 'ApiError';
  }
}

async function pedir<T>(ruta: string, init?: RequestInit): Promise<T> {
  // Un timeout explícito: sin él, una API colgada deja la petición del usuario
  // esperando hasta que el navegador se rinda, sin nada que mostrar.
  const control = new AbortController();
  const reloj = setTimeout(() => control.abort(), TIMEOUT_MS);

  try {
    const respuesta = await fetch(`${BASE}${ruta}`, {
      ...init,
      signal: control.signal,
      headers: {
        'X-API-Key': LLAVE,
        Accept: 'application/json',
        ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
        ...init?.headers,
      },
    });

    if (!respuesta.ok) {
      const detalle = await respuesta.text().catch(() => '');
      throw new ApiError(respuesta.status, ruta, detalle.slice(0, 300));
    }
    return (await respuesta.json()) as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof Error && error.name === 'AbortError') {
      throw new ApiError(504, ruta, `La API no respondió en ${TIMEOUT_MS / 1000}s`);
    }
    throw new ApiError(502, ruta, error instanceof Error ? error.message : 'error desconocido');
  } finally {
    clearTimeout(reloj);
  }
}

/**
 * Traduce los filtros a query string.
 *
 * `seguro` y `categoria` se repiten en vez de unirse por comas: es lo que la
 * API acepta y lo que hace que `?seguro=IPAB&seguro=PROSOFIPO` sea una URL
 * legible y compartible, que es el punto de tenerlos en la query.
 */
export function filtrosAQuery(filtros: Partial<FiltrosMercado>): URLSearchParams {
  const query = new URLSearchParams();
  if (filtros.plazo && filtros.plazo !== 'TODOS') query.set('plazo', filtros.plazo);
  for (const seguro of filtros.seguros ?? []) query.append('seguro', seguro);
  for (const categoria of filtros.categorias ?? []) query.append('categoria', categoria);
  if (filtros.monto) query.set('monto', filtros.monto);
  if (filtros.orden) query.set('orden', filtros.orden);
  if (filtros.sinBanderas) query.set('sin_banderas', 'true');
  return query;
}

export const api = {
  comparador(filtros: Partial<FiltrosMercado> = {}): Promise<RespuestaComparador> {
    const query = filtrosAQuery(filtros).toString();
    return pedir<RespuestaComparador>(`/api/v1/comparador${query ? `?${query}` : ''}`);
  },

  institucion(referencia: string): Promise<DetalleInstitucion> {
    return pedir<DetalleInstitucion>(`/api/v1/instituciones/${encodeURIComponent(referencia)}`);
  },

  frescura(): Promise<RespuestaFrescura> {
    return pedir<RespuestaFrescura>('/api/v1/meta/frescura');
  },

  combinacion(cuerpo: {
    monto_total: string;
    horizonte_dias: number;
    items: ItemCombinacion[];
  }): Promise<RespuestaCombinacion> {
    return pedir<RespuestaCombinacion>('/api/v1/calculadora/combinacion', {
      method: 'POST',
      body: JSON.stringify(cuerpo),
    });
  },

  optimizar(cuerpo: {
    monto_total: string;
    horizonte_dias: number;
    respetar_seguro: boolean;
    excluir_rojas: boolean;
  }): Promise<RespuestaCombinacion> {
    return pedir<RespuestaCombinacion>('/api/v1/calculadora/optimizar', {
      method: 'POST',
      body: JSON.stringify(cuerpo),
    });
  },
};
