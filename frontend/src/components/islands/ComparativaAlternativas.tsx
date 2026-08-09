/**
 * La combinación comparada contra sus referencias — como chart con eje.
 *
 * Un solo grid coloca etiquetas, barras y cifras: la capa de rejilla (un SVG
 * sin viewBox con coordenadas en %) atraviesa la columna de trazado de todas
 * las filas, así el eje es compartido de verdad; cada barra es otro SVG sin
 * viewBox en la misma columna, con la misma escala en %. Todo el texto vive
 * en HTML a tamaño fijo — un <text> dentro de un viewBox escala con el ancho
 * y se vuelve ilegible en móvil. La colocación por índice es necesaria: con
 * autoplacement, las celdas que la capa ocupa se esquivarían.
 *
 * La escala parte de 0 siempre (el dominio incluye el 0; una ganancia
 * negativa crece hacia la izquierda del cero, pintada de `--negativo`).
 * `pos()` y los ticks son geometría de presentación, como los width% del
 * resto de las gráficas. El delta del subtítulo se resta en centavos
 * enteros: es lo único aritmético-de-dinero del archivo y es exacto — nada
 * de coma flotante con dinero.
 *
 * Junto a cada cifra va su porcentaje protegido — el costo de cada camino a
 * la vista, que es el porqué de diversificar sin decirle a nadie qué hacer.
 * Un tono para las referencias, el acento (gradiente de marca) para la fila
 * propia; las cifras van en tinta de texto, nunca en el color de la barra.
 */
import { Fragment } from 'react';
import { dinero, dineroCorto } from '~/lib/formato';
import type { RespuestaCombinacion } from '~/lib/tipos';

function centavos(valor: string): number {
  const negativo = valor.trim().startsWith('-');
  const [entero, decimales = ''] = valor.replace('-', '').split('.');
  const monto = Number(entero) * 100 + Number((decimales + '00').slice(0, 2));
  return negativo ? -monto : monto;
}

function delta(alternativa: string, propia: string): string {
  const diferencia = centavos(alternativa) - centavos(propia);
  if (diferencia === 0) return 'igual que tu combinación';
  const absoluto = dinero((Math.abs(diferencia) / 100).toFixed(2));
  return diferencia > 0
    ? `${absoluto} más que tu combinación`
    : `${absoluto} menos que tu combinación`;
}

/** Ticks «bonitos» ([1, 2, 2.5, 5]×10^k), a lo sumo ~5, siempre con el 0. */
function ticksEje(minimo: number, maximo: number): number[] {
  const rango = maximo - minimo || 1;
  const base = Math.pow(10, Math.floor(Math.log10(rango / 4)));
  const paso =
    [1, 2, 2.5, 5, 10].map((m) => m * base).find((p) => rango / p <= 4.5) ?? base * 10;
  const ticks: number[] = [];
  for (let i = Math.ceil(minimo / paso); i <= Math.floor(maximo / paso); i += 1) {
    ticks.push(i * paso);
  }
  return ticks;
}

export default function ComparativaAlternativas({
  resultado,
}: {
  resultado: RespuestaCombinacion;
}) {
  if (resultado.alternativas.length === 0 || resultado.asignaciones.length === 0) {
    return null;
  }

  const filas = [
    {
      clave: 'propia',
      etiqueta: 'Tu combinación',
      ganancia_real: resultado.ganancia_real,
      porcentaje_protegido: resultado.porcentaje_protegido,
      propia: true,
    },
    ...resultado.alternativas.map((a) => ({ ...a, propia: false })),
  ];

  const valores = filas.map((f) => Number(f.ganancia_real));
  const minimo = Math.min(0, ...valores);
  const maximo = Math.max(0, ...valores);
  const rango = maximo - minimo || 1;
  const pos = (v: number) => ((v - minimo) / rango) * 100;
  const cero = pos(0);
  const ticks = ticksEje(minimo, maximo);

  return (
    <div className="benchmark">
      <div className="etiqueta">Comparado con</div>
      <p className="tenue-2 bm-criterio">
        Cada referencia se evalúa con tu monto y tu horizonte. El instrumento único es el de
        mayor ganancia real a ese monto; las banderas rojas quedan fuera.
      </p>
      <div className="bm-chart">
        <svg
          className="bm-rejilla"
          aria-hidden="true"
          preserveAspectRatio="none"
          style={{ gridColumn: 2, gridRow: `1 / span ${filas.length * 2}` }}
        >
          <defs>
            <linearGradient id="bm-acento" x1="0" y1="0" x2="1" y2="0">
              <stop offset="0%" style={{ stopColor: 'var(--marca-300)' }} />
              <stop offset="100%" style={{ stopColor: 'var(--marca-400)' }} />
            </linearGradient>
          </defs>
          {ticks.map((t) => (
            <line
              key={t}
              x1={`${pos(t)}%`}
              y1="0"
              x2={`${pos(t)}%`}
              y2="100%"
              className={t === 0 ? 'bm-linea-cero' : 'bm-linea'}
            />
          ))}
        </svg>

        {filas.map((fila, indice) => {
          const valor = Number(fila.ganancia_real);
          // Barra anclada al cero con un mínimo visible; la negativa crece a
          // la izquierda. Geometría de presentación, no dinero.
          const ancho = Math.max(1.5, Math.abs(pos(valor) - cero));
          const x = valor < 0 ? cero - ancho : cero;
          const clase =
            valor < 0 ? 'bm-barra negativa' : fila.propia ? 'bm-barra propia' : 'bm-barra';
          const filaBase = indice * 2 + 1;
          return (
            <Fragment key={fila.clave}>
              <span
                className={fila.propia ? 'bm-nombre bm-propia' : 'bm-nombre tenue'}
                style={{ gridColumn: 1, gridRow: filaBase }}
              >
                {fila.etiqueta}
              </span>
              <span
                className="bm-pista"
                style={{ gridColumn: 2, gridRow: filaBase }}
                aria-hidden="true"
              >
                <svg className="bm-barra-svg" preserveAspectRatio="none">
                  <rect className={clase} x={`${x}%`} y="0" width={`${ancho}%`} height="12" rx="3" />
                </svg>
              </span>
              <span
                className="cifra bm-valor"
                style={{ gridColumn: 3, gridRow: filaBase }}
              >
                {dinero(fila.ganancia_real)}
              </span>
              <span
                className="bm-sub tenue-2"
                style={{ gridColumn: '1 / -1', gridRow: filaBase + 1 }}
              >
                protegido {Number(fila.porcentaje_protegido)} %
                {!fila.propia && <> · {delta(fila.ganancia_real, resultado.ganancia_real)}</>}
              </span>
            </Fragment>
          );
        })}

        <span
          className="bm-eje"
          style={{ gridColumn: 2, gridRow: filas.length * 2 + 1 }}
        >
          {ticks.map((t) => {
            const p = pos(t);
            // Los extremos no se salen de la columna: ajustan su anclaje.
            const ajuste = p < 4 ? '0' : p > 96 ? '-100%' : '-50%';
            return (
              <span
                key={t}
                className="bm-tick"
                style={{ left: `${p}%`, transform: `translateX(${ajuste})` }}
              >
                {dineroCorto(t)}
              </span>
            );
          })}
        </span>
      </div>
      <style>{ESTILOS}</style>
    </div>
  );
}

const ESTILOS = `
.benchmark { margin-top: 16px; }
.bm-criterio { margin: 4px 0 10px; font-size: 11px; max-width: 60ch; }
.bm-chart {
  display: grid;
  grid-template-columns: minmax(110px, 190px) 1fr auto;
  column-gap: 10px;
  row-gap: 2px;
  align-items: center;
}
.bm-rejilla { width: 100%; height: 100%; min-width: 0; align-self: stretch; }
.bm-linea { stroke: var(--linea-suave); stroke-width: 1; }
.bm-linea-cero { stroke: var(--linea-fuerte); stroke-width: 1; }
.bm-nombre {
  overflow: hidden;
  font-size: 12px;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.bm-propia { font-weight: 600; color: var(--texto-fuerte); }
.bm-pista { display: block; min-width: 0; padding: 5px 0; }
.bm-barra-svg { display: block; width: 100%; height: 12px; }
.bm-barra { fill: var(--rampa-saldo-3); }
.bm-barra.propia { fill: url(#bm-acento); }
.bm-barra.negativa { fill: var(--negativo); }
.bm-valor { font-size: 13px; color: var(--texto-fuerte); }
.bm-sub { margin-bottom: 6px; font-size: 11px; }
.bm-eje { position: relative; display: block; height: 16px; font-size: 10px; }
.bm-tick {
  position: absolute;
  top: 2px;
  color: var(--texto-tenue-2);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
`;
