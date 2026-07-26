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
import { colorSerie, dinero, miles, porcentaje, soloDigitos } from '~/lib/formato';
import { limpiar, quitar, repartoInicial, seleccion } from '~/lib/seleccion';
import type { RespuestaCombinacion } from '~/lib/tipos';

const HORIZONTES = [28, 91, 182, 364];
const MONTOS = [
  [50_000, '$50k'],
  [250_000, '$250k'],
  [1_000_000, '$1 M'],
  [5_000_000, '$5 M'],
] as const;

export default function Combinador() {
  const instrumentos = useStore(seleccion);

  const [montoTexto, setMontoTexto] = useState('250,000');
  const [horizonte, setHorizonte] = useState(91);
  const [respetarSeguro, setRespetarSeguro] = useState(true);
  const [excluirRojas, setExcluirRojas] = useState(true);
  const [pesos, setPesos] = useState<Record<number, string>>({});

  const [resultado, setResultado] = useState<RespuestaCombinacion | null>(null);
  const [cargando, setCargando] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const monto = soloDigitos(montoTexto);

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

  const calcular = useCallback(async () => {
    if (instrumentos.length === 0 || !monto || Number(monto) <= 0) {
      setResultado(null);
      return;
    }
    setCargando(true);
    setError(null);
    try {
      const respuesta = await fetch('/api/combinacion', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          monto_total: monto,
          horizonte_dias: horizonte,
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
  }, [instrumentos, monto, horizonte, pesos]);

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
          horizonte_dias: horizonte,
          respetar_seguro: respetarSeguro,
          excluir_rojas: excluirRojas,
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
    } catch (e) {
      setError(e instanceof Error ? e.message : 'No se pudo optimizar');
    } finally {
      setCargando(false);
    }
  }

  const sumaOk = Math.abs(sumaPesos - 100) < 1;
  const real = resultado ? Number(resultado.ganancia_real) : 0;
  const protegido = resultado ? Number(resultado.porcentaje_protegido) : 0;

  return (
    <div className="rejilla">
      {/* ── Entrada ── */}
      <div className="panel">
        <div className="campo">
          <label htmlFor="c-monto">Monto total (MXN)</label>
          <input
            id="c-monto"
            className="control monto"
            type="text"
            inputMode="numeric"
            value={montoTexto}
            onChange={(e) => setMontoTexto(miles(soloDigitos(e.target.value)))}
          />
          <div className="chips">
            {MONTOS.map(([valor, etiqueta]) => (
              <button
                key={valor}
                type="button"
                className="chip"
                onClick={() => setMontoTexto(miles(String(valor)))}
              >
                {etiqueta}
              </button>
            ))}
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

        <div className="instrumentos">
          <div className="instrumentos-cabecera">
            <span className="etiqueta">Instrumentos ({instrumentos.length})</span>
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

          {instrumentos.length === 0 && (
            <p className="vacio">
              Aún no hay instrumentos. Agrégalos desde el <a href="/">mercado</a> o pulsa{' '}
              <strong>Optimizar</strong> para una propuesta automática.
            </p>
          )}

          <div className="lista">
            {instrumentos.map((instrumento, indice) => (
              <div key={instrumento.productoId} className="item">
                <i className="punto" style={{ background: colorSerie(indice) }} aria-hidden="true" />
                <div className="item-texto">
                  <div className="item-nombre">{instrumento.institucion}</div>
                  <div className="item-sub">{instrumento.producto}</div>
                </div>
                <input
                  className="pct"
                  type="number"
                  min="0"
                  max="100"
                  step="0.1"
                  aria-label={`Porcentaje de ${instrumento.institucion}`}
                  value={pesos[instrumento.productoId] ?? ''}
                  onChange={(e) =>
                    setPesos((previos) => ({
                      ...previos,
                      [instrumento.productoId]: e.target.value,
                    }))
                  }
                />
                <span className="tenue-2">%</span>
                <button
                  type="button"
                  className="quitar"
                  aria-label={`Quitar ${instrumento.institucion}`}
                  onClick={() => quitar(instrumento.productoId)}
                >
                  ×
                </button>
              </div>
            ))}
          </div>

          <button type="button" className="optimizar" onClick={optimizar} disabled={cargando}>
            ⚡ Optimizar combinación
          </button>
          <p className="nota-optimizar">
            Optimizar <strong>reemplaza tu selección actual</strong>: ordena por TEN y llena cada
            institución hasta su límite de cobertura
            {respetarSeguro
              ? '.'
              : ' — con los límites desactivados, todo el monto puede ir a un solo emisor.'}
          </p>
        </div>
      </div>

      {/* ── Resultados ── */}
      <div>
        {error && <p className="error">{error}</p>}

        {!resultado && !error && (
          <div className="panel espera">
            <p className="tenue">
              Elige un monto y al menos un instrumento para ver el desglose, o pulsa Optimizar.
            </p>
          </div>
        )}

        {resultado && (
          <>
            <div className={`panel ${cargando ? 'calculando' : ''}`}>
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
                </div>
              </div>

              {/* Barra de asignación */}
              <div className="asignacion">
                <div className="barra">
                  {resultado.asignaciones.map((a, indice) => (
                    <div
                      key={a.producto_id}
                      title={`${a.institucion.nombre} ${a.porcentaje}%`}
                      style={{ width: `${a.porcentaje}%`, background: colorSerie(indice) }}
                    />
                  ))}
                </div>
                <div className="leyenda">
                  {resultado.asignaciones.map((a, indice) => (
                    <span key={a.producto_id} className="leyenda-item">
                      <i style={{ background: colorSerie(indice) }} aria-hidden="true" />
                      {a.institucion.nombre} {Number(a.porcentaje).toFixed(1)}%
                    </span>
                  ))}
                </div>
              </div>

              <Cascada datos={resultado} />

              <p className="narrativa">{resultado.narrativa}</p>

              {Number(resultado.monto_no_asignado) > 0 && (
                <p className="sin-asignar">
                  {dinero(resultado.monto_no_asignado)} no se pudieron colocar sin exceder algún
                  límite de seguro. Repartirlos entre los instrumentos ya llenos anularía la
                  protección que pediste respetar.
                </p>
              )}

              <div className="detalle">
                <div className="etiqueta">
                  Detalle por instrumento · {resultado.horizonte_dias} días
                </div>
                {resultado.asignaciones.map((a, indice) => (
                  <div key={a.producto_id} className="detalle-fila">
                    <i className="punto" style={{ background: colorSerie(indice) }} aria-hidden="true" />
                    <span className="detalle-nombre">{a.institucion.nombre}</span>
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
                    {a.advertencia_liquidez && (
                      <span className="aviso-liquidez" title={a.advertencia_liquidez}>
                        vence después del horizonte
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </div>

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
      </div>

      <style>{ESTILOS}</style>
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
      sub: `Retención ${porcentaje(datos.asignaciones[0]?.cascada.ten ? Number(datos.asignaciones[0].cascada.tasa_nominal) - Number(datos.asignaciones[0].cascada.ten) : 0)} anual sobre el capital`,
    },
    {
      etiqueta: 'Efecto inflación',
      monto: `−${dinero(datos.efecto_inflacion)}`,
      izquierda: desde(Math.max(0, bruto - isr - inflacion)),
      ancho: ancho(inflacion),
      color: '#8fa0c9',
      texto: '#b9cff0',
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

  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .chip {
    padding: 4px 12px; cursor: pointer; font-size: 12px;
    border: 1px solid var(--linea); border-radius: var(--radio-pastilla);
    background: transparent; color: var(--texto-tenue);
  }
  .chip:hover { border-color: var(--linea-fuerte); color: var(--texto-fuerte); }

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

  .instrumentos { padding-top: 14px; border-top: 1px solid var(--linea); }
  .instrumentos-cabecera {
    display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 10px;
  }
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
  .vacio { margin: 4px 0 8px; font-size: 13px; color: var(--texto-tenue-2); }

  .lista { display: grid; gap: 8px; }
  .item {
    display: flex; align-items: center; gap: 9px; padding: 9px 11px;
    border: 1px solid var(--linea); border-radius: var(--radio-sub);
    background: rgba(20,22,58,.5);
  }
  .punto { width: 9px; height: 9px; flex: none; border-radius: 3px; }
  .item-texto { flex: 1; min-width: 0; }
  .item-nombre {
    font-family: var(--fuente-titulo); font-weight: 600; font-size: 14px;
    color: var(--texto-fuerte); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .item-sub { font-size: 11px; color: var(--texto-tenue-2); }
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
  .positivo { color: var(--positivo); }

  .asignacion { margin-bottom: 16px; }
  .barra {
    display: flex; height: 22px; overflow: hidden;
    border: 1px solid var(--linea); border-radius: var(--radio-pastilla);
  }
  .leyenda { display: flex; flex-wrap: wrap; gap: 8px 14px; margin-top: 8px; }
  .leyenda-item {
    display: inline-flex; align-items: center; gap: 6px; font-size: 11px;
    color: var(--texto-tenue);
  }
  .leyenda-item i { width: 8px; height: 8px; border-radius: 3px; }

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

  .detalle { margin-top: 16px; display: grid; gap: 6px; }
  .detalle-fila {
    display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px;
    padding: 9px 12px; font-size: 12px;
    border: 1px solid var(--linea); border-radius: var(--radio-sub);
    background: rgba(20,22,58,.5);
  }
  .detalle-nombre {
    min-width: 130px; font-family: var(--fuente-titulo); font-weight: 600;
    font-size: 13px; color: var(--texto-fuerte);
  }
  .cobertura { margin-left: auto; }
  .aviso-liquidez {
    padding: 1px 8px; border-radius: var(--radio-pastilla);
    background: var(--aviso-fondo); color: var(--aviso); font-size: 11px;
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
