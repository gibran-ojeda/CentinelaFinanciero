/**
 * La calculadora de combinación.
 *
 * Regla que gobierna el archivo entero: **aquí no se calcula nada.** Ni la
 * TEN, ni la cascada, ni el reparto del optimizador. Todo sale de la API,
 * donde vive en `Decimal` y con tests. Lo que hace este componente es capturar
 * la entrada, pedir el cálculo y dibujar el resultado.
 *
 * Es la misma razón por la que el optimizador está en Python: un reparto
 * hecho en coma flotante acumula error a lo largo de la cascada y acaba
 * mostrando que la suma no cuadra, justo en la pantalla cuyo argumento es que
 * los números cuadran.
 */

import { useStore } from '@nanostores/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ComparativaAlternativas from '~/components/islands/ComparativaAlternativas';
import ExplicacionOptimizador from '~/components/islands/ExplicacionOptimizador';
import {
  colorSerie,
  dinero,
  dineroCorto,
  etiquetaChipTramo,
  miles,
  porcentaje,
  soloDigitos,
} from '~/lib/formato';
import { limpiar, quitar, repartoInicial, seleccion, type Instrumento } from '~/lib/seleccion';
import type { RespuestaCombinacion } from '~/lib/tipos';

const HORIZONTES = [28, 91, 182, 364];
const PASO_MONTO = 1_000;

export default function Combinador() {
  const instrumentos = useStore(seleccion);

  const [montoTexto, setMontoTexto] = useState('250,000');
  // 'vista' = liquidez inmediata: sólo productos sin plazo, proyectados a un
  // año. Para la API el horizonte sigue siendo un número de días — la huella
  // y las peticiones usan siempre `horizonteDias`, nunca el valor unión, para
  // que alternar 364 ↔ vista produzca peticiones idénticas y no invalide el
  // resultado del optimizador.
  const [horizonte, setHorizonte] = useState<number | 'vista'>(91);
  const modoVista = horizonte === 'vista';
  const horizonteDias = modoVista ? 364 : horizonte;
  const [respetarSeguro, setRespetarSeguro] = useState(true);
  const [excluirRojas, setExcluirRojas] = useState(true);
  const [pesos, setPesos] = useState<Record<number, string>>({});

  const [resultado, setResultado] = useState<RespuestaCombinacion | null>(null);
  const [cargando, setCargando] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const monto = soloDigitos(montoTexto);

  // Edición de la entrada, no cálculo de dinero: suma enteros de pesos al
  // texto del campo, igual que teclear dígitos.
  function ajustarMonto(delta: number) {
    const siguiente = Math.max(0, (Number(soloDigitos(montoTexto)) || 0) + delta);
    setMontoTexto(miles(String(siguiente)));
  }

  /**
   * Los porcentajes se reajustan cuando cambia la selección.
   *
   * Añadir un instrumento reescala los que ya estaban y le da su parte; quitar
   * uno reparte lo suyo entre el resto. Es lo que evita que el usuario tenga
   * que recalcular a mano cada vez que toca la lista.
   */
  useEffect(() => {
    setPesos((previos) => {
      const ids = instrumentos.map((i) => i.productoId);
      const conocidos = ids.filter((id) => previos[id] !== undefined);
      if (conocidos.length === ids.length && ids.length === Object.keys(previos).length) {
        return previos;
      }
      const reparto = repartoInicial(ids.length);
      return Object.fromEntries(ids.map((id, indice) => [id, reparto[indice]]));
    });
  }, [instrumentos]);

  const sumaPesos = useMemo(
    () => Object.values(pesos).reduce((total, p) => total + (Number(p) || 0), 0),
    [pesos],
  );

  // Evita disparar una petición por tecla mientras se escribe el monto.
  const temporizador = useRef<ReturnType<typeof setTimeout> | null>(null);

  // La huella de la última propuesta del optimizador. Optimizar reescribe la
  // selección y los pesos, y eso re-dispara el recálculo automático ~300 ms
  // después: sin esto, el POST a /api/combinacion pisaba el resultado y
  // mataba lo que sólo el optimizador devuelve (pasos, descartes,
  // monto_no_asignado). Si la entrada actual ES la propuesta, no hay nada
  // que recalcular; en cuanto el usuario edita algo, la huella deja de
  // coincidir y el flujo normal continúa.
  const huellaOptimizador = useRef<string | null>(null);

  const calcular = useCallback(async () => {
    if (instrumentos.length === 0 || !monto || Number(monto) <= 0) {
      setResultado(null);
      return;
    }
    const huellaActual = JSON.stringify([
      monto,
      horizonteDias,
      instrumentos.map((i) => i.productoId),
      instrumentos.map((i) => pesos[i.productoId] ?? '0'),
    ]);
    if (huellaOptimizador.current === huellaActual) return;
    huellaOptimizador.current = null;
    setCargando(true);
    setError(null);
    try {
      const respuesta = await fetch('/api/combinacion', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          monto_total: monto,
          horizonte_dias: horizonteDias,
          items: instrumentos.map((i) => ({
            producto_id: i.productoId,
            porcentaje: pesos[i.productoId] ?? '0',
          })),
        }),
      });
      const datos = await respuesta.json();
      if (!respuesta.ok) throw new Error(datos.error ?? 'No se pudo calcular');
      setResultado(datos as RespuestaCombinacion);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'No se pudo calcular');
      setResultado(null);
    } finally {
      setCargando(false);
    }
  }, [instrumentos, monto, horizonteDias, pesos]);

  useEffect(() => {
    if (temporizador.current) clearTimeout(temporizador.current);
    temporizador.current = setTimeout(calcular, 300);
    return () => {
      if (temporizador.current) clearTimeout(temporizador.current);
    };
  }, [calcular]);

  async function optimizar() {
    if (!monto || Number(monto) <= 0) return;
    setCargando(true);
    setError(null);
    try {
      const respuesta = await fetch('/api/optimizar', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          monto_total: monto,
          horizonte_dias: horizonteDias,
          respetar_seguro: respetarSeguro,
          excluir_rojas: excluirRojas,
          solo_vista: modoVista,
        }),
      });
      const datos = await respuesta.json();
      if (!respuesta.ok) throw new Error(datos.error ?? 'No se pudo optimizar');

      const propuesta = datos as RespuestaCombinacion;
      // El optimizador reemplaza la selección: es lo que el diseño anuncia
      // bajo el botón, y lo que hace que el resultado en pantalla sea el que
      // se acaba de proponer y no una mezcla con lo anterior.
      seleccion.set(
        propuesta.asignaciones.map((a) => ({
          productoId: a.producto_id,
          institucion: a.institucion.nombre,
          producto: a.producto,
          slug: a.producto_slug,
        })),
      );
      setPesos(
        Object.fromEntries(propuesta.asignaciones.map((a) => [a.producto_id, a.porcentaje])),
      );
      setResultado(propuesta);
      // Construida desde la propuesta, en el mismo orden en que quedará la
      // selección: el recálculo que estos set van a disparar la comparará y
      // se saltará el fetch, conservando la respuesta exacta del optimizador.
      huellaOptimizador.current = JSON.stringify([
        monto,
        horizonteDias,
        propuesta.asignaciones.map((a) => a.producto_id),
        propuesta.asignaciones.map((a) => a.porcentaje),
      ]);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'No se pudo optimizar');
    } finally {
      setCargando(false);
    }
  }

  const sumaOk = Math.abs(sumaPesos - 100) < 1;
  const real = resultado ? Number(resultado.ganancia_real) : 0;
  const protegido = resultado ? Number(resultado.porcentaje_protegido) : 0;

  // La etiqueta de vista exige que TODO lo asignado sea sin plazo: con la
  // pastilla activa y un plazo agregado a mano, «disponible en cualquier
  // momento» mentiría — se vuelve al número de días.
  const todasVista =
    resultado !== null &&
    resultado.asignaciones.length > 0 &&
    resultado.asignaciones.every((a) => a.plazo_dias === null);
  const subtituloReparto = resultado
    ? modoVista && todasVista
      ? 'disponible en cualquier momento · proyección a 1 año'
      : `${resultado.horizonte_dias} días`
    : null;

  return (
    <div className="rejilla">
      {/* ── Entrada ── */}
      <div className="panel">
        <div className="campo">
          <label htmlFor="c-monto">Monto total (MXN)</label>
          <div className="monto-grupo">
            <button
              type="button"
              className="monto-paso"
              aria-label="Restar $1,000"
              onClick={() => ajustarMonto(-PASO_MONTO)}
            >
              −
            </button>
            <input
              id="c-monto"
              className="control monto"
              type="text"
              inputMode="numeric"
              value={montoTexto}
              onChange={(e) => setMontoTexto(miles(soloDigitos(e.target.value)))}
              onKeyDown={(e) => {
                if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
                  e.preventDefault();
                  ajustarMonto(e.key === 'ArrowUp' ? PASO_MONTO : -PASO_MONTO);
                }
              }}
            />
            <button
              type="button"
              className="monto-paso"
              aria-label="Sumar $1,000"
              onClick={() => ajustarMonto(PASO_MONTO)}
            >
              +
            </button>
          </div>
        </div>

        <div className="campo">
          <span className="pseudo-label">Horizonte</span>
          <div className="chips">
            {HORIZONTES.map((dias) => (
              <button
                key={dias}
                type="button"
                className={`pastilla ${horizonte === dias ? 'activa' : ''}`}
                aria-pressed={horizonte === dias}
                onClick={() => setHorizonte(dias)}
              >
                {dias} días
              </button>
            ))}
            <button
              type="button"
              className={`pastilla ${modoVista ? 'activa' : ''}`}
              aria-pressed={modoVista}
              onClick={() => setHorizonte('vista')}
            >
              A la vista
            </button>
          </div>
        </div>

        <div className="opciones">
          <button
            type="button"
            className={`opcion ${respetarSeguro ? 'activa' : ''}`}
            aria-pressed={respetarSeguro}
            onClick={() => setRespetarSeguro((v) => !v)}
          >
            <span>Respetar límites de seguro</span>
            <strong>{respetarSeguro ? 'Sí' : 'No'}</strong>
          </button>
          <button
            type="button"
            className={`opcion ${excluirRojas ? 'activa' : ''}`}
            aria-pressed={excluirRojas}
            onClick={() => setExcluirRojas((v) => !v)}
          >
            <span>Excluir banderas rojas</span>
            <strong>{excluirRojas ? 'Sí' : 'No'}</strong>
          </button>
        </div>

        <button type="button" className="optimizar" onClick={optimizar} disabled={cargando}>
          ⚡ Buscar mejor alternativa
        </button>
        <p className="nota-optimizar">
          La búsqueda <strong>reemplaza tu selección actual</strong>: ordena por tasa efectiva
          neta y llena cada institución hasta su límite de cobertura
          {respetarSeguro
            ? '.'
            : ' — con los límites desactivados, todo el monto puede ir a un solo emisor.'}
        </p>
      </div>

      {/* ── Resultados ── */}
      <div>
        {error && <p className="error">{error}</p>}

        {instrumentos.length === 0 && !resultado && !error && (
          <div className="panel espera">
            <p className="tenue">
              Elige un monto y agrega instrumentos desde el <a href="/">mercado</a>, o pulsa{' '}
              <strong>Buscar mejor alternativa</strong> para una propuesta automática.
            </p>
          </div>
        )}

        {(instrumentos.length > 0 || resultado) && (
          <>
            <div className={`panel ${cargando ? 'calculando' : ''}`}>
              {resultado && (
                <div className="stats">
                  <div className="stat">
                    <div className="stat-etiqueta">TEN ponderada</div>
                    <div className="cifra stat-valor positivo">
                      {porcentaje(resultado.ten_ponderada)}
                    </div>
                  </div>
                  <div className="stat">
                    <div className="stat-etiqueta">Ganancia real</div>
                    <div
                      className="cifra stat-valor"
                      style={{ color: real < 0 ? 'var(--negativo)' : 'var(--positivo)' }}
                    >
                      {dinero(resultado.ganancia_real)}
                    </div>
                  </div>
                  <div className="stat">
                    <div className="stat-etiqueta">Protegido</div>
                    <div
                      className="cifra stat-valor"
                      style={{
                        color:
                          protegido >= 100
                            ? 'var(--positivo)'
                            : protegido >= 60
                              ? 'var(--aviso)'
                              : 'var(--negativo)',
                      }}
                    >
                      {protegido} %
                    </div>
                    <div className="tenue-2 stat-sub">{dinero(resultado.monto_protegido)}</div>
                  </div>
                </div>
              )}

              <RepartoDona
                instrumentos={instrumentos}
                pesos={pesos}
                resultado={resultado}
                monto={monto}
                sumaPesos={sumaPesos}
                sumaOk={sumaOk}
                subtitulo={subtituloReparto}
                alCambiarPeso={(productoId, valor) =>
                  setPesos((previos) => ({ ...previos, [productoId]: valor }))
                }
              />

              {resultado && (
                <>
                  <Cascada datos={resultado} />

                  <ComparativaAlternativas resultado={resultado} />

                  <p className="narrativa">{resultado.narrativa}</p>

                  {Number(resultado.monto_no_asignado) > 0 && (
                    <p className="sin-asignar">
                      {dinero(resultado.monto_no_asignado)} no se pudieron colocar sin exceder
                      algún límite de seguro. Repartirlos entre los instrumentos ya llenos
                      anularía la protección que pediste respetar.
                    </p>
                  )}
                </>
              )}
            </div>

            {resultado && (
              <>
                <ExplicacionOptimizador
                  pasos={resultado.pasos_optimizador}
                  descartes={resultado.descartes_optimizador}
                  asignaciones={resultado.asignaciones}
                />

                <div className="avisos">
                  <div className="aviso-caja">
                    <div className="aviso-titulo">Nota fiscal</div>
                    <p className="tenue aviso-texto">{resultado.nota_fiscal}</p>
                  </div>
                  <div className="aviso-caja">
                    <div className="aviso-titulo">Sobre el optimizador</div>
                    <p className="tenue aviso-texto">{resultado.aviso_optimizador}</p>
                  </div>
                  <p className="tenue-2 disclaimer">{resultado.disclaimer}</p>
                </div>
              </>
            )}
          </>
        )}
      </div>

      <style>{ESTILOS}</style>
    </div>
  );
}

/**
 * El reparto como dona anotada.
 *
 * Un solo bloque cuenta lo que antes contaban tres (la lista de instrumentos,
 * la barra de asignación y el detalle por instrumento): quiénes son, cuánto
 * les toca y qué hace cada peso. Las filas nacen del store —un instrumento
 * recién quitado desaparece al instante— y las cifras llegan del resultado,
 * unidas por id; sin resultado, la fila es su propio esqueleto durante el
 * debounce.
 *
 * La dona es geometría de porcentajes ya calculados por la API: con
 * `pathLength=100`, el dasharray de cada gajo ES su porcentaje, sin
 * trigonometría. El centro va en HTML superpuesto, nunca en <text> SVG, que
 * escala con el ancho. Sin resultado no se dibujan gajos provisionales: los
 * pesos no se normalizan en cliente para adivinar un reparto.
 */
function RepartoDona({
  instrumentos,
  pesos,
  resultado,
  monto,
  sumaPesos,
  sumaOk,
  subtitulo,
  alCambiarPeso,
}: {
  instrumentos: Instrumento[];
  pesos: Record<number, string>;
  resultado: RespuestaCombinacion | null;
  monto: string;
  sumaPesos: number;
  sumaOk: boolean;
  subtitulo: string | null;
  alCambiarPeso: (productoId: number, valor: string) => void;
}) {
  const asignaciones = resultado?.asignaciones ?? [];
  const porProducto = new Map(asignaciones.map((a) => [a.producto_id, a]));

  // Offset acumulado de cada gajo sobre pathLength=100: geometría, no dinero.
  const offsets: number[] = [];
  let acumulado = 0;
  for (const a of asignaciones) {
    offsets.push(acumulado);
    acumulado += Number(a.porcentaje);
  }

  return (
    <div className="reparto">
      <div className="reparto-cabecera">
        <span className="reparto-titulo">
          <span className="etiqueta">Tu reparto</span>
          {subtitulo && <span className="tenue-2 reparto-sub">{subtitulo}</span>}
        </span>
        <span className="suma">
          <span style={{ color: sumaOk ? 'var(--positivo)' : 'var(--aviso)' }}>
            Σ {sumaPesos.toFixed(1)} %
          </span>
          {instrumentos.length > 0 && (
            <button type="button" className="enlace" onClick={limpiar}>
              Limpiar
            </button>
          )}
        </span>
      </div>

      <div className="reparto-cuerpo">
        <div className="dona-caja">
          <svg className="dona" viewBox="0 0 100 100" aria-hidden="true">
            <g transform="rotate(-90 50 50)">
              <circle className="dona-pista" cx="50" cy="50" r="40" />
              {asignaciones.map((a, indice) => (
                <circle
                  key={a.producto_id}
                  cx="50"
                  cy="50"
                  r="40"
                  pathLength={100}
                  strokeDasharray={`${Number(a.porcentaje)} ${100 - Number(a.porcentaje)}`}
                  strokeDashoffset={-offsets[indice]}
                  style={{ stroke: colorSerie(indice) }}
                />
              ))}
            </g>
          </svg>
          <div className="dona-centro">
            <div className="cifra dona-monto">{dineroCorto(resultado?.monto_total ?? monto)}</div>
            <div className="tenue-2">
              {instrumentos.length} instrumento{instrumentos.length === 1 ? '' : 's'}
            </div>
          </div>
        </div>

        <div className="reparto-filas">
          {instrumentos.map((instrumento, indice) => {
            const a = porProducto.get(instrumento.productoId);
            return (
              <div key={instrumento.productoId} className="fila-instrumento">
                <div className="fila-linea">
                  <i
                    className="punto"
                    style={{ background: colorSerie(indice) }}
                    aria-hidden="true"
                  />
                  <span className="fila-nombre">{instrumento.institucion}</span>
                  {a ? (
                    /* El enlace usa el slug de la institución que viaja en la
                       asignación: el del store es el del producto. */
                    <a className="fila-producto tenue" href={`/institucion/${a.institucion.slug}`}>
                      {a.producto}
                    </a>
                  ) : (
                    <span className="fila-producto tenue">{instrumento.producto}</span>
                  )}
                  <span className="fila-peso">
                    <input
                      className="pct"
                      type="number"
                      min="0"
                      max="100"
                      step="0.1"
                      aria-label={`Porcentaje de ${instrumento.institucion}`}
                      value={pesos[instrumento.productoId] ?? ''}
                      onChange={(e) => alCambiarPeso(instrumento.productoId, e.target.value)}
                    />
                    <span className="tenue-2">%</span>
                  </span>
                  <button
                    type="button"
                    className="quitar"
                    aria-label={`Quitar ${instrumento.institucion}`}
                    onClick={() => quitar(instrumento.productoId)}
                  >
                    ×
                  </button>
                </div>
                {a && (
                  <div className="fila-datos">
                    <span className="tenue">
                      {dinero(a.monto)} ({Number(a.porcentaje).toFixed(1)}%)
                    </span>
                    <span className="positivo">TEN {porcentaje(a.ten)}</span>
                    <span className="tenue">real {dinero(a.cascada.ganancia_real)}</span>
                    <span
                      className="cobertura"
                      style={{ color: a.cubierto ? 'var(--positivo)' : 'var(--aviso)' }}
                    >
                      {a.cubierto
                        ? `Cubierto (${a.cobertura.tipo === 'SOBERANO' ? 'soberano' : a.cobertura.tipo})`
                        : `Excede cobertura ${dinero(a.monto_expuesto)}`}
                    </span>
                  </div>
                )}
                {a?.escalonada && (
                  /* La TEN de la fila ya es la efectiva del monto asignado
                     (la calcula la API); los chips enseñan la escalera que la
                     explica — visibles, no en un title que el táctil no puede
                     abrir. */
                  <div className="fila-tramos">
                    {a.tramos.map((t) => (
                      <span key={t.desde} className="chip-tramo">
                        {etiquetaChipTramo(t)}
                      </span>
                    ))}
                  </div>
                )}
                {a?.advertencia_liquidez && (
                  <div className="aviso-liquidez">{a.advertencia_liquidez}</div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

/** La cascada de §6: cuatro barras, no una tabla. */
function Cascada({ datos }: { datos: RespuestaCombinacion }) {
  const bruto = Number(datos.rendimiento_bruto) || 1;
  const isr = Number(datos.isr_retenido);
  const inflacion = Number(datos.efecto_inflacion);
  const real = Number(datos.ganancia_real);
  const negativa = real < 0;

  const ancho = (v: number) => `${Math.max(2, Math.round((Math.abs(v) / bruto) * 100))}%`;
  const desde = (v: number) => `${Math.max(0, Math.round((v / bruto) * 100))}%`;

  const pasos = [
    {
      etiqueta: 'Rendimiento bruto',
      monto: `+${dinero(datos.rendimiento_bruto)}`,
      izquierda: '0%',
      ancho: '100%',
      color: 'var(--positivo)',
      texto: 'var(--texto)',
      sub: `${datos.asignaciones.length} instrumento${datos.asignaciones.length === 1 ? '' : 's'} × ${datos.horizonte_dias} días`,
    },
    {
      etiqueta: 'ISR retenido',
      monto: `−${dinero(datos.isr_retenido)}`,
      izquierda: desde(bruto - isr),
      ancho: ancho(isr),
      color: 'var(--aviso)',
      texto: 'var(--aviso)',
      // Antes derivaba la tasa restando nominal − TEN **del primer
      // instrumento** y la presentaba como global: en una mezcla heterogénea
      // describía a uno solo. La tasa exacta ya viaja en la nota fiscal.
      sub: 'Retención de ISR sobre el capital invertido; la tasa exacta está en la nota fiscal.',
    },
    {
      etiqueta: 'Efecto inflación',
      monto: `−${dinero(datos.efecto_inflacion)}`,
      izquierda: desde(Math.max(0, bruto - isr - inflacion)),
      ancho: ancho(inflacion),
      color: 'var(--cascada-inflacion)',
      texto: 'var(--cascada-inflacion-tinta)',
      sub: `Pérdida de poder adquisitivo (${porcentaje(datos.inflacion_anual)} anual)`,
    },
    {
      etiqueta: 'Ganancia real',
      monto: `${negativa ? '−' : ''}${dinero(Math.abs(real))}`,
      izquierda: '0%',
      ancho: ancho(real),
      color: negativa ? 'var(--negativo)' : 'var(--gradiente-acento)',
      texto: negativa ? 'var(--negativo)' : 'var(--positivo)',
      sub: negativa
        ? 'Con estos supuestos pierdes poder adquisitivo.'
        : 'Lo que de verdad crece tu patrimonio',
    },
  ];

  return (
    <div className="cascada">
      {pasos.map((paso) => (
        <div key={paso.etiqueta}>
          <div className="paso-cabecera">
            <span className="paso-etiqueta" style={{ color: paso.texto }}>
              {paso.etiqueta}
            </span>
            <span className="cifra paso-monto" style={{ color: paso.texto }}>
              {paso.monto}
            </span>
          </div>
          <div className="pista">
            <div
              className="relleno"
              style={{ left: paso.izquierda, width: paso.ancho, background: paso.color }}
            />
          </div>
          <div className="tenue-2 paso-sub">{paso.sub}</div>
        </div>
      ))}
    </div>
  );
}

const ESTILOS = `
  .rejilla { display: grid; gap: 18px; }
  .panel {
    padding: 18px;
    border: 1px solid var(--linea);
    border-radius: var(--radio-tarjeta);
    background: var(--gradiente-panel);
    box-shadow: var(--sombra-md);
  }
  .panel.calculando { opacity: .75; }
  .espera { color: var(--texto-tenue); }

  .campo { margin-bottom: 12px; }
  .campo label, .pseudo-label {
    display: block; margin-bottom: 5px; font-size: 12px; color: var(--texto-tenue);
  }
  .control {
    width: 100%; min-height: 36px; padding: 6px 12px; font-size: 14px;
    color: var(--texto-fuerte); background: var(--superficie);
    border: 1px solid var(--linea); border-radius: var(--radio-campo);
  }
  .monto { min-height: 44px; font-size: 18px; font-variant-numeric: tabular-nums; }
  .monto-grupo { display: flex; gap: 6px; }
  .monto-grupo .control { flex: 1; min-width: 0; }
  /* «monto-paso», no «paso-monto»: la cascada ya usa .paso-monto para sus
     cifras y esta hoja es global en la página. */
  .monto-paso {
    width: 44px; min-height: 44px; flex: none; cursor: pointer;
    font-size: 18px; line-height: 1; color: var(--texto-fuerte);
    background: var(--superficie); border: 1px solid var(--linea);
    border-radius: var(--radio-campo);
  }
  .monto-paso:hover { border-color: var(--linea-fuerte); }

  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }

  .pastilla {
    min-height: 36px; padding: 7px 15px; cursor: pointer; white-space: nowrap;
    border: 1px solid var(--linea); border-radius: var(--radio-pastilla);
    background: transparent; color: var(--texto-tenue);
    font-family: var(--fuente-titulo); font-weight: 600; font-size: 13px;
  }
  .pastilla.activa {
    border-color: rgba(95,176,201,.6); background: var(--gradiente-acento);
    color: var(--texto-fuerte);
  }

  .opciones { display: grid; gap: 8px; margin-bottom: 14px; }
  .opcion {
    display: flex; justify-content: space-between; align-items: center; gap: 10px;
    padding: 10px 14px; cursor: pointer; text-align: left; font-size: 13px;
    border: 1px solid var(--linea); border-radius: var(--radio-sub);
    background: transparent; color: var(--texto-tenue); font-family: inherit;
  }
  .opcion.activa {
    border-color: rgba(167,224,219,.4); background: rgba(167,224,219,.08);
    color: var(--marca-200);
  }
  .opcion strong { font-family: var(--fuente-titulo); }

  .suma {
    display: inline-flex; align-items: center; gap: 10px; font-size: 12px;
    font-variant-numeric: tabular-nums;
  }
  .enlace {
    padding: 0; cursor: pointer; border: none; background: transparent;
    color: var(--texto-tenue-2); font: inherit; font-size: 11px;
    text-decoration: underline; text-underline-offset: 3px;
  }
  .enlace:hover { color: var(--negativo); }
  .punto { width: 9px; height: 9px; flex: none; border-radius: 3px; }
  .pct {
    width: 62px; min-height: 36px; padding: 4px 8px; text-align: right;
    font: inherit; font-size: 14px; color: var(--texto-fuerte);
    background: var(--superficie); border: 1px solid var(--linea);
    border-radius: 9px;
  }
  .quitar {
    width: 28px; height: 28px; flex: none; cursor: pointer; border-radius: 50%;
    border: 1px solid var(--linea); background: transparent;
    color: var(--texto-tenue); font-size: 14px; line-height: 1;
  }
  .quitar:hover { color: var(--negativo); border-color: var(--negativo); }

  .optimizar {
    width: 100%; min-height: 48px; margin-top: 12px; padding: 13px 18px;
    cursor: pointer; border: none; border-radius: var(--radio-pastilla);
    background: var(--gradiente-acento); color: var(--texto-fuerte);
    font-family: var(--fuente-titulo); font-weight: 600; font-size: 16px;
    letter-spacing: .02em; box-shadow: var(--sombra-md);
  }
  .optimizar:hover:not(:disabled) { filter: brightness(1.12); }
  .optimizar:disabled { opacity: .6; cursor: progress; }
  .nota-optimizar { margin: 8px 0 0; font-size: 11px; color: var(--texto-tenue-2); }

  .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 16px; }
  .stat {
    padding: 12px 14px; border: 1px solid var(--linea);
    border-radius: var(--radio-sub); background: rgba(20,22,58,.5);
  }
  .stat-etiqueta {
    font-size: 10px; letter-spacing: .08em; text-transform: uppercase;
    color: var(--texto-tenue-2);
  }
  .stat-valor { font-size: clamp(20px, 3vw, 30px); }
  .stat-sub { margin-top: 2px; font-size: 11px; }
  .positivo { color: var(--positivo); }

  .reparto { margin-bottom: 16px; }
  .reparto-cabecera {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 10px; margin-bottom: 10px;
  }
  .reparto-titulo { display: inline-flex; align-items: baseline; gap: 8px; min-width: 0; }
  .reparto-sub { font-size: 11px; }
  .reparto-cuerpo { display: grid; gap: 14px; align-items: start; }
  .dona-caja { position: relative; width: 158px; margin: 0 auto; }
  .dona { display: block; width: 100%; height: auto; }
  .dona circle { fill: none; stroke-width: 12; }
  .dona-pista { stroke: var(--linea); }
  .dona-centro {
    position: absolute; inset: 0; display: grid; place-content: center;
    gap: 2px; text-align: center;
  }
  .dona-monto { font-size: 19px; color: var(--texto-fuerte); }
  .reparto-filas { display: grid; gap: 8px; min-width: 0; }
  .fila-instrumento {
    display: grid; gap: 6px; padding: 9px 12px; font-size: 12px;
    border: 1px solid var(--linea); border-radius: var(--radio-sub);
    background: rgba(20,22,58,.5);
  }
  .fila-linea { display: flex; align-items: center; gap: 9px; min-width: 0; }
  .fila-nombre {
    font-family: var(--fuente-titulo); font-weight: 600; font-size: 13px;
    color: var(--texto-fuerte); white-space: nowrap;
  }
  .fila-producto {
    min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
    color: inherit;
  }
  a.fila-producto { text-decoration: underline dotted; text-underline-offset: 2px; }
  a.fila-producto:hover, a.fila-producto:focus-visible { color: var(--marca-200); }
  .fila-peso { display: inline-flex; align-items: center; gap: 4px; margin-left: auto; }
  .fila-datos { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; }
  .fila-tramos { display: flex; flex-wrap: wrap; gap: 5px; }
  @media (min-width: 720px) {
    .reparto-cuerpo { grid-template-columns: 170px 1fr; }
  }

  .cascada { display: grid; gap: 12px; margin-bottom: 16px; }
  .paso-cabecera {
    display: flex; justify-content: space-between; align-items: baseline;
    gap: 8px; margin-bottom: 4px;
  }
  .paso-etiqueta {
    font-family: var(--fuente-titulo); font-weight: 600; font-size: 12px;
    letter-spacing: .04em; text-transform: uppercase;
  }
  .paso-monto { font-size: 18px; }
  .pista {
    position: relative; height: 12px; overflow: hidden;
    border: 1px solid rgba(167,224,219,.1); border-radius: var(--radio-pastilla);
    background: rgba(20,22,58,.7);
  }
  .relleno { position: absolute; top: 0; bottom: 0; border-radius: var(--radio-pastilla); }
  .paso-sub { margin-top: 3px; font-size: 11px; }

  .narrativa {
    padding: 12px 16px; margin: 0;
    border: 1px solid rgba(95,176,201,.3); border-radius: var(--radio-sub);
    background: rgba(95,176,201,.1); font-size: 14px;
  }
  .sin-asignar {
    margin: 10px 0 0; padding: 10px 14px; font-size: 12px;
    border: 1px solid var(--aviso-borde); border-radius: var(--radio-sub);
    background: var(--aviso-fondo); color: var(--aviso);
  }

  /* Compartido con el panel de explicación: su hoja sólo se monta cuando hay
     pasos, y estos chips también aparecen en mezclas manuales. */
  .chip-tramo {
    padding: 1px 8px; border-radius: var(--radio-pastilla); font-size: 11px;
    color: var(--marca-200); background: rgba(167, 224, 219, 0.1);
    white-space: nowrap;
  }
  .cobertura { margin-left: auto; }
  .aviso-liquidez {
    flex-basis: 100%; padding: 4px 10px; border-radius: var(--radio-sub);
    background: var(--aviso-fondo); color: var(--aviso); font-size: 11px;
    line-height: 1.4;
  }

  .avisos { display: grid; gap: 10px; margin-top: 14px; }
  .aviso-caja {
    padding: 12px 16px; border: 1px solid var(--linea);
    border-radius: var(--radio-sub); background: rgba(20,22,58,.4);
  }
  .aviso-titulo {
    margin-bottom: 4px; font-family: var(--fuente-titulo); font-weight: 600;
    font-size: 12px; letter-spacing: .06em; text-transform: uppercase;
    color: var(--marca-200);
  }
  .aviso-texto { margin: 0; font-size: 12px; }
  .disclaimer { margin: 0; font-size: 11px; }

  .error {
    padding: 14px 18px; border: 1px solid var(--negativo-borde);
    border-radius: var(--radio-tarjeta); background: var(--negativo-fondo);
    color: var(--negativo);
  }

  @media (min-width: 720px) {
    .rejilla { grid-template-columns: 360px 1fr; align-items: start; }
  }
  @media (min-width: 1080px) {
    .rejilla { grid-template-columns: 400px 1fr; }
  }
`;
