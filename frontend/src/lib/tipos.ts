/**
 * El contrato de la API, en TypeScript.
 *
 * Espejo de `src/api/schemas.py`. Se escribe a mano en vez de generarse del
 * OpenAPI por una razón concreta: el generador produce un tipo por esquema y
 * nombres como `RespuestaComparador_Output`, y este frontend consume seis
 * endpoints. Un archivo que se lee de un vistazo vale más aquí que uno
 * derivado que nadie abre. Si la API cambia, `astro check` no lo detecta —
 * lo detectan los tests de contrato del backend, que es donde vive la verdad.
 */

export type Severidad = 'AMARILLA' | 'ROJA';

export type EstadoIndicador =
  | 'EN_RANGO'
  | 'ATENCION'
  | 'ALERTA'
  | 'SIN_DATO'
  | 'INFORMATIVO';

export type UnidadIndicador = 'PORCENTAJE' | 'MONEDA' | 'VECES' | 'NIVEL';

export type TipoSeguro = 'SOBERANO' | 'IPAB' | 'PROSOFIPO' | 'NINGUNO';

export type CategoriaInstitucion =
  | 'GOBIERNO'
  | 'BANCO_TRADICIONAL'
  | 'BANCO_DIGITAL'
  | 'SOFIPO'
  | 'IFPE';

export type EstadoTasa = 'VIGENTE' | 'PENDIENTE_REVISION' | 'RECHAZADA';

export interface Procedencia {
  fecha_dato: string;
  fuente: string;
  fuente_url: string | null;
  estado: EstadoTasa;
  /** false = no se pudo confirmar contra la fuente oficial. Se muestra. */
  verificada: boolean;
}

export interface Gat {
  nominal: string;
  real: string;
  origen: 'PUBLICADA' | 'CALCULADA';
  es_calculada: boolean;
}

export interface Cobertura {
  tipo: TipoSeguro;
  limite_udis: string | null;
  /** null = sin límite (deuda soberana). No es "desconocido". */
  limite_mxn: string | null;
  valor_udi: string;
  sin_limite: boolean;
  sin_cobertura: boolean;
}

export interface Bandera {
  tipo: string;
  severidad: Severidad;
  motivo: string;
  periodo_dato: string | null;
  compuesta: boolean;
}

export interface InstitucionResumen {
  id: number;
  nombre: string;
  slug: string;
  categoria: CategoriaInstitucion;
  tipo_seguro: TipoSeguro;
  /** Institución ficticia, sembrada para ilustrar. Se marca con ◆. */
  es_demostracion: boolean;
}

/** Un escalón de una tasa escalonada por saldo: [desde, hasta). */
export interface TramoTasa {
  desde: string;
  /** null = sin techo publicado (infinito). */
  hasta: string | null;
  tasa_nominal: string;
}

export interface FilaComparador {
  institucion: InstitucionResumen;
  producto_id: number;
  producto: string;
  producto_slug: string;
  tipo: 'VISTA' | 'PLAZO';
  instrumento: string;
  plazo_dias: number | null;
  monto_minimo: string;
  liquidez: string;
  /** En un producto escalonado, la tasa del primer tramo — la titular. */
  tasa_nominal: string;
  ten: string;
  gat: Gat;
  cobertura: Cobertura;
  banderas: Bandera[];
  procedencia: Procedencia;
  escalonada: boolean;
  /** Escalera por saldo; vacía = la tasa aplica a todo el saldo. */
  tramos: TramoTasa[];
  /** Ponderada al monto consultado; null sin monto. En filas planas, igual a la titular. */
  tasa_efectiva: string | null;
  ten_efectiva: string | null;
}

export interface RespuestaComparador {
  filas: FilaComparador[];
  total: number;
  inflacion_anual: string;
  valor_udi: string;
  tasa_retencion_capital: string;
  monto_consultado: string | null;
  generado_en: string;
  disclaimer: string;
}

export interface IndicadorEvaluado {
  clave: string;
  etiqueta: string;
  valor: string | null;
  valor_texto: string | null;
  unidad: UnidadIndicador;
  estado: EstadoIndicador;
  descripcion: string;
}

export interface Indicadores {
  periodo: string;
  imor: string | null;
  icap: string | null;
  icor: string | null;
  nicap_nivel: string | null;
  captacion: string | null;
  cartera_total: string | null;
  fuente_url: string | null;
  evaluados: IndicadorEvaluado[];
}

export interface ProductoDetalle {
  id: number;
  nombre: string;
  slug: string;
  tipo: 'VISTA' | 'PLAZO';
  instrumento: string;
  plazo_dias: number | null;
  monto_minimo: string;
  liquidez: string;
  penalizacion_retiro: string | null;
  tasa_nominal: string | null;
  ten: string | null;
  gat: Gat | null;
  procedencia: Procedencia | null;
  escalonada: boolean;
  tramos: TramoTasa[];
}

export interface DetalleInstitucion {
  id: number;
  nombre: string;
  slug: string;
  categoria: CategoriaInstitucion;
  tipo_seguro: TipoSeguro;
  estatus_regulatorio: string | null;
  url_sitio: string | null;
  activa: boolean;
  es_demostracion: boolean;
  notas: string | null;
  cobertura: Cobertura;
  productos: ProductoDetalle[];
  indicadores_ultimo_periodo: Indicadores | null;
  banderas_activas: Bandera[];
  banderas_historicas: Bandera[];
  disclaimer: string;
}

export interface Cascada {
  monto_invertido: string;
  rendimiento_bruto: string;
  isr_retenido: string;
  rendimiento_neto: string;
  efecto_inflacion: string;
  ganancia_real: string;
  plazo_dias: number;
  tasa_nominal: string;
  ten: string;
  inflacion_anual: string;
  nota_fiscal: string;
}

export interface Asignacion {
  institucion: InstitucionResumen;
  producto_id: number;
  producto: string;
  producto_slug: string;
  plazo_dias: number | null;
  porcentaje: string;
  monto: string;
  /** TEN efectiva del monto asignado: en un escalonado, la de la ponderada. */
  ten: string;
  cascada: Cascada;
  escalonada: boolean;
  tramos: TramoTasa[];
  cobertura: Cobertura;
  monto_cubierto: string;
  monto_expuesto: string;
  cubierto: boolean;
  advertencia_liquidez: string | null;
  banderas: Bandera[];
  procedencia: Procedencia;
}

export interface RespuestaCombinacion {
  monto_total: string;
  monto_no_asignado: string;
  horizonte_dias: number;
  ten_ponderada: string;
  rendimiento_bruto: string;
  isr_retenido: string;
  rendimiento_neto: string;
  efecto_inflacion: string;
  ganancia_real: string;
  monto_protegido: string;
  porcentaje_protegido: string;
  asignaciones: Asignacion[];
  narrativa: string;
  nota_fiscal: string;
  inflacion_anual: string;
  valor_udi: string;
  generado_en: string;
  aviso_optimizador: string;
  disclaimer: string;
}

export interface FrescuraFuente {
  fuente: string;
  ultima_actualizacion: string | null;
  dias_desde_actualizacion: number | null;
  /** null = fuente informativa, sin cadencia que vigilar (MANUAL, LLM). */
  sla_dias: number | null;
  dentro_de_sla: boolean;
  observaciones: number;
}

export interface RespuestaFrescura {
  fuentes: FrescuraFuente[];
  ultima_actualizacion: string | null;
  mostrar_tasas_sin_verificar: boolean;
  generado_en: string;
  todo_dentro_de_sla: boolean;
}

/** Los filtros de §7, tal como viajan en la query string. */
export interface FiltrosMercado {
  plazo: string | null;
  seguros: TipoSeguro[];
  categorias: CategoriaInstitucion[];
  monto: string | null;
  orden: 'ten' | 'tasa_nominal' | 'gat' | 'cobertura';
  sinBanderas: boolean;
}

export interface ItemCombinacion {
  producto_id: number;
  porcentaje: string;
}
