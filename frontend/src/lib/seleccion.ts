/**
 * La selección de instrumentos, compartida entre páginas e islas.
 *
 * Vive fuera del estado de los filtros a propósito: el diseño exige que
 * sobreviva a cambiar de plazo, de categoría o de página. Los filtros van en
 * la query string —son parte de la URL, que debe ser compartible— y la
 * selección en `localStorage`, porque no lo es: nadie quiere mandar un enlace
 * y que el otro reciba su cartera.
 *
 * Se ejecuta sólo en el navegador. El servidor no la conoce, y por eso el
 * mercado renderiza igual sin JavaScript: lo que se pierde sin él es poder
 * armar una combinación, no ver las tasas.
 */

import { persistentAtom } from '@nanostores/persistent';

export interface Instrumento {
  productoId: number;
  institucion: string;
  producto: string;
  slug: string;
}

export const seleccion = persistentAtom<Instrumento[]>('centinela:seleccion', [], {
  encode: JSON.stringify,
  decode: (crudo) => {
    try {
      const valor: unknown = JSON.parse(crudo);
      // Defensivo a propósito: `localStorage` es editable por el usuario y
      // sobrevive a despliegues. Un valor de una versión anterior no debe
      // dejar la calculadora rota sin forma de recuperarse.
      return Array.isArray(valor) ? (valor as Instrumento[]) : [];
    } catch {
      return [];
    }
  },
});

export function alternar(instrumento: Instrumento): void {
  const actual = seleccion.get();
  const existe = actual.some((i) => i.productoId === instrumento.productoId);
  seleccion.set(
    existe ? actual.filter((i) => i.productoId !== instrumento.productoId) : [...actual, instrumento],
  );
}

export function quitar(productoId: number): void {
  seleccion.set(seleccion.get().filter((i) => i.productoId !== productoId));
}

export function limpiar(): void {
  seleccion.set([]);
}

export function estaSeleccionado(productoId: number): boolean {
  return seleccion.get().some((i) => i.productoId === productoId);
}

/** Reparto inicial a partes iguales, con un decimal y sumando 100. */
export function repartoInicial(cuantos: number): string[] {
  if (cuantos === 0) return [];
  const cada = Math.round((100 / cuantos) * 10) / 10;
  const pesos = Array.from({ length: cuantos }, () => cada);
  const residuo = Math.round((100 - pesos.reduce((a, b) => a + b, 0)) * 10) / 10;
  pesos[0] = Math.round((pesos[0] + residuo) * 10) / 10;
  return pesos.map((p) => p.toFixed(1));
}
