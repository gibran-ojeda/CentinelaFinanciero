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

/**
 * Se lee de `process.env` **en cada llamada**, no una vez al importar.
 *
 * Vite no expone al bundle las variables sin prefijo `PUBLIC_`, así que
 * `import.meta.env.API_READ_KEY` llega vacío en el build de producción y la
 * API responde 401. Es la cara buena de la misma regla que impide filtrar la
 * llave al navegador.
 *
 * Leerlo en tiempo de ejecución es además lo correcto para el despliegue: la
 * llave la inyecta el compose en el contenedor, y hornearla en el artefacto
 * significaría reconstruir la imagen para rotarla.
 */
function entorno(clave: string, porDefecto = ''): string {
  return process.env[clave] ?? porDefecto;
}

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
  const base = entorno('API_BASE_URL', 'http://127.0.0.1:8010');
  const llave = entorno('API_READ_KEY');
  const timeoutMs = Number(entorno('API_TIMEOUT_SECONDS', '10')) * 1000;

  // Un timeout explícito: sin él, una API colgada deja la petición del usuario
  // esperando hasta que el navegador se rinda, sin nada que mostrar.
  const control = new AbortController();
  const reloj = setTimeout(() => control.abort(), timeoutMs);

  try {
    const respuesta = await fetch(`${base}${ruta}`, {
      ...init,
      signal: control.signal,
      headers: {
        'X-API-Key': llave,
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
      throw new ApiError(504, ruta, `La API no respondió en ${timeoutMs / 1000}s`);
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
