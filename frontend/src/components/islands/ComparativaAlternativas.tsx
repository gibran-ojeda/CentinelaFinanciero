/**
 * La combinación comparada contra sus referencias.
 *
 * Barras horizontales de ganancia real con base en 0: «Tu combinación» y las
 * alternativas que la API evaluó con el mismo monto y horizonte (todo en
 * CETES, todo en el instrumento único de mayor ganancia). Junto a cada cifra
 * va su porcentaje protegido — el costo de cada camino a la vista, que es el
 * porqué de diversificar sin decirle a nadie qué hacer.
 *
 * Una sola medida ⇒ un solo tono para las referencias, y el acento (el mismo
 * gradiente que la cascada usa para la ganancia real) para la fila propia.
 * Las cifras van en tinta de texto, nunca en el color de la barra; una
 * ganancia negativa pinta su barra de `--negativo`, como en la cascada.
 *
 * El delta es texto descriptivo («$X más/menos que tu combinación»). La
 * resta se hace en centavos enteros: es lo único aritmético del archivo y
 * es exacto — nada de coma flotante con dinero.
 */
import { dinero } from '~/lib/formato';
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
  const maxima = Math.max(...filas.map((f) => Math.abs(Number(f.ganancia_real))), 1);

  return (
    <div className="benchmark">
      <div className="etiqueta">Comparado con</div>
      <p className="tenue-2 bm-criterio">
        Cada referencia se evalúa con tu monto y tu horizonte. El instrumento único es el de
        mayor ganancia real a ese monto; las banderas rojas quedan fuera.
      </p>
      {filas.map((fila) => {
        const valor = Number(fila.ganancia_real);
        const ancho = `${Math.max(2, (Math.abs(valor) / maxima) * 100).toFixed(1)}%`;
        const claseBarra = valor < 0 ? 'bm-barra negativa' : fila.propia ? 'bm-barra propia' : 'bm-barra';
        return (
          <div key={fila.clave} className="bm-fila">
            <span className={fila.propia ? 'bm-nombre bm-propia' : 'bm-nombre tenue'}>
              {fila.etiqueta}
            </span>
            <span className="bm-pista" aria-hidden="true">
              <span className={claseBarra} style={{ width: ancho }} />
            </span>
            <span className="cifra bm-valor">{dinero(fila.ganancia_real)}</span>
            <span className="bm-sub tenue-2">
              protegido {Number(fila.porcentaje_protegido)} %
              {!fila.propia && <> · {delta(fila.ganancia_real, resultado.ganancia_real)}</>}
            </span>
          </div>
        );
      })}
      <style>{ESTILOS}</style>
    </div>
  );
}

const ESTILOS = `
.benchmark { margin-top: 16px; }
.bm-criterio { margin: 4px 0 10px; font-size: 11px; max-width: 60ch; }
.bm-fila {
  display: grid;
  grid-template-columns: minmax(110px, 190px) 1fr auto;
  gap: 2px 10px;
  align-items: center;
  padding: 4px 0;
}
.bm-nombre {
  overflow: hidden;
  font-size: 12px;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.bm-propia { font-weight: 600; color: var(--texto-fuerte); }
.bm-pista { min-width: 0; }
.bm-barra {
  display: block;
  height: 10px;
  border-radius: 0 4px 4px 0;
  background: var(--rampa-saldo-3);
}
.bm-barra.propia { background: var(--gradiente-acento); }
.bm-barra.negativa { background: var(--negativo); }
.bm-valor { font-size: 13px; color: var(--texto-fuerte); }
.bm-sub { grid-column: 1 / -1; font-size: 11px; }
`;
