/**
 * La barra "Tu selección" y la píldora flotante de móvil.
 *
 * Es la única isla del mercado. Existe porque tiene que redibujarse cada vez
 * que cambia el store, y eso es exactamente para lo que sirve un componente
 * reactivo — a diferencia de los botones "+", que sólo alternan un valor y no
 * necesitan React para hacerlo.
 *
 * «Calcular →» es el único camino que arma la combinación. El enlace de la
 * navegación lleva a la calculadora sin tocar la selección, tal como pide el
 * diseño.
 */

import { useStore } from '@nanostores/react';
import { limpiar, quitar, seleccion } from '~/lib/seleccion';

export default function BarraSeleccion() {
  const instrumentos = useStore(seleccion);

  if (instrumentos.length === 0) return null;

  return (
    <>
      <div className="barra">
        <span className="etiqueta">Tu selección</span>

        {instrumentos.map((instrumento) => (
          <button
            key={instrumento.productoId}
            type="button"
            className="chip"
            title="Quitar de la selección"
            onClick={() => quitar(instrumento.productoId)}
          >
            {instrumento.institucion} · {instrumento.producto}
            <span aria-hidden="true">×</span>
            <span className="solo-lectores">Quitar</span>
          </button>
        ))}

        <button type="button" className="limpiar" onClick={limpiar}>
          Limpiar todo
        </button>

        <a className="calcular" href="/calculadora">
          Calcular →
        </a>
      </div>

      <div className="flotante">
        <div className="flotante-caja">
          <span className="flotante-conteo">
            {instrumentos.length} {instrumentos.length === 1 ? 'seleccionado' : 'seleccionados'}
          </span>
          <button type="button" className="flotante-limpiar" onClick={limpiar}>
            Limpiar
          </button>
          <a className="flotante-calcular" href="/calculadora">
            Calcular →
          </a>
        </div>
      </div>

      <style>{`
        .barra {
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 8px;
          margin: 0 0 14px;
          padding: 10px 14px;
          border: 1px solid var(--acento-borde);
          border-radius: 14px;
          background: var(--acento-fondo);
        }
        .chip {
          display: inline-flex;
          align-items: center;
          gap: 7px;
          padding: 5px 12px;
          cursor: pointer;
          border: 1px solid rgba(167, 224, 219, 0.35);
          border-radius: var(--radio-pastilla);
          background: rgba(20, 22, 58, 0.5);
          color: var(--texto);
          font: inherit;
          font-size: 12px;
        }
        .chip:hover { border-color: var(--negativo); color: var(--negativo); }
        .limpiar {
          padding: 5px 12px;
          cursor: pointer;
          border: none;
          background: transparent;
          color: var(--texto-tenue-2);
          font: inherit;
          font-size: 12px;
          text-decoration: underline;
          text-underline-offset: 3px;
        }
        .limpiar:hover { color: var(--negativo); }
        .calcular {
          margin-left: auto;
          padding: 7px 16px;
          border-radius: var(--radio-pastilla);
          background: var(--gradiente-acento);
          color: var(--texto-fuerte);
          font-family: var(--fuente-titulo);
          font-weight: 600;
          font-size: 13px;
          text-decoration: none;
        }
        .calcular:hover { filter: brightness(1.12); }

        .flotante {
          position: fixed;
          left: 14px;
          right: 14px;
          bottom: 78px;
          z-index: 70;
        }
        .flotante-caja {
          display: flex;
          align-items: center;
          gap: 8px;
          padding: 8px 8px 8px 18px;
          border: 1px solid rgba(167, 224, 219, 0.3);
          border-radius: var(--radio-pastilla);
          background: linear-gradient(90deg, #3e6d9c, #2a2f63);
          box-shadow: var(--sombra-lg);
          color: var(--texto-fuerte);
          font-family: var(--fuente-titulo);
          font-weight: 600;
          font-size: 14px;
        }
        .flotante-conteo { flex: 1; }
        .flotante-limpiar {
          padding: 8px 14px;
          cursor: pointer;
          border: 1px solid rgba(227, 246, 245, 0.3);
          border-radius: var(--radio-pastilla);
          background: transparent;
          color: rgba(227, 246, 245, 0.8);
          font: inherit;
          font-size: 12px;
        }
        .flotante-limpiar:hover { color: var(--negativo); border-color: var(--negativo); }
        .flotante-calcular {
          padding: 9px 16px;
          border-radius: var(--radio-pastilla);
          background: rgba(227, 246, 245, 0.95);
          color: #2a2f63;
          font-family: var(--fuente-titulo);
          font-weight: 600;
          font-size: 13px;
          text-decoration: none;
        }

        /* En escritorio manda la barra; la píldora es el recurso de móvil,
           donde la barra queda fuera de pantalla al desplazarse. */
        @media (min-width: 720px) {
          .flotante { display: none; }
        }
      `}</style>
    </>
  );
}
