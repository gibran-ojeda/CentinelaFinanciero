/**
 * El porqué del reparto del optimizador.
 *
 * Los pasos del water-filling, en el orden en que ocurrieron: cada peso fue
 * al tramo con mejor TEN disponible en su vuelta, y cada paso dice dónde
 * cayó, cuánto, a qué tasa marginal y qué lo detuvo. Debajo, plegado, lo que
 * quedó fuera y por qué — la respuesta a «¿y por qué no me propuso X?»,
 * disponible cuando la pregunta surge y silencioso el resto del tiempo.
 *
 * Todo descriptivo, nada prescriptivo (criterios de redacción): el panel
 * cuenta lo que el algoritmo hizo, no lo que el usuario debería hacer.
 *
 * Los diccionarios de texto llano llevan fallback al valor crudo: si el
 * contrato gana una razón nueva antes de que este mapa la aprenda, se
 * enseña el código en vez de esconder el paso.
 */
import { colorSerie, dinero, etiquetaChipTramo, porcentaje } from '~/lib/formato';
import type { Asignacion, DescarteOptimizador, PasoOptimizador } from '~/lib/tipos';

const CORTES: Record<string, string> = {
  TRAMO_LLENO: 'se llenó el tramo',
  LIMITE_SEGURO: 'límite de seguro',
  MONTO_AGOTADO: 'se acabó el monto',
  COMPRA_MINIMO: 'compra del mínimo',
};

const DESCARTES: Record<string, string> = {
  TIENE_PLAZO: 'es a plazo: el reparto se pidió sólo con liquidez inmediata',
  PLAZO_MAYOR_AL_HORIZONTE: 'vence después del horizonte',
  MINIMO_SUPERA_MONTO: 'el mínimo supera tu monto',
  BANDERA_ROJA: 'bandera roja activa',
  ESCALERA_CRECIENTE: 'escalera creciente: fuera del reparto automático',
  SIN_COBERTURA: 'sin fondo de protección, con límites de seguro activos',
  EMISOR_LLENO: 'el emisor llegó a su límite',
  MINIMO_INALCANZABLE: 'el mínimo ya no cabía en lo restante',
};

export default function ExplicacionOptimizador({
  pasos,
  descartes,
  asignaciones,
}: {
  pasos: PasoOptimizador[];
  descartes: DescarteOptimizador[];
  asignaciones: Asignacion[];
}) {
  if (pasos.length === 0) return null;

  // El color de cada paso es el de su asignación en la barra y el detalle:
  // mismo índice, mismo tono, para que las tres vistas se lean como una.
  const porProducto = new Map(asignaciones.map((a, indice) => [a.producto_id, { a, indice }]));

  return (
    <div className="panel explicacion">
      <div className="etiqueta">Por qué este reparto</div>
      <p className="tenue criterio">
        Cada peso fue al tramo con la mejor tasa efectiva neta disponible en su vuelta. Estos son
        los pasos, en orden:
      </p>
      <ol className="pasos">
        {pasos.map((paso, i) => {
          const duenio = porProducto.get(paso.producto_id);
          return (
            <li key={i} className="paso-fila">
              <i
                className="punto"
                style={{ background: colorSerie(duenio?.indice ?? 0) }}
                aria-hidden="true"
              />
              <span className="paso-quien">{duenio?.a.institucion.nombre ?? paso.producto_id}</span>
              <span className="chip-tramo">{etiquetaChipTramo(paso.tramo)}</span>
              <span className="cifra">{dinero(paso.monto)}</span>
              <span className="tenue-2 paso-ten">TEN marginal {porcentaje(paso.ten_marginal)}</span>
              <span className="paso-corte">{CORTES[paso.razon_corte] ?? paso.razon_corte}</span>
              {paso.compra_minimo && (
                <span className="tenue-2 paso-nota">
                  la entrada compró el mínimo del producto, cruzando tramos
                </span>
              )}
            </li>
          );
        })}
      </ol>
      {descartes.length > 0 && (
        <details className="descartes">
          <summary className="tenue">Qué quedó fuera y por qué</summary>
          <ul>
            {descartes.map((d) => (
              <li key={d.producto_id} className="tenue descarte-fila">
                {d.institucion} — {d.producto}:{' '}
                <span className="tenue-2">{DESCARTES[d.razon] ?? d.razon}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
      <style>{ESTILOS}</style>
    </div>
  );
}

/* `.panel`, `.etiqueta`, `.punto` y `.cifra` vienen de la hoja del
   Combinador: los <style> de las islas son globales en la página. Aquí solo
   lo propio. */
const ESTILOS = `
.explicacion { margin-top: 14px; }
.explicacion .criterio { margin: 4px 0 10px; font-size: 13px; }
.pasos {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
  padding-left: 22px;
}
.paso-fila {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 10px;
  font-size: 13px;
}
.paso-fila::marker { color: var(--texto-tenue-2); font-size: 11px; }
.paso-quien { font-weight: 600; color: var(--texto-fuerte); }
/* .chip-tramo la define la hoja del Combinador, que siempre está montada. */
.paso-ten { font-size: 11px; }
.paso-corte {
  padding: 1px 8px;
  border: 1px dashed var(--linea-fuerte);
  border-radius: var(--radio-pastilla);
  font-size: 11px;
  color: var(--texto-tenue);
  white-space: nowrap;
}
.paso-nota { flex-basis: 100%; font-size: 11px; }
.descartes { margin-top: 10px; font-size: 13px; }
.descartes summary { cursor: pointer; }
.descartes ul {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 8px 0 0;
  padding-left: 18px;
}
.descarte-fila { font-size: 12px; }
`;
