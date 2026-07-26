/** BFF del optimizador. Ver `combinacion.ts` para el porqué de esta capa. */

import type { APIRoute } from 'astro';
import { api } from '~/lib/api';
import { desdeApi, json } from './combinacion';

export const prerender = false;

interface Peticion {
  monto_total?: string;
  horizonte_dias?: number;
  respetar_seguro?: boolean;
  excluir_rojas?: boolean;
}

export const POST: APIRoute = async ({ request }) => {
  let cuerpo: Peticion;
  try {
    cuerpo = (await request.json()) as Peticion;
  } catch {
    return json({ error: 'Cuerpo inválido' }, 400);
  }

  if (!cuerpo.monto_total || !cuerpo.horizonte_dias) {
    return json({ error: 'Faltan monto u horizonte' }, 400);
  }

  try {
    return json(
      await api.optimizar({
        monto_total: cuerpo.monto_total,
        horizonte_dias: cuerpo.horizonte_dias,
        // Los dos interruptores vienen encendidos si no se dicen: el defecto
        // seguro es proteger, no maximizar.
        respetar_seguro: cuerpo.respetar_seguro ?? true,
        excluir_rojas: cuerpo.excluir_rojas ?? true,
      }),
    );
  } catch (error) {
    return desdeApi(error);
  }
};
