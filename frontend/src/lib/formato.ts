/**
 * Formato de cifras. Se ejecuta en servidor y en cliente.
 *
 * La API manda `Decimal` serializado como cadena —"6.1800", "219797.18"— y no
 * como número, a propósito: convertir a `number` en Python o en JSON perdería
 * precisión en cuanto los importes crecen. Aquí sí se convierte, pero sólo
 * para **mostrar**: ningún cálculo ocurre en el navegador.
 */

const MONEDA = new Intl.NumberFormat('es-MX', {
  style: 'currency',
  currency: 'MXN',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const MONEDA_CORTA = new Intl.NumberFormat('es-MX', {
  style: 'currency',
  currency: 'MXN',
  maximumFractionDigits: 0,
});

const MILES = new Intl.NumberFormat('es-MX');

export function dinero(valor: string | number | null | undefined): string {
  if (valor === null || valor === undefined) return '—';
  return MONEDA.format(Number(valor));
}

export function dineroCorto(valor: string | number | null | undefined): string {
  if (valor === null || valor === undefined) return '—';
  return MONEDA_CORTA.format(Number(valor));
}

/** Para cifras grandes en poco espacio: "$3.52 M", "$220 mil". */
export function dineroCompacto(valor: string | number | null | undefined): string {
  if (valor === null || valor === undefined) return 'Sin límite';
  const n = Number(valor);
  if (n === 0) return '$0';
  if (Math.abs(n) >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)} M`;
  if (Math.abs(n) >= 1_000) return `$${Math.round(n / 1_000)} mil`;
  return MONEDA_CORTA.format(n);
}

export function porcentaje(valor: string | number | null | undefined, decimales = 2): string {
  if (valor === null || valor === undefined) return '—';
  return `${Number(valor).toFixed(decimales)} %`;
}

/**
 * "$30 mil", "$1 M": los cortes de una escalera en poco espacio. Distinto de
 * `dineroCompacto` a propósito: aquí no hay caso null — un techo ausente
 * significa «en adelante», no «Sin límite», y eso lo dice `tramoEtiqueta`.
 */
export function montoCorto(valor: string | number): string {
  const n = Number(valor);
  if (Math.abs(n) >= 1_000_000) {
    const millones = n / 1_000_000;
    return `$${Number.isInteger(millones) ? millones : millones.toFixed(2)} M`;
  }
  if (Math.abs(n) >= 1_000) return `$${Math.round(n / 1_000)} mil`;
  return MONEDA_CORTA.format(n);
}

/** "hasta $30 mil" | "en adelante", para pintar cada tramo de una escalera. */
export function tramoEtiqueta(hasta: string | null): string {
  return hasta === null ? 'en adelante' : `hasta ${montoCorto(hasta)}`;
}

/**
 * Un tramo como texto de chip: «15.00 % hasta $25 mil», «7.00 % $25 mil–$1 M»,
 * «6.30 % en adelante». El tramo base y el abierto se explican con
 * `tramoEtiqueta`; el intermedio acotado necesita sus dos fronteras — «hasta
 * $1 M» a secas mentiría sobre dónde empieza.
 */
export function etiquetaChipTramo(tramo: {
  desde: string;
  hasta: string | null;
  tasa_nominal: string;
}): string {
  const tasa = porcentaje(tramo.tasa_nominal);
  if (Number(tramo.desde) === 0 || tramo.hasta === null) {
    return `${tasa} ${tramoEtiqueta(tramo.hasta)}`;
  }
  return `${tasa} ${montoCorto(tramo.desde)}–${montoCorto(tramo.hasta)}`;
}

export function miles(valor: string | number | null | undefined): string {
  if (valor === null || valor === undefined || valor === '') return '';
  return MILES.format(Number(valor));
}

/** Deja sólo dígitos. El campo de monto se teclea con separadores. */
export function soloDigitos(texto: string): string {
  return texto.replace(/\D/g, '');
}

const MESES = [
  'ene',
  'feb',
  'mar',
  'abr',
  'may',
  'jun',
  'jul',
  'ago',
  'sep',
  'oct',
  'nov',
  'dic',
];

/**
 * "21 jul 2026". Se construye a mano en vez de con `toLocaleDateString`
 * porque la API manda fechas ISO sin hora y `new Date("2026-07-21")` las
 * interpreta en UTC: en México eso se muestra como el día anterior.
 */
export function fecha(iso: string | null | undefined): string {
  if (!iso) return '—';
  const [anio, mes, dia] = iso.slice(0, 10).split('-').map(Number);
  if (!anio || !mes || !dia) return iso;
  return `${dia} ${MESES[mes - 1]} ${anio}`;
}

/** "may 2026", para los periodos de la CNBV. */
export function periodo(iso: string | null | undefined): string {
  if (!iso) return '—';
  const [anio, mes] = iso.slice(0, 10).split('-').map(Number);
  if (!anio || !mes) return iso;
  return `${MESES[mes - 1]} ${anio}`;
}

export function plazoCorto(plazoDias: number | null, tipo: string): string {
  if (tipo === 'VISTA' || plazoDias === null) return 'Vista';
  return `${plazoDias} d`;
}

export function plazoLargo(plazoDias: number | null, tipo: string): string {
  if (tipo === 'VISTA' || plazoDias === null) return 'A la vista';
  return `${plazoDias} días`;
}

/** Iniciales para el avatar: "Nu México" → "NU", "Mercado Pago" → "MP". */
export function iniciales(nombre: string): string {
  const palabras = nombre.trim().split(/\s+/);
  if (palabras.length === 1) return palabras[0].slice(0, 2).toUpperCase();
  return (palabras[0][0] + palabras[1][0]).toUpperCase();
}

const TINTES = [
  'var(--serie-2)',
  'var(--serie-1)',
  'var(--serie-3)',
  'var(--serie-6)',
  'var(--serie-5)',
  'var(--serie-7)',
];

/**
 * Color estable del avatar. Es una función del nombre, no del índice de la
 * fila: así una institución conserva su tinte al cambiar de orden o de
 * filtro, y el ojo la reconoce entre pantallas.
 */
export function tinte(clave: string): string {
  let acumulado = 0;
  for (const caracter of clave) {
    acumulado = (acumulado * 31 + caracter.charCodeAt(0)) % TINTES.length;
  }
  return TINTES[acumulado];
}

export const COLORES_SERIE = [
  'var(--serie-1)',
  'var(--serie-2)',
  'var(--serie-3)',
  'var(--serie-4)',
  'var(--serie-5)',
  'var(--serie-6)',
  'var(--serie-7)',
  'var(--serie-8)',
];

export function colorSerie(indice: number): string {
  return COLORES_SERIE[indice % COLORES_SERIE.length];
}

/**
 * El ciclo de color de los chips de condiciones — y de los segmentos de la
 * barra de escalera: mismo índice, mismo tono, para que el chip de un tramo
 * y su segmento se reconozcan como la misma cosa.
 *
 * Cuatro tonos fríos de la paleta de series. Fuera quedan `--serie-8`
 * (idéntica a `--aviso`: un chip de condición se leería como advertencia),
 * `--serie-2` (idéntica a `--positivo`: se leería como «verificada»),
 * `--serie-5` (casi blanca, el tinte de fondo desaparece) y `--serie-4`
 * (demasiado oscura como color de texto a 11 px).
 */
export const COLORES_CHIP = [
  'var(--serie-1)',
  'var(--serie-3)',
  'var(--serie-7)',
  'var(--serie-6)',
];

export function colorChip(indice: number): string {
  return COLORES_CHIP[indice % COLORES_CHIP.length];
}
