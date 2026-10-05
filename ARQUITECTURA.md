# Arquitectura del proyecto, módulo por módulo

Este documento es la explicación detallada de **por qué** el código es como es.
`README.md` tiene el resumen ejecutivo (tabla de primitivas, tabla de defectos y
comandos); aquí está el razonamiento completo de cada módulo.

Los números de línea corresponden al commit `7e99cfc`. Si el código cambia, las
referencias `archivo:línea` se desplazan, pero los nombres de símbolo no.

---

## Índice

1. [La idea que sostiene todo](#1-la-idea-que-sostiene-todo)
2. [Mapa de dependencias](#2-mapa-de-dependencias)
3. [`config.py` — las constantes](#3-configpy--las-constantes)
4. [`estado.py` — entidades y las cinco primitivas](#4-estadopy--entidades-y-las-cinco-primitivas)
5. [`hilos.py` — los seis tipos de hilo](#5-hilospy--los-seis-tipos-de-hilo)
6. [`partida.py` — ciclo de vida, tick y reglas](#6-partidapy--ciclo-de-vida-tick-y-reglas)
7. [`render.py` — la frontera del render](#7-renderpy--la-frontera-del-render)
8. [`telemetria.py` — logging con nombre de hilo](#8-telemetriapy--logging-con-nombre-de-hilo)
9. [`main.py` — el bucle tonto](#9-mainpy--el-bucle-tonto)
10. [`benchmark.py` — la medición defendible](#10-benchmarkpy--la-medicion-defendible)
11. [`tests/` y `conftest.py`](#11-tests-y-conftestpy)
12. [Apéndice: dónde vive cada defecto](#12-apndice--dnde-vive-cada-defecto)

---

## 1. La idea que sostiene todo

Hay una sola regla de diseño, y todo lo demás se deriva de ella:

> **Ninguna regla de juego depende de que haya ventana.**

`main.py` es el único archivo que sabe de pygame. `config`, `estado`, `hilos`,
`partida`, `telemetria` y `benchmark` no lo importan. De ahí salen tres
consecuencias:

- Los 43 tests corren en **12 segundos** sin abrir nada.
- El tick se puede medir sin que el dibujado contamine el resultado.
- Una exclusión mutua se puede demostrar en un test, no solo afirmar en la
  memoria.

La regla se manifiesta en dos fronteras:

| Frontera | Quién pide | Quién no sabe |
|---|---|---|
| Render | `main.py` pide un `Snapshot` y lo dibuja | El juego no sabe que hay pantalla |
| Entrada | `main.py` llama a `partida.tick()` | El juego no sabe que hay hilo principal |

---

## 2. Mapa de dependencias

```
            config.py            (constantes, sin pygame)
               |
        +------+------+
        |             |
    estado.py      main.py -----------> render.py -----> pygame
        |             |                    ^
        |          (lee teclado)          |
        |             |                    |
     hilos.py  <-------+                    |
        |             |                    |
    partida.py --------+                    |
        |             |                    |
        +------+------+                    |
               |                           |
          telemetria.py                    |
                                        (Snapshot)
```

Las flechas hacia `render.py` **nunca** cruzan desde el juego: el juego no
importa `render`. Solo `main.py` los junta.

Regla práctica: si un módulo de la columna izquierda necesita dibujar algo,
es que se ha roto la frontera.

---

## 3. `config.py` — las constantes

173 líneas, cero lógica. Existe por una razón concreta: **cualquier número que
aparece en dos sitios debe estar aquí**, porque los números escritos a mano en
dos sitios divergen.

Fue el defecto **D7** de la versión de partida: el OVNI se dibujaba en
`y=40..60` y la bala colisionaba contra `40 <= bala.y <= 60`, escritos a mano en
sitios distintos. Los dos valores se separaron en algún momento y el OVNI se
volvió imparable. Ahora los dos salen de `OVNI_Y`, `OVNI_H` y `OVNI_W`
(`config.py:123-125`) y la colisión de `hilos.py:231-235`.

### Ritmo

```python
TICK_MS = 180            # latido del juego
TICK_MS_POR_NIVEL = 30   # se acelera 30 ms por ronda
NIVELES_POR_DEFECTO = 3
```

El suelo de 60 ms se aplica en `partida.py:200`:

```python
return max(60, C.TICK_MS - max(0, self.estado.nivel - 1) * C.TICK_MS_POR_NIVEL)
```

### Flota

`INVASOR_TOTAL` no está escrito como 32:

```python
INVASOR_FILAS = 4
INVASOR_COLUMNAS = 8
INVASOR_TOTAL = INVASOR_FILAS * INVASOR_COLUMNAS  # 32 hilos de invasor
```

Eso importa más de lo que parece, porque 32 es también el número de semáforos
de turno de tick. Si la rejilla fuera 5×8, los semáforos se adaptarían solos
(`estado.py:211` itera sobre `INVASOR_TOTAL`).

Las filas de abajo disparan más, como en el arcade:

```python
PROB_DISPARO_POR_FILA = (0.004, 0.008, 0.014, 0.022)
PUNTOS_POR_FILA       = (50, 40, 30, 20)
```

Ambas tuplas están indexadas por fila, así que la fila *n* del juego y el índice
*n* de la tupla son siempre el mismo número.

### Sincronización

```python
TIMEOUT_ESPERA_TICK_S    = 1.0
TIMEOUT_BARRERA_NIVEL_S  = 3.0
TIMEOUT_JOIN_S           = 1.0
```

Y el comentario de `TIMEOUT_ESPERA_TICK_S` declara una decisión de fondo que
conviene citar en la sustentación:

```python
#: Timeout de la espera del principal a los invasores. Es una red de
#: seguridad, NUNCA una mecanica de juego: si se dispara, se registra en el log.
```

En la versión de partida el timeout **era** la mecánica: al agotarse, el
`except BrokenBarrierError: pass` se tragaba el error y la partida seguía con
una flota congelada y sin decir nada. Aquí el timeout solo avisa, y como el
permiso de turno se devuelve en un `finally` (`hilos.py:154-159`), avisar no
desalinea el conteo.

---

## 4. `estado.py` — entidades y las cinco primitivas

### Las entidades

**`Invasor`** (`estado.py:60-73`). Dataclass normal. Cada uno es poseído por
exactamente un hilo, y la propiedad `puntos` traduce la fila a puntuación.

**`Bala` y `Bomba`** (`estado.py:76-97`). Llevan **`@dataclass(eq=False)`**, y es
un bug silencioso que merece explicación:

```python
# `eq=False` es IMPORTANTE: con `@dataclass` a secas se genera `__eq__` por
# campos, y `list.remove(bala)` borraria el primer proyectil *igual en
# coordenadas*, no necesariamente el suyo. Con identidad, `remove` borra
# exactamente el suyo.
```

Dos balas en `(400.0, 300.0)` son *iguales* con `__eq__` por campos. Cuando una
impacta, `list.remove(esa_bala)` borraba la primera gemela, no la suya: la bala
que sigue volando desaparecía de la lista y la que había impactado se quedaba
colgada para siempre. Con `eq=False` la comparación es por identidad y `remove`
borra la correcta.

Lo cubre `test_bala_no_borra_otra_bala_igual` (`tests/test_concurrencia.py:321`).

**`Escudo`** (`estado.py:100-140`). Un bunker modelado como rejilla de
12 × 8 celdas booleanas, inicializada del arte ASCII `ESCUDO_FORMA`
(`config.py:109-118`):

```
    ####
   ######
  ########
  ########
  ########
###      ###
###      ###
###      ###
```

Como un escudo solo pierde material y nunca lo recupera, un `bool` por celda
alcanza: no hace falta un contador de salud.

El atributo `sucio` **no es parte del juego**: es una cache de render que
`render.py` consulta. Volvemos a esto en la sección de `render.py`.

**`Snapshot`** (`estado.py:143-166`). La frontera del render. Un dataclass que
se rellena entero bajo el lock y luego se dibuja sin él.

### La clase `Estado`

`estado.py:172-291` acumula **todos** los datos compartidos y **todas** las
primitivas. El docstring declara la regla de oro del archivo:

```python
# Todo dato mutado por mas de un hilo se protege SIEMPRE con `estado.lock`.
# La unica excepcion son los objetos de `threading` que ya son seguros por si
# mismos (`Lock`, `Event`, `Semaphore`, `Barrier`), que se usan sin lock
# adicional porque son la sincronizacion en si.
```

Es una regla que se puede auditar: cualquier `with self.lock` ausente en un
lugar donde se muta estado compartido es un bug, y se ve leyendo.

### Las cinco primitivas y el problema de cada una

| Objeto | Tipo | Qué problema concreto resuelve |
|---|---|---|
| `estado.lock` | `threading.Lock()` | 36 hilos sobre 32 posiciones, 6 proyectiles y banderas |
| `estado.fin` | `threading.Event()` | Señal de parada que consultan todos los hilos en su bucle |
| `estado.permisos[i]` | 32 × `Semaphore(0)` | Turno de tick, **uno por hilo** |
| `estado.listo_sem` | `Semaphore(0)` | "Ya me moví": 32 avisos de fin de turno |
| `estado.listos_nivel` | `Barrier(33)` | Reunión de la flota al arrancar cada ronda |
| `estado.balas_sem` | `Semaphore(3)` | Cupo de proyectiles del jugador |
| `estado.bombas_libres` | `Semaphore(3)` | Cupo de proyectiles enemigos |
| `estado.disparo_sem` | `Semaphore(0)` | Petición de tiro enemigo |

### Por qué 32 semáforos privados y no uno compartido

Este es el hallazgo técnico central del trabajo, y el comentario de
`estado.py:199-207` lo documenta entero.

La primera corrección del diseño fue un **único** semáforo `tick_sem` con 32
permisos que el principal liberaba de golpe en cada tick. Parecía correcta: 32
permisos, 32 hilos, 32 `acquire()`. El conteo cuadraba.

No lo era. **`threading.Semaphore` no garantiza justicia.** Cuando el principal
suelta 32 permisos de golpe y un hilo ya está despierto, ese hilo puede vaciarlos
todos antes de que el planificador despierte a los otros 31, que se quedan
esperando un permiso que ya no existe.

Medido tras un tick:

```
x = [292, 155, 210, 265, 320, 375, ...]
```

El invasor 0 estaba en 292 y los otros en 155, 210, 265… El invasor 0 se había
movido **32 veces** (100 → 292 = 32 × 6 px, con `INVASOR_STEP = 6`) y los otros
31 **ninguna**.

Lo importante para la sustentación: **el conteo de permisos cuadraba
perfectamente**. Cada hilo consumió un permiso y devolvió un aviso. Cualquier
aserción sobre el semáforo lo daba por bueno. El defecto era invisible a las
comprobaciones obvias, y solo aparecía mirando las posiciones.

**La solución: un semáforo privado por hilo.** `permisos` es una lista de 32
semáforos, y el hilo *i* solo puede tomar el suyo:

```python
# partida.py:228  -- dar el turno
for sem in e.permisos:
    sem.release()

# hilos.py:137    -- tomar SU turno
e.permisos[self.inv.idx].acquire()
```

Con esa estructura el encuentro 1:1 es exacto por construcción, llegue quien
llegue y cuando llegue.

**`listo_sem` sí es compartido, y a propósito.** También justificado:

```python
# `listo_sem` si es compartido, y a proposito: al principal solo le
# interesa un total de 32 avisos, no quien los dara.
```

Repartir los avisos entre 32 semáforos sería tan caro como inútil: al que
espera en `partida.py:242-257` le importa la **cuenta**, no la identidad de
quien llega.

### Por qué la barrera es one-shot

```python
self.listos_nivel = threading.Barrier(C.INVASOR_TOTAL + 1)
```

Se crea en `nuevo_nivel()` (`estado.py:322`), es decir **una instancia nueva por
ronda**, y nunca se le pasa `timeout` a los hilos de juego: solo al principal.

La versión de partida usaba dos `threading.Barrier(33)` **reutilizables**, y ese
es el defecto **D1**:

```python
# `threading.Barrier.wait(timeout=1.0)` NO devuelve False si se agota el
# tiempo: lanza BrokenBarrierError y ademas pone la barrera en estado roto
# IRREVERSIBLE.
```

El `except BrokenBarrierError: pass` se tragaba ese error, y un solo hilo
retrasado más de 1 s mataba la sincronización para el resto de la partida: los
32 hilos se iban por su `return` y el juego seguía dibujando una flota
congelada sin decir nada. Medido en la versión original:

```
tick 1: BARRERA ROTA (tras 1.00s)
invasores vivos: 0 / 32 -> flota congelada para siempre
```

Un semáforo no tiene estado de fallo: si un hilo tarda más, solo hace que el
principal espere más. Por eso los 32 permisos del tick son semáforos, y la
barrera sobrevive solo como reunión de ronda, donde no se le da timeout a
nadie y no puede quedar rota.

---

## 5. `hilos.py` — los seis tipos de hilo

El reparto está en el docstring (`hilos.py:5-17`):

| Hilo | `daemon` | Rol |
|---|---|---|
| `HiloInvasor` × 32 | `False` | Mueven la flota, uno por tick |
| `HiloBala` | `False` | Efímero, uno por proyectil del jugador |
| `HiloBomba` × 3 | `False` | Pool fijo de proyectiles enemigos |
| `HiloOVNI` | `True` | Tarea de servicio, perderla es aceptable |
| `HiloTelemetria` | `True` | Tarea de servicio en segundo plano |
| `HiloDemonioPeligroso` | `True` | Demostración opt-in de un demonio incorrecto |

### `HiloSeguro` — la base que impide morir en silencio

`hilos.py:40-82`. Tres decisiones que vale la pena explicar.

**a) `daemon` es un atributo de clase, no un argumento del constructor.**

```python
class HiloSeguro(threading.Thread):
    daemon = False
```

Cada subclase lo sobrescribe en su cuerpo (`hilos.py:391`, `:431`, `:462`) y la
base lo pasa a `super().__init__` (`hilos.py:53`). Así el contraste
demonio / no-demonio es **visible leyendo la clase**, sin tener que ir a buscar
el `start()`.

**b) Ningún hilo muere callado** (`hilos.py:59-66`):

```python
def run(self) -> None:
    try:
        self.cuerpo()
    except threading.BrokenBarrierError:
        log.debug("[%s] barrera rota durante el apagado", self.name)
    except Exception:
        log.exception("[%s] excepcion inesperada; se detiene la partida", self.name)
        self.estado.fin.set()
```

En la versión de partida, un hilo de invasor que moría por una excepción
dejaba la barrera reutilizable sin sus 33 partes, y el juego se congelaba **sin
mostrar ningún error**. Aquí una muerte es visible en el log y orderly: marca
`fin` y el apagado procede.

**c) `_dormir()` duerme en rebanadas** (`hilos.py:69-82`):

```python
limite = time.monotonic() + segundos
while True:
    restante = limite - time.monotonic()
    if restante <= 0:
        return False
    if self.estado.fin.is_set():
        return True
    time.sleep(min(0.05, restante))
```

Arregla el defecto **D9**: el OVNI de la versión de partida hacía
`time.sleep(20)`, así que el apagado tardaba hasta 20 segundos en notarse.
Aquí cualquier espera se reposa cada 50 ms y comprueba la señal de parada.

### `HiloInvasor` — el corazón del diseño

`hilos.py:88-175`. El ciclo de una iteración está numerado tanto en el docstring
como en los comentarios:

```python
# --- 1. Barrera de arranque de nivel (una vez por ronda) -------
# --- 2. Permiso de tick ----------------------------------------
# --- 3. Movimiento bajo el lock ---------------------------
# --- 4. Aviso de "ya me movi" ---------------------------
```

#### El hilo nunca muere durante la partida

```python
# El hilo NO muere nunca durante la partida. Un invasor ya muerto sigue
# existiendo: consume su permiso y no hace nada. Esa es la clave de que la
# sincronizacion sea a prueba de hilos caidos, porque el conteo de permisos
# siempre cuadra.
```

Un invasor muerto sigue despertando, toma su permiso, ve que no está vivo, no
hace nada y devuelve su aviso. El conteo de 32 siempre cuadra.

Eso elimina de raíz **toda la familia de fallos por hilos caídos**. La
alternativa —que un hilo muerto salga del bucle y deje de participar— obliga a
que el principal detecte la muerte y rebaje el contador esperado, que es
exactamente el tipo de estado repartido que produce los errores de la versión de
partida.

Lo cubre `test_el_estado_no_se_desincroniza_si_muere_un_hilo`
(`tests/test_concurrencia.py:251`).

#### El aviso va en un `finally`

```python
finally:
    # En `finally` para que el permiso se devuelva SIEMPRE. Si un
    # hilo devolviera el permiso solo en el camino feliz, una
    # excepcion desalinearia el conteo para toda la partida.
    e.listo_sem.release()
```

Esa es la razón por la que el timeout de 1 s del principal puede avisar sin
miedo. Si el permiso se devolviera solo en el camino feliz, una excepción en un
hilo desalinearía el conteo de forma permanente, y el log de avisos que verías
sería: *tick 47: el hilo 20 no respondió*.

#### Detalles finos

**Un RNG por hilo** (`hilos.py:110`):

```python
self.rng = Random((estado.seed * 1_000_003) ^ (idx * 7_919))
```

Cada hilo tiene su propio generador derivado de la semilla global. Así
`--seed 42` da una partida reproducible **y** los hilos no compiten por el
`Random` compartido del estado (que es solo del hilo principal).

**El retraso de estrés va fuera del lock** (`hilos.py:141-144`):

```python
if self.retraso_ms:
    # Fuera del lock a proposito: simula que el sistema se
    # congela. Debe ser inocuo para la sincronizacion.
    time.sleep(self.retraso_ms / 1000.0)
```

Si el retraso estuviera bajo el lock, simularía un sistema lento en el
hilo, no una pausa del SO. La prueba de estrés debe golpear el mutex.

### `HiloBala` — el cupo se reserva antes de crear el hilo

`hilos.py:181-267`. El cupo no lo toma este hilo: lo reserva `Partida.disparar()`
con `blocking=False` **antes** de crearlo (`partida.py:173`).

El comentario cuantifica el defecto **D3** de la versión de partida:

```python
# El cupo de `balas_sem` lo reserva `Partida.disparar()` con
# `blocking=False` ANTES de crear este hilo. Por eso ningun hilo queda
# esperando turno en el semaforo, que era el defecto D3: en la version
# de partida, 20 pulsaciones de espacio creaban 20 hilos y 17 se quedaban en
# cola disparando en rafaga.
```

Con la versión de partida, el `acquire()` era bloqueante **dentro** del hilo, así
que el cupo no limitaba nada: los hilos se acumulaban en la cola del semáforo y
disparaban en ráfaga cuando los anteriores morían.

**Propiedad de los recursos en las colisiones** (`hilos.py:247-251`):

```python
# La bala destruye la bomba, pero NO devuelve `bombas_libres`:
# el permiso es del hilo de la bomba, que lo devuelve en su
# `finally`. Devolverlo aqui duplicaria el cupo.
del e.bombas[i]
```

Un cupo tiene exactamente un dueño. Si la bala y la bomba devolveran las dos
`bombas_libres`, el contador subiría de 3 y el cupo de bombas quedaría roto
para el resto de la partida: la flota podría disparar seis bombas con un cupo
de tres.

### `HiloBomba` — el pool, y el token fantasma

`hilos.py:273-377`. Es el diseño menos obvio de los tres, porque son **3 hilos
fijos** que esperan peticiones, no un hilo por bomba.

El bucle espera bloqueante, que aquí es lo correcto:

```python
# Espera una peticion de disparo. Este `acquire()` es bloqueante a
# proposito: el hilo duerme aqui, sin consumir CPU, hasta que un
# invasor le reserve una plaza.
e.disparo_sem.acquire()
```

La plaza ya la reservó el invasor con `bombas_libres.acquire(blocking=False)`
(`hilos.py:173`), así que nunca hay más de `MAX_BOMBAS` bombas vivas ni se
acumulan tokens.

**El token fantasma** (`hilos.py:302-315`) es un bug que encontré midiendo, y es
buen ejemplo de por qué los timeouts de apagado hay que hacerlos bien:

```python
# Se saca la peticion ANTES de mirar `fin`, porque un token sin
# peticion es un token fantasma: lo publico el apagado al soltar
# permisos para despertar a este hilo, y no tiene plaza reservada
# que devolver. Si se soltara `bombas_libres` al ver `fin`, el
# contador del semaforo pasaria de MAX_BOMBAS y el cupo quedaria
# roto para el resto de la partida.
with e.lock:
    x = e.peticiones.popleft() if e.peticiones else None

if x is None:
    # Token fantasma. Si ademas estamos apagando, se sale limpio.
    if e.fin.is_set():
        return
    continue  # defensa ante una desincronizacion improbable
```

El apagado suelta tokens en `disparo_sem` (`partida.py:140-141`) para despertar
a los hilos del pool, porque un `acquire()` bloqueante no se despierta con la
señal `fin`. Pero esos tokens **no tienen plaza reservada detrás**: si el hilo
devolviera `bombas_libres` sin comprobar, el contador del semáforo pasaría de
`MAX_BOMBAS`.

Sacar la petición de la cola *antes* de mirar `fin` es lo que permite
distinguir un token real de uno fantasma.

### Los tres demonios

**`HiloOVNI`** (`hilos.py:383-420`). Demonio *obediente*. Espera entre 12 y 20 s
(`OVNI_MIN_MS` / `OVNI_MAX_MS`), cruza la pantalla y desaparece. Si el programa
termina mientras está en pantalla, no importa: se pierde el OVNI y ya.

**`HiloTelemetria`** (`hilos.py:423-444`). Demonio *bien portado*, y el matiz
que más se agradece en una sustentación está en su docstring:

```python
# Comprueba `fin` y sale con education. Aun asi sigue siendo demonio, y esa
# es la leccion: un demonio puede ser obediente y aun asi no se le espera con
# `join`, porque el programa puede morir antes de que termine su ultimo volcado.
```

Que un demonio.exit limpio **no** significa que se pueda hacer `join` con él. El
proceso puede morir entre su último volcado y su `return`, y con `join` el
proceso esperaría a un hilo que no tiene nada útil que aportar.

**`HiloDemonioPeligroso`** (`hilos.py:447-476`). Tres errores deliberados,
numerados en el propio código:

| # | Error | Dónde |
|---|---|---|
| 1 | Ignora `estado.fin`: nunca consulta la señal de parada | `hilos.py:471` |
| 2 | Toma el `Lock` compartido mientras el intérprete se desmonta | `hilos.py:473` |
| 3 | Escribe en el logging cuando los handlers pueden estar cerrados | `hilos.py:476` |

```python
while True:  # <-- nunca consulta e.fin: ese es el error nº1
    n += 1
    with e.lock:  # <-- error nº2: lock compartido en pleno apagado
        cont = e.contadores.copia()
    time.sleep(self.periodo_s)
    log.warning("DEMONIO-PELIGROSO vuelta %d (ignora 'fin'): %s", n, cont)
```

Está detrás de `--demo-daemon-peligroso` y apagado por defecto. La conclusión
que documenta:

```python
# Un hilo demonio no puede colgar el proceso principal, pero si puede dejar
# un hilo a medio escribir, ensuciar la salida o lanzar excepciones durante
# el apagado.
```

Esa es la asimetría real de `daemon=True`: no es "inofensivo", es
"no bloquea la salida, pero puede ensuciarla".

---

## 6. `partida.py` — ciclo de vida, tick y reglas

Módulo sin pygame, a propósito: todo el juego se puede ejecutar y probar sin
ventana. Su docstring (`partida.py:7-18`) es un índice de dónde vive cada
primitiva, que viene muy bien si alguien pregunta.

### `arrancar()` — `partida.py:75-110`

Dos bloques visualmente separados:

```python
# --- Hilos no demonio: se garantiza su terminacion con join ---
for i in range(C.INVASOR_TOTAL):
    h = HiloInvasor(e, i, retraso_ms=self.retraso_invasor_ms)
    h.start()
    e.hilos.append(h)          # <-- se registran para poder hacer join

# --- Hilos demonio: tareas de servicio, el proceso no los espera ---
ovni = HiloOVNI(e)
ovni.start()
```

La separación es el argumento, no un detalle de estilo: solo los no-demonio
entran en `e.hilos`, y `e.hilos` es exactamente la lista sobre la que se hace
`join`.

### `tick()` — `partida.py:202-268`

El corazón. Tres fases:

**Fase 1 — dar el turno, un permiso por hilo** (`partida.py:228-229`):

```python
for sem in e.permisos:
    sem.release()
```

**Fase 2 — la barrera de ronda** (`partida.py:231-239`), y aquí está el **orden
obligatorio**, fuente de otro bug que encontré antes de corregirlo:

```python
# El orden importa: los permisos se sueltan ANTES de esperar en la
# barrera de nivel, porque los hilos solo llegan a ella DESPUES de
# consumir su turno. Al reves, el principal esperaria 3 s a 32 hilos
# que estan bloqueados en su `acquire()`, y la barrera se romperia.
```

Los hilos llegan a `listos_nivel` en su **siguiente** iteración del bucle
(`hilos.py:119-133`), es decir después de haber consumido su turno. Si el
principal esperara en la barrera antes de soltar los permisos, los 32 hilos
seguirían bloqueados en `permisos[i].acquire()`, la barrera agotaría su timeout
de 3 s, se rompería, y cada cambio de ronda costaría 3 segundos de congelación.

**Fase 3 — esperar los 32 avisos** (`partida.py:241-257`):

```python
for i in range(C.INVASOR_TOTAL):
    if e.fin.is_set():
        break
    if not e.listo_sem.acquire(timeout=C.TIMEOUT_ESPERA_TICK_S):
        log.warning("tick %d: el hilo %02d no respondio en %.1fs", ...)
```

El `if e.fin.is_set(): break` del principio es lo que permite apagar a mitad de
tick sin esperar el timeout entero 32 veces. Y el timeout produce un
`log.warning`, no una excepción: es una red de seguridad que **registra**, nunca
una mecánica.

Después, las reglas y el reloj (`partida.py:259-268`):

```python
self._decidir_giro()
self._revisar_estado()

with e.lock:
    e.contadores.ticks += 1
    transcurrido = int((time.monotonic() - t0) * 1000)
    if transcurrido > e.contadores.tiempo_tick_max_ms:
        e.contadores.tiempo_tick_max_ms = transcurrido
```

`tiempo_tick_max_ms` es la métrica que permite ver un problema de
sincronización en el log de una partida normal: en el log del resumen aparece
`tick_max=2ms`. Si un día sube a 400 ms, hay un bug de sincronización que
investigar.

### `apagar()` — `partida.py:112-157`

El apagado ordenado, y **el orden también es obligatorio**:

```python
e.fin.set()                      # 1. la señal de parada
for sem in e.permisos:          # 2. despertar a quien está en un acquire()
    sem.release()
for _ in range(C.MAX_BOMBAS):
    e.disparo_sem.release()
e.listos_nivel.abort()          # 3. liberar la barrera
for h in e.hilos:                # 4. join con timeout
    h.join(timeout=C.TIMEOUT_JOIN_S)
```

**El paso 2 es el que se olvida.** Un semáforo no se despierta solo con la señal
`fin`: `acquire()` es un bloqueo y necesita un `release()` para salir. Sin el
paso 2, los 32 hilos de invasor y los 3 del pool quedarían bloqueados para
siempre, y el proceso no podría salir aunque todos fueran no-demonio.

**El paso 3 es igual de estructural, aunque el docstring de `apagar()` lo
mencione menos.** Los tres hilos del pool están bloqueados en
`disparo_sem.acquire()` (`hilos.py:300`). Medido: quitando ese `release()` de
una copia del apagado,

```
apagando (sin disparo_sem.release())...
  [VIVO tras join] bomba-0
  [VIVO tras join] bomba-1
  [VIVO tras join] bomba-2
El proceso NO puede salir: son no-demonio.
```

el proceso no termina y hay que matarlo a la fuerza. Es el mismo razonamiento
que justifica el paso 2, y conviene que el docstring lo diga, porque es la
línea que alguien borraría sin querer.

**El paso 4, en cambio, es defensa y no estructura.** Se comprobó quitándolo
también: el apagado sale limpio igual, porque cada invasor comprueba
`if e.fin.is_set(): return` **antes** de entrar en `barrera.wait()`
(`hilos.py:125-126`), así que nunca llega a bloquear la barrera. Se deja por
precaución, pero no hay que defenderlo como imprescindible.

**El paso 3** es `Barrier.abort()`, que es lo equivalente para la barrera: la
libera y marca el estado.

**El paso 4 verifica en vez de asumir** (`partida.py:145-156`):

```python
for h in e.hilos:
    h.join(timeout=C.TIMEOUT_JOIN_S)
    if h.is_alive():
        supervivientes.append(h.name)
        log.error("[%s] no termino en %.1fs", h.name, C.TIMEOUT_JOIN_S)
...
log.info("los %d hilos no-demonio terminated correctamente", len(e.hilos))
```

Por eso en el log ves `los 35 hilos no-demonio terminaron correctamente`. Un
`join` que no comprueba el resultado no demuestra nada: es una línea que no falla
nunca.

### `disparar()` — `partida.py:162-186`

Dos cosas además de la reserva del cupo.

**El cupo antes del hilo** (`partida.py:173`):

```python
if not e.balas_sem.acquire(blocking=False):
    e.contadores.disparos_rechazados += 1
    return False
```

`blocking=False` es lo que arregla D3. Si no hay cupo, la pulsación **se
descarta** (y se cuenta en `disparos_rechazados`), no se encola.

**La poda de hilos efímeros** (`partida.py:182-185`):

```python
if len(e.hilos) > PODA_HILOS:
    e.hilos = [t for t in e.hilos if t.is_alive()]
```

`HiloBala` es efímero, así que sin esta poda `e.hilos` crecería sin límite en
una partida larga, y `apagar()` intentaría hacer `join` de miles de hilos ya
muertos.

### `_decidir_giro()` — `partida.py:273-296`

```python
# Los invasores NO se auto-dirigen: cada uno lee estos dos valores ya
# calculados, bajo el lock. Concentrar aqui la decision evita la
# condicion de carrera clasica de que 32 hilos escriban `direccion` a la
# vez, y es la razon de que la decision se tome una sola vez por tick.
```

La decisión se toma **una vez**, en el hilo principal, y los 32 hilos solo la
leen. La alternativa —que cada invasor compruebe el borde y escriba `direccion`
y `bajar`— sería 32 escritores compitiendo por los mismos dos campos, y el
resultado dependería del orden de ejecución.

Aquí está también la corrección del defecto **D5**: `bajar` se recalcula en
cada tick, y la rama `else` lo pone a `False`. En la versión de partida se fijaba
a `True` al primer borde y nunca volvía a `False`, así que tras el primer giro la
flota bajaba en cada tick.

### `_revisar_estado()` — `partida.py:298-323`

```python
# Importante: al superar una ronda NO se activa `fin`. Los hilos siguen
# vivos y se reunen en la barrera nueva del siguiente nivel. Activar `fin`
# aqui los mataria y habria que relanzar 32 hilos en cada ronda.
```

Superar una ronda es el único evento del juego que **no** apaga nada. Solo
activan `fin` la victoria (`partida.py:315`), la derrota (`hilos.py:375`) y la
invasión (`partida.py:322`).

La consecuencia es que los 32 hilos sobreviven a la partida entera: cada ronda
cruza una barrera nueva y nada más.

### Las propiedades con lock

`partida.py:346-357`. `terminada` y `entre_rondas` se leen bajo el lock, con un
comentario que explica por qué se paga ese coste:

```python
# Aunque hoy solo el hilo principal lee y escribe estos tres campos,
# se leen bajo el lock: el coste es un acquire por frame y la garantia
# de que nadie accedera al estado compartido sin el lock.
```

Es una decisión de legibilidad sobre rendimiento: un `acquire` por frame es
irrelevante comparado con el dibujado, y a cambio la regla "todo estado
compartido pasa por el lock" deja de tener excepciones que recordar.

---

## 7. `render.py` — la frontera del render

`render.py:1-11` declara la regla:

```python
# REGLA: el render NUNCA dibuja mientras otro hilo escribe. El flujo es siempre
#
#     snapshot = tomar_snapshot(estado)   # una pasada rapida BAJO el lock
#     dibujar(screen, snapshot)           # todo el dibujado FUERA del lock
```

En la versión de partida, las ~30 líneas de dibujado estaban **dentro** de
`with estado.lock`. Cada frame congelaba a los 36 hilos durante el dibujado.
Ese es el defecto **D4**.

### `tomar_snapshot()` — `render.py:159-181`

La única sección crítica del render, y dura microsegundos **porque no dibuja
nada**: solo copia números a listas de tuplas.

```python
with estado.lock:
    return Snapshot(
        jugador_x=estado.jugador_x,
        invaders=[(i.x, i.y, i.fila) for i in estado.invasores if i.vivo],
        escudos=estado.escudos,  # por referencia: el render no los muta
        contadores=estado.contadores.copia(),
        ...
    )
```

`invaders` son 32 tuplas de tres flotantes, no 32 objetos `Invasor`. Eso hace
que la copia sea barata y, además, snapshot por construcción.

Los escudos se pasan **por referencia**, y el comentario dice por qué es
correcto: el render solo lee `celdas` y `x`/`y`, y `Escudo` tiene `__slots__`
para que no pueda añadirse nada por accidente.

### Las dos optimizaciones de rendimiento

**Sprites pre-renderizados** (`render.py:21-27`):

```python
#: Sprites: se dibujan una vez a superficies y luego solo se blitean.
#: Dibujar 32 invasores celda a celda cada frame son ~2800 draw.rect por frame;
#: blitear 32 superficies son 32 llamadas.
```

Los invasores se construyen a partir de siluetas ASCII escaladas
(`render.py:99-116`), cacheadas por fila. Hay tres formas distintas y la fila 3
reutiliza la de la fila 2:

```python
arte = _INVASOR_ART.get(min(fila, 2), _INVASOR_ART[2])
```

**Caché de escudos** (`render.py:134-153`). Aquí entra el atributo `sucio` de
`estado.py`. Redibujar 4 escudos de 96 celdas cada frame serían 384 `draw.rect`;
con la caché son 4 `blit`.

```python
clave = id(escudo)
sup = _FONDO_ESCUDOS.get(clave)
if sup is not None and not escudo.sucio:
    return sup
```

El ciclo de `sucio` es corto y en las dos direcciones:

- `Escudo.danar()` pone `sucio = True` (`estado.py:139`) al destruir una celda.
- `_fondo_escudo()` lo pone a `False` (`render.py:152`) tras reconstruir.

Y `nuevo_nivel()` los invalida todos al rehacer las celdas
(`estado.py:314`).

`id(escudo)` como clave merece una nota: es válido porque el `Estado` mantiene
vivos a los escudos durante toda la partida, así que las direcciones no se
reutilizan mientras la caché exista.

### `superposicion()` — `render.py:241-289`

Dibuja los avisos grandes (victoria, derrota, invasión, ronda superada, pausa).
Lee **todo del snapshot**, nunca del estado vivo. `main.py` decide si llamarla
usando también los campos del snapshot.

---

## 8. `telemetria.py` — logging con nombre de hilo

72 líneas, pero una decisión que importa más de lo que parece:

```python
LOG_FORMAT = "[%(levelname)-7s] %(threadName)-18s : %(message)s"
```

El `%(threadName)s` es lo que hace útil el logging cuando hay hilos: permite
ver qué hilo produjo cada línea. Por eso el log del test de estrés se lee así:

```
[WARNING] MainThread         : tick 20: el hilo 20 no respondio en 1.0s
[WARNING] demonio-peligroso  : DEMONIO-PELIGROSO vuelta 4 (ignora 'fin'): {...}
```

Se sabe de inmediato que el primero lo escribió el principal y el segundo el
demonio. Sin `threadName`, esas dos líneas serían indistinguibles.

Es el mismo formato que el cuaderno del curso (celda 46), por si comparan.

`resumen()` y `informe_ticks()` leen bajo el lock y devuelven cadenas ya
formateadas, así que el llamador imprime sin tocar el estado compartido.

`configurar()` es idempotente: quita los handlers anteriores antes de añadir
los nuevos, así que se puede llamar varias veces sin duplicar salida.

---

## 9. `main.py` — el bucle tonto

Deliberadamente tonto, como dice su docstring: lee el teclado, pide ticks a
`Partida` y dibuja el `Snapshot`. Ninguna regla de juego vive ahí, y nada de lo
que hay ahí bloquea a los hilos.

### El driver SDL se fija antes de importar pygame

```python
# El driver SDL se fija antes de importar pygame para poder correr sin ventana.
if "--headless" in sys.argv:
    import os
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402  (import tardio a proposito)
```

El orden importa: las variables de entorno tienen que existir antes de que SDL
se cargue.

### No se inicializa el mixer

```python
pygame.display.init()
pygame.font.init()
```

en vez de `pygame.init()`. `render.py` solo usa `display` y `font`, así que el
mixer es innecesario. Y no es solo un valor: en una máquina sin tarjeta de sonido,
`pygame.init()` hace que ALSA entre en un bucle de reintentos que bloquea el
arranque. Este juego no tiene audio.

### El estado de la superposición viene del snapshot

```python
s = render.tomar_snapshot(partida.estado)
render.dibujar(pantalla, s)
# El estado de la superposicion se lee del snapshot, no del estado
# vivo: asi el render no necesita el lock y no hay dos lecturas
# distintas del mismo frame.
if s.nivel_superado or s.resultado != "jugando" or s.pausado:
    render.superposicion(pantalla, s)
```

Si se leyera `partida.entre_rondas or partida.terminada or partida.estado.pausa`
habría dos lecturas distintas del mismo frame, y una superposición podría
aparecer un frame antes o después de la pausa.

### La pausa pasa por un método

```python
elif evento.key == pygame.K_p and not partida.terminada:
    log.info("pausa=%s", partida.alternar_pausa())
```

Y no `partida.estado.pausa = not partida.estado.pausa`, para que ningún escritor
del juego tenga que acordarse de tomar el lock.

### El `flip` está fuera de `dibujar`

```python
# El flip va despues de la superposicion, no dentro de `dibujar`.
pygame.display.flip()
```

Como `superposicion()` dibuja después de `dibujar()`, el flip tiene que ser
lo último.

### El disparo es por pulsación

```python
elif evento.key == pygame.K_SPACE and not partida.terminada:
    # Disparo por pulsacion, como en el arcade. El cupo lo
    # impone el semaforo `balas_sem`; si esta lleno, la
    # pulsacion se descarta (no se encola).
    partida.disparar()
```

`KEYDOWN`, no "disparar cada frame mientras se mantenga pulsado". Una pulsación
= una bala, y el cupo lo impone el semáforo.

---

## 10. `benchmark.py` — la medición defendible

Mide **tres magnitudes por separado**, y eso es lo que hace el argumento
defendible en vez de un número confuso.

```python
T_trabajo   lo que cuesta hacer el trabajo en un solo hilo, sin sincronización
T_semaforo  lo que cuesta UNA ida y vuelta de un threading.Semaphore
T_n         lo que cuesta el mismo trabajo repartido en N hilos, con el ciclo
            de sincronización real del juego
```

### Las tres funciones de medición

**`medir_trabajo(ticks)`** — un solo hilo, los 32 movimientos por tick, sin
sincronizar. Es la línea base real, y el `sum()` final evita que el optimizador
se borne el trabajo.

**`medir_semaforo(operaciones)`** — mide el intercambio con dos hilos
enfrentados:

```python
# El intercambio es `principal --a.release()--> worker` y
# `worker --b.release()--> principal`, de modo que una ida y vuelta completa
# mide exactamente un `release` + un `acquire` cruzando dos hilos.
```

Es exactamente el patrón del juego en cada tick, sin el trabajo de mover
rectángulos.

**`medir_hilos(ticks, n_hilos)`** — replica los **32 semáforos privados** del
juego:

```python
permisos = [threading.Semaphore(0) for _ in range(n_hilos)]
...
for sem in permisos:  # un permiso privado por hilo, como en el juego
    sem.release()
```

Si usara un semáforo compartido, mediría un defecto en vez del diseño real.

### Los resultados

```
magnitudes de referencia
  T_trabajo  (1 hilo, sin sincronizar)      0.200 ms   =     0.67 us/tick
  T_semaforo (1 ida y vuelta)               62.909 us   =     94.5x el trabajo de un tick

 hilos      tiempo     ticks/s     S(n)  coste rel.   us/tick
     1    18.72 ms      16,029    0.011       93.7x      62.4
     2    37.46 ms       8,009    0.005      187.5x     124.9
     4    77.31 ms       3,881    0.003      387.0x     257.7
     8   144.71 ms       2,073    0.001      724.4x     482.4
    16   280.64 ms       1,069    0.000     1298.2x     935.5
    32   554.11 ms         541    0.000     2563.3x    1847.0
    64  1082.77 ms         277    0.000     5008.9x    3609.2
```

**Una sola ida y vuelta de semáforo cuesta 94 veces más que los 32 movimientos
completos de un tick.** Esa es la frase que resume todo el hallazgo.

`S(n) = T_trabajo / T_n` está muy por debajo de 1 y **empeora** al aumentar *n*,
con `coste rel.` creciendo linealmente.

### Por qué esto no es un error del código

Dos hechos, y ninguno depende de cómo esté escrito el programa:

**1. El trabajo por tick es diminuto.** Son unas pocas operaciones de punto
flotante sobre un atributo: 0,67 µs para los 32 invasores. Hablar de "cientos
de nanosegundos" es generoso.

**2. El GIL ya serializa el bytecode de Python.** Los N hilos no se ejecutan en
paralelo en la CPU, solo se reparten el tiempo. Añadir hilos no añade potencia de
cálculo, solo coordinación.

Por eso la conclusión defendible es precisamente esa: la aceleración real es
`S(n) ≤ 1` y el coste crece linealmente con *n*. La arquitectura con hilos está
justificada por el **modelo de dominio** —un hilo por invasor es lo que pide el
enunciado—, no por el rendimiento.

El `benchmark.py` también avisa de que los valores absolutos dependen de la
máquina y de la carga, y de que lo que no depende de la máquina es el orden de
magnitud.

---

## 11. `tests/` y `conftest.py`

### `conftest.py` (17 líneas)

Dos cosas: mete la raíz del proyecto en `sys.path` para que `import config`
funcione al ejecutar pytest, y fija el driver SDL a `dummy`.

```python
RAIZ = pathlib.Path(__file__).parent.resolve()
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
```

### Las tres utilidades de los tests

```python
def nueva_partida(**kw) -> Partida:
    kw.setdefault("telemetria", False)      # no lanza el demonio de telemetría
    return Partida(seed=7, niveles=kw.pop("niveles", 99), **kw)
```

`niveles=99` por defecto para que ningún test gane por accidente. `seed=7` fijo
para que sean deterministas.

```python
def correr_ticks(p: Partida, n: int) -> None:
    """Fuerza n ticks ignorando el reloj (para tests rapidos)."""
    for _ in range(n):
        if p.terminada or p.entre_rondas:
            return
        p.ultimo_tick = 0.0
        assert p.tick_pendiente(), "el tick deberia estar pendiente"
        p.tick()
```

`p.ultimo_tick = 0.0` fuerza el reloj. Eso es lo que permite hacer 170 ticks en
milisegundos en lugar de 170 × 180 ms = 30 segundos.

### Las 12 secciones

| § | Tests | Qué demuestra |
|---|---|---|
| 1 | 6 | El encuentro 1:1: permisos a cero tras cada tick, los 32 se mueven, cada hilo recibe el suyo, y funciona con llegadas muy desordenadas |
| 2 | 3 | **D1 y D2**: un hilo retrasado o muerto no rompe la sincronización |
| 3 | 4 | **D3** el cupo de balas, `eq=False`, **D6** reloj monotónico |
| 4 | 3 | El cupo del pool de bombas nunca se excede ni se rompe |
| 5 | 3 | **D7**: las colisiones del OVNI salen de constantes |
| 6 | 4 | **D5**: giro en ambos bordes, un solo descenso, no salir de pantalla |
| 7 | 4 | Apagado < 2 s, idempotente, durante un tick, y el contraste demonio / no-demonio |
| 8 | 3 | La barrera se recrea en cada ronda y no se reutiliza |
| 9 | 6 | Victoria, invasión, pausa, aceleración por ronda |
| 10 | 3 | Escudos: bloquean, se degradan, las balas los destruyen |
| 11 | 2 | La puntuación depende de la fila |
| 12 | 2 | Coherencia del estado con proyectiles vivos; los hilos de bala se podan |

Dos tests que merecen mención aparte:

**`test_el_rendezvous_es_exacto_durante_10_ticks`**
(`tests/test_concurrencia.py:115`). Comprueba que dentro de cada fila **todos los
invasores avanzan lo mismo**, no que sus posiciones sean iguales:

```python
delta = [x[base + c] - x_inicial[base + c] for c in range(C.INVASOR_COLUMNAS)]
assert len(set(delta)) == 1, f"la fila {f} se movio de forma desigual: {delta}"
```

Esa es exactamente la firma del bug del semáforo compartido: un único invasor
moviéndose el doble o el triple que sus compañeros de fila.

**`test_el_reloj_usa_marcas_monotonas`**
(`tests/test_concurrencia.py:337`). No prueba el comportamiento en ejecución,
sino que **el código fuente** no contiene `time.time()`:

```python
# D6: la version de partida usaba `time.time()`, que salta si se ajusta el
# reloj del sistema. La partida debe usar `time.monotonic()`.
```

Es un test estático sobre el código, y es válido porque un reloj que salta
produciría ticks negativos o esperas de horas, que son fallos difíciles de
reproducir en una prueba.

---

## 12. Apéndice: dónde vive cada defecto

Para cuando alguien pregunte "¿y esto qué lo arregla?":

| # | Defecto | Dónde se arregla | Qué lo fija |
|---|---|---|---|
| D1 | Barrera reutilizable rota de forma irreversible | `estado.py:211` (permisos en vez de barrera), `estado.py:322` (barrera nueva por ronda) | §1, §2 |
| D2 | Carrera de salida entre las dos barreras | `hilos.py:154-159` (`finally`), `hilos.py:100-103` (los hilos no mueren) | §2 |
| D3 | `acquire()` bloqueante + un hilo por pulsación | `partida.py:173` (`blocking=False` antes de crear el hilo) | §3 |
| D4 | ~30 líneas de dibujado dentro del lock | `render.py:159-181` (`Snapshot`) | §7 |
| D5 | `bajar` nunca se reseteaba | `partida.py:296` (rama `else`) | §6 |
| D6 | `time.time()` para medir ticks | `time.monotonic()` en `partida.py`, `hilos.py` | §3 |
| D7 | Colisiones del OVNI escritas a mano | `config.py:123-125` + `hilos.py:231-235` | §5 |
| D8 | Sin escudos, vidas, puntuación ni reinicio | `estado.py:100-138`, `partida.py:298-323` | §10, §11 |
| D9 | `time.sleep(20)` en el OVNI | `hilos.py:69-82` (`_dormir` en rebanadas) | §5 |
| D10 | Todos los hilos demonio | `hilos.py:50` (`daemon` por clase), `partida.py:146` (`join` real) | §5, §6 |
| — | Un semáforo compartido no garantiza justicia | `estado.py:211` (32 permisos privados) | §1 |
| — | Token fantasma al apagar el pool de bombas | `hilos.py:308-315` (sacar la petición antes de mirar `fin`) | §5 |
| — | Orden tick/barrera invertido | `partida.py:228-239` (permisos antes de la barrera) | §6 |