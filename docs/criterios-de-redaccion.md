# Criterios de redacción

> Cierre de la decisión **D5** del [plan](../plan-de-implementacion/00-overview.md). Aplica a todo texto que llegue a un usuario: banderas, disclaimers, copy de la interfaz, respuestas de la API y cualquier página futura.

## El principio

Que la gente tenga acceso a información con la que entienda **lo importante que es elegir dónde ahorra**. Pueden tomarla como referencia, pero siempre deben investigar por su cuenta.

De ahí salen los cinco criterios de abajo. Ninguno es una preferencia estética: cada uno existe porque la alternativa convierte un comparador en algo que no es.

---

## Los cinco criterios

### 1. Se describe, no se aconseja

Quedan fuera del vocabulario: **«conviene»**, **«deberías»**, **«te recomendamos»**, **«la mejor opción»**, **«la más segura»**, **«ideal para ti»**.

Lo que sí se puede decir: qué mide un número, cómo se calculó, de cuándo es y contra qué se compara. La diferencia entre «Finsus paga 8.69 %, la mediana a 364 días es 7.20 %» y «te conviene Finsus» es la diferencia entre informar y asesorar.

El orden de una tabla no es un consejo mientras el criterio de orden esté a la vista y lo controle quien lee.

### 2. Una bandera es una señal, nunca un dictamen

Las banderas salen de datos públicos de la CNBV con uno a tres meses de rezago. Eso es una señal sobre un indicador en un periodo, no un juicio de solvencia ni una predicción.

En la práctica:

- Cada bandera lleva **su número, su umbral y el periodo del dato**. Sin periodo no se publica.
- Se describe **qué mide el indicador**, no qué le pasa a la institución. «Una morosidad en ese rango presiona liquidez y capital» sí; «la institución tiene problemas» no — eso último afirma como hecho algo deducido de un cociente.
- Los verbos de inferencia van hedgeados: *puede indicar*, *suele asociarse a*, *es un patrón que*. Nunca *significa que* ni *es*.
- La prosa **no puede hornear la interpretación de un umbral configurable**. Los umbrales viven en el ConfigStore y se mueven; una frase como «ni la mitad» deja de ser cierta en cuanto alguien cambia el valor y nadie se entera.
- Sin dato no hay bandera. La ausencia de bandera no es un aval.

### 3. Ningún número sin fecha y sin fuente

Toda tasa, todo indicador y toda métrica derivada muestran **de cuándo son** y **de dónde salieron**, con enlace a la fuente cuando existe. Un dato sin procedencia no se publica, ni siquiera marcado.

Las tasas que no se han podido confirmar contra la página de la propia institución se marcan **«sin verificar»** junto al número. Durante la transición del lanzamiento sí salen al sitio público, etiquetadas (`mostrar_tasas_sin_verificar`), hasta que la lectura oficial de cada producto las sustituye — la promesa operativa es **nada sin etiquetar**, no nada sin verificar: lo único que se afirma sin matiz es lo confirmado en fuente primaria. Todo lo que sale lleva enlace a la página de donde se leyó, para que quien quiera comprobarlo lo haga en un clic. Ése es el mecanismo por el que «investiga por tu cuenta» deja de ser una frase.

La GAT calculada por nosotros se marca `(equiv.)` y nunca se presenta como la GAT publicada por la institución.

### 4. El optimizador se presenta siempre con su criterio a la vista

El botón que propone cómo repartir un monto es lo que más se acerca a una recomendación, así que es donde más explícito hay que ser. Toda respuesta —de la interfaz **y de la API**— lleva el criterio completo: *ordena por tasa efectiva neta y llena cada institución hasta su límite de seguro de depósito*; y la advertencia de que es una heurística informativa que no considera situación fiscal, liquidez ni tolerancia al riesgo.

Un reparto sin su criterio a la vista es una recomendación disfrazada de cálculo.

### 5. Se dice cómo se sostiene el proyecto

No hay patrocinio, no hay orden pagado, no se cobra a las instituciones listadas y no se intermedia ninguna contratación. Mientras eso sea cierto, se dice. El día que deje de serlo, se dice también — y se reabre D5 (ver abajo).

### 6. El número sale de quien lo ofrece o de quien lo regula

La institución en su propia página, la autoridad (SIE de Banxico, CNBV) o el emisor. **Un agregador no es fuente publicable**: otro comparador, la prensa financiera, un foro. No es un impedimento legal — es que republicar lo que recopiló otro comparador deja al sitio sin fuente propia. No podría responder de un número que nadie de aquí leyó en su origen, no controla cuándo se actualiza, y hereda en silencio los errores ajenos.

El uso legítimo de un agregador es de **contraste**: guardado con `FuenteTasa.AGREGADOR`, su valor es el término contra el que se mide la primera lectura oficial. La invariante «AGREGADOR nunca VIGENTE» se hace cumplir al escribir, en [cli/tasas.py](../src/cli/tasas.py), y no filtrando al leer — así no depende de que cada consulta futura se acuerde de excluirlo.

Corolario para el copy: la frase del pie «cada tasa muestra su fecha de actualización y su fuente» sólo es cierta si la fuente es la de origen. Enlazar a un agregador la volvería falsa.

---

## Auditoría del copy vigente (2026-07-27)

Se revisó todo el texto que hoy ve un usuario contra los cinco criterios.

**Cumple:**

| Dónde | Qué |
|---|---|
| `DISCLAIMER` en [schemas.py](../src/api/schemas.py) | Viaja en **cada** respuesta de la API, no sólo en la interfaz |
| `AVISO_OPTIMIZADOR` en [schemas.py](../src/api/schemas.py) | Criterio explícito + heurística, en la respuesta y no en la plantilla |
| Pie de página y «Qué NO es Centinela» en [metodologia.astro](../frontend/src/pages/metodologia.astro) | Qué no es, cómo se sostiene, que las banderas son señales |
| `evaluar_red_flag_tasa` en [flags.py](../src/metrics/flags.py) | «puede indicar necesidad de liquidez» — hedgeado, con la mediana a la vista |
| `evaluar_sin_cobertura` | Distingue fideicomiso segregado de seguro de depósitos en vez de simplificar |
| Marcas ◆ y «sin verificar» en [MarcaDato.astro](../frontend/src/components/MarcaDato.astro) | Dos cosas distintas señaladas distinto: institución ilustrativa ≠ tasa sin confirmar |

**No cumplía, y se corrigió en este mismo commit:**

| Dónde | Texto anterior | Por qué |
|---|---|---|
| `evaluar_imor`, bandera roja | «La institución **tiene** problemas para cobrar lo que prestó» | Afirma como hecho un estado de la institución deducido de un solo cociente. Criterio 2 |
| `evaluar_cobertura_cartera`, bandera roja | «No tiene reservas para cubrir ni **la mitad larga** de lo que ya está vencido» | Hornea en prosa la lectura de un umbral configurable: si `umbral_cobertura_roja` se mueve, la frase miente. Criterio 2 |
| `evaluar_no_recomendable` | «**Es** el patrón de una institución que capta para cubrir deudas previas» | Atribuye intención. El docstring de la propia función era más prudente que el texto que veía el usuario. Criterio 2 |

Nota interna: el tipo de bandera se llama `NO_RECOMENDABLE` en el enum. Es un identificador de código y no aparece en la interfaz —lo que se muestra es el motivo y la severidad—, pero el nombre es un juicio y conviene no filtrarlo nunca a una respuesta legible.

---

## Alcance de este cierre

D5 se cierra **sin revisión legal profesional**, como aceptación de riesgo documentada del operador. Es defendible mientras las cinco condiciones siguientes se mantengan, y sólo mientras se mantengan:

1. El sitio es **gratuito** y no muestra publicidad.
2. **No se cobra** a las instituciones listadas ni existe orden patrocinado.
3. **No se intermedia** ninguna contratación ni se capta dinero de nadie.
4. **No hay recomendación personalizada**: nada que tome datos del usuario para decirle qué hacer con su dinero.
5. Los instrumentos listados son depósitos e instrumentos de deuda gubernamental, **no valores bursátiles**.

**Disparadores de reapertura.** Cualquiera de estos obliga a revisión profesional antes de publicar el cambio:

- Monetización de cualquier tipo: afiliados, publicidad, pago de instituciones, suscripción.
- Cualquier función que dé una recomendación personalizada o pida datos personales del usuario.
- Alta de un instrumento que sea un valor bursátil.
- Uso de logotipos de las instituciones, más allá del nombre y la denominación.
- Cualquier redacción que atribuya insolvencia, quiebra o mala fe a una institución identificada.
- Publicar un dato cuya procedencia sea un agregador. Además de romper el criterio 6, redistribuir la recopilación de un tercero es un terreno distinto del de citar a la fuente original.

Esto **no es asesoría legal** y no pretende serlo. Es el criterio con el que se escribe y la lista de condiciones bajo las que ese criterio deja de bastar.
