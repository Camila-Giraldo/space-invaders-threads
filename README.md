# Space Invaders concurrente

Juego de arcade en Python con `pygame`, reescrito desde cero para que la
concurrencia sea **correcta y demostrable**: cada invocador de `threading`
tiene un problema concreto que resuelve, y cada defecto de la versión anterior
tiene un test que lo fija.

- **32 hilos de invocador** (uno por invasor, de larga vida)
- **3 hilos de pool de bombas** enemigas
- **1 hilo efímero por proyectil del jugador** (con cupo de 3)
- **2 hilos demonio** de servicio: OVNI y telemetría
- **1 hilo demonio incorrecto**, opt-in, para demostrar por que hay que tener cuidado

---

## Instalación

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Se usa `pygame-ce` porque publica ruedas para CPython 3.14, que `pygame`
oficial todavía no cubre.

## Cómo jugar

```bash
.venv/bin/python main.py
```

| Tecla        | Acción                        |
|--------------|-------------------------------|
| ← →          | mover la nave                 |
| ESPACIO      | disparar (cupo de 3 balas)    |
| P            | pausa                         |
| R            | reiniciar tras el fin de partida |
| ESC          | salir                         |

### Opciones

```bash
python main.py --seed 42                  # partida reproducible
python main.py --niveles 5                # 5 rondas para ganar
python main.py --debug                    # log a nivel DEBUG, con fecha
python main.py --log partida.log          # vuelca el log a un fichero
python main.py --retraso-invasor 1400     # test de estrés de sincronización
python main.py --demo-daemon-peligroso    # demostración opt-in
python main.py --headless --frames 200    # sin ventana, para CI
python benchmark.py --grafico acel.png    # gráfico de aceleración
```

---

## Las cinco primitivas y dónde están

| Primitiva         | Dónde                                        | Qué problema resuelve |
|-------------------|----------------------------------------------|----------------------|
| `threading.Lock`  | `estado.lock` (`estado.py`)                  | Datos compartidos por 36 hilos. Secciones críticas de microsegundos. |
| `threading.Event` | `estado.fin` (`estado.py`)                   | Señal de parada cooperativa para todos los hilos. |
| `threading.Semaphore` | `permisos[i]`, `listo_sem`, `balas_sem`, `bombas_libres`, `disparo_sem` | Turno de tick y cupos de recursos. |
| `threading.Barrier` | `listos_nivel` (`estado.py`)               | Reunión de la flota al arrancar cada ronda. |
| `Thread.join`     | `Partida.apagar()` (`partida.py`)            | Cierre ordenado: garantiza que los 35 hilos no-demonio terminan. |
| `daemon=True`     | `HiloOVNI`, `HiloTelemetria`, `HiloDemonioPeligroso` | Tareas de servicio que no justifican bloquear la salida. |

### Un solo `Lock`, y por qué

Hay exactamente un mutex. Los datos compartidos son pocos (32_position,
3 + 3 proyectiles, unas cuantas banderas) y cada sección crítica dura
microsegundos, así que un solo lock es **más fácil de demostrar correcto**
que varios locks con un orden de adquisición que se pueda violar. Añadir un
segundo lock obligaría a fijar un orden global (`lock_ovni` antes que `lock`)
y a demostrar que nadie lo viola. El coste está medido y es aceptable.

---

## El diseño del tick, y dos errores reales que conviene documentar

El corazón del sincronismo es un tick de la flota. La versión de partida lo
hacía con dos `threading.Barrier(33)` **reutilizables**. Aquí hace falta otro
enfoque, y el camino hasta el paso por dos errores que vale la pena documentar.

### Error 1: la barrera reutilizable se rompe para siempre

`threading.Barrier.wait(timeout=1.0)` **no** devuelve `False` si se agota el
tiempo: lanza `BrokenBarrierError` y además pone la barrera en estado roto
**irreversible**. En la versión de partida el `except BrokenBarrierError: pass`
se tragaba ese error, y un solo hilo retrasado más de 1 s mataba la
sincronización para el resto de la partida: los 32 hilos se iban por su
`return`, y el juego seguía dibujando una flota congelada sin decir nada.

Medido en la versión original:

```
tick 1: BARRERA ROTA (tras 1.00s)
invasores vivos: 0 / 32 -> flota congelada para siempre
```

Un semáforo no tiene estado de fallo: si un hilo tarda más, solo hace que el
principal espere más.

### Error 2: un semáforo **compartido** tampoco sirve para un encuentro 1:1

La primera corrección fue usar **un** semáforo `tick_sem` compartido con 32
permisos, que el principal liberaba de golpe en cada tick. Parecía correcta. No
lo era:

```
tras 1 tick -> x = [292, 155, 210, 265, 320, 375, ...]
```

El invasor 0 se había movido **32 veces** (100 → 292 = 32 × 6 px) y los otros
31 **ninguna**. El contador de permisos cuadraba perfectamente, de modo que
cualquier aserción sobre el semáforo lo daba por bueno.

El motivo es que `Semaphore` **no garantiza justicia**. Cuando el principal
suelta 32 permisos de golpe y un hilo ya está despierto, ese hilo los vacía
todos antes de que el planificador despierte a los demás, que se quedan
esperando. Cada hilo consume un permiso, pero no necesariamente el suyo.

**La solución: un semáforo privado por hilo.** `estado.permisos` es una lista
de 32 semáforos, y el hilo *i* solo puede tomar el suyo. El encuentro 1:1 pasa
a ser exacto por construcción, llegue quien llegue y cuando llegue.

```python
# partida.py -- dar el turno
for sem in e.permisos:
    sem.release()

# hilos.py -- tomar SU turno
e.permisos[self.inv.idx].acquire()
```

### El tick final

```python
for sem in e.permisos:          # 1. un permiso por hilo
    sem.release()
if nivel_nuevo:                 # 2. barrera, DESPUÉS de soltar los permisos
    e.listos_nivel.wait()
for _ in range(32):             # 3. esperar los 32 "ya me moví"
    e.listo_sem.acquire(timeout=1.0)
```

El orden del punto 2 es obligatorio y es la fuente de otro bug encontrado: los
hilos **solo llegan a la barrera después de consumir su turno**. Si el
principal espera en la barrera antes de soltar los permisos, espera 3 s a 32
hilos que están bloqueados en su `acquire()`, la barrera se rompe y cada
cambio de ronda cuesta 3 segundos de congelación.

`listo_sem` sí es compartido, y a propósito: al principal solo le interesa un
total de 32 avisos, no quién los dará.

### Por qué el permiso se devuelve en un `finally`

```python
e.permisos[self.inv.idx].acquire()
try:
    ...
finally:
    e.listo_sem.release()   # SIEMPRE, incluso si el hilo revienta
```

Si el permiso se devolviera solo en el camino feliz, una excepción en un hilo
desalinearía el conteo **para el resto de la partida**. Por eso el `timeout`
de la fase 3 puede avisar por log sin miedo: no desalinea nada.

Y los hilos de invocador **nunca mueren durante la partida**. Uno ya muerto
sigue existiendo, consume su turno y no hace nada. Eso elimina de raíz toda la
familia de fallos por hilos caídos.

---

## Demonios: `join` y `daemon=True` no son lo mismo

En la versión de partida **todos** los hilos eran demonios, así que el `join`
no demostraba nada. Aquí el contraste es explícito:

| Hilo                  | `daemon` | Motivo |
|-----------------------|----------|--------|
| `invasor-00` … `invasor-31` | `False` | El juego no funciona sin ellos. `join` **garantiza** que terminan. |
| `bomba-0` … `bomba-2` | `False` | Proyectiles en vuelo: perderlos dejaria entidades en la lista de hilos sin cerrar. |
| `bala-001` …          | `False` | Proyectil en vuelo. |
| `ovni`                | `True`   | Tarea de servicio. Perder el OVNI en pantalla no importa. |
| `telemetria`          | `True`   | Volcado de contadores al log. Es *obediente* (comprueba `fin`), pero el proceso no lo espera. |
| `demonio-peligroso`   | `True`   | Demostración opt-in de un demonio **incorrecto**. |

- **`join` garantiza** que un hilo termina. Es un contrato.
- **`daemon=True` renuncia** a esa garantía a cambio de que el proceso pueda
  salir sin esperar. Es una renuncia.

Un demonio no puede colgar el proceso principal, pero sí dejar un hilo a medio
escribir, ensuciar la salida o lanzar excepciones durante el apagado. Por eso
`HiloDemonioPeligroso` —que ignora `fin`, toma el `Lock` compartido mientras el
intérprete se desmonta y escribe en el sistema de logging cuando los handlers
pueden estar cerrados— está detrás de un flag y apagado por defecto.

### El apagado ordenado

```python
e.fin.set()                      # 1. la señal de parada
for sem in e.permisos:          # 2. despertar a quien está en un acquire()
    sem.release()
e.listos_nivel.abort()          # 3. liberar la barrera
for h in e.hilos:                # 4. join con timeout
    h.join(timeout=1.0)
```

El paso 2 es obligatorio: **un semáforo no se despierta solo** con la señal
`fin`. Sin él, los 32 hilos de la flota quedarían bloqueados para siempre en
`acquire()` y el proceso no podría salir.

---

## Defectos de la versión de partida y qué se hizo

| # | Defecto | Ubicación original | Solución |
|---|---------|--------------------|----------|
| D1 | Barrera reutilizable rota de forma irreversible; un hilo lento congela la flota para siempre | `barrera.wait(timeout=1.0)` + `except BrokenBarrierError: pass` | Semáforos de turno, uno privado por hilo. Sin estado de fallo. |
| D2 | Carrera de salida: el `return` entre las dos barreras dejaba el conteo de partes incompleto | entre las dos barreras reutilizables | El permiso se devuelve en `finally`; los hilos no mueren nunca en partida. |
| D3 | `balas_sem.acquire()` bloqueante + un hilo por pulsación: 100 pulsaciones creaban 100 hilos y 97 quedaban en cola | `hilo_bala` | `acquire(blocking=False)` antes de crear el hilo. Si no hay cupo, la pulsación **se descarta**. |
| D4 | Las ~30 líneas de dibujado dentro de `estado.lock` congelaban a los 36 hilos en cada frame | bloque de render | `Snapshot`: una pasada rápida bajo el lock, todo el dibujado fuera. |
| D5 | `estado.bajar` nunca se reseteaba cuando `vivos` era vacío | `_revisar_estado` | Se recalcula en cada tick desde las posiciones reales. |
| D6 | `time.time()` para medir ticks: salta si se ajusta el reloj del sistema | reloj de la partida | `time.monotonic()`. Hay un test que lo comprueba sobre el código fuente. |
| D7 | El OVNI se dibujaba en `y=40..60` y se colisionaba con `40 <= bala.y <= 60` escritos a mano | `hilo_bala` vs `pygame.draw.rect` | Todo sale de `config.OVNI_Y/W/H`. Hay un test que lo comprueba. |
| D8 | Sin escudos, sin vidas, sin puntuación, sin reinicio: no se podía perder ni ganar | — | Escudos destructibles, 3 vidas, 50/40/30/20 por fila, `R` reinicia, pausa con `P`. |
| D9 | `time.sleep(20)` en el OVNI: el apagado tardaba hasta 20 s en notarse | `hilo_ovni` | Duerme en rebanadas de 50 ms comprobando `fin`. |
| D10 | `Thread(..., daemon=True)` en todo: el `join` no demostraba nada | todos los hilos | No-demonio para lo esencial, demonio para lo accesorio. |

---

## Estructura del proyecto

Este README es el resumen. El razonamiento completo de cada módulo —por qué 32
semáforos y no uno, por qué los permisos se sueltan antes de la barrera, por qué
un demonio obediente tampoco se puede `join`ear— está en
**[`ARQUITECTURA.md`](ARQUITECTURA.md)**.

```
config.py       constantes. Sin pygame.
estado.py       entidades + Estado con TODAS las primitivas de sincronización.
hilos.py        las 6 clases de hilo y el decorador que impide morir en silencio.
partida.py      ciclo de vida: arrancar, tick por tick, reglas, apagar.
render.py       el único módulo que dibuja. Snapshot bajo lock, dibujo fuera.
telemetria.py   logging y volcado de contadores.
benchmark.py    medición de aceleración (no usa pygame).
main.py         bucle de pygame, teclado y apagado.
tests/          43 tests headless.
ARQUITECTURA.md el razonamiento de cada módulo, en detalle.
```

`config`, `estado`, `hilos`, `partida`, `telemetria` y `benchmark` **no importan
pygame**. Eso permite ejecutar y probar toda la lógica de concurrencia en modo
headless, sin ventana, en CI, y en menos de 12 segundos.

### La frontera del render

```python
s = render.tomar_snapshot(partida.estado)   # una pasada bajo el lock
render.dibujar(pantalla, s)                 # todo el dibujado FUERA del lock
```

`Snapshot` es un dataclass que se rellena entero bajo el lock en una sola
pasada rápida, y luego se dibuja sin lock. Los sprites se pre-renderizan a
`Surface` una vez y solo se blitean: dibujar 32 invasores celda a celda serían
~2 800 `draw.rect` por frame, blitear 32 superficies son 32 llamadas.

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -v      # 43 tests, ~12 s
```

Headless y deterministas. Cada uno ataca un defecto concreto:

- el invariante de permisos (todos a cero tras cada tick);
- que los 32 hilos se mueven en cada tick, incluso con llegadas desordenadas;
- el test de estrés que congelaba la versión original (retraso de 1,4 s);
- el cupo de balas y el de bombas, y que los saldos nunca excedan la capacidad;
- la barrera se recrea en cada ronda y no se reutiliza;
- el apagado termina los 35 hilos en menos de 2 s y es idempotente;
- que la partida continue tras la muerte de un hilo;
- `eq=False` en las entidades, para que `list.remove` borre la bala correcta;
- que la flota llegue a los dos bordes, gire, baje **una** fila y no se salga
  de la pantalla (aquí está D5: `bajar` nunca se reseteaba);
- escudos, puntuación, pausa, victoria, invasión, avance de ronda.

### Reproducir el congelamiento original

```bash
python main.py --retraso-invasor 1400
```

Un hilo se duerme 1,4 s en cada tick (más que el timeout de 1 s). En la versión
original esto congelaba la flota para siempre; aquí solo produce un tick lento y
un aviso en el log:

```
[WARNING] MainThread : tick 20: el hilo 20 no respondio en 1.0s
[INFO   ] MainThread : resumen: ... ticks=30 ... tick_max=1402ms
```

---

## Aceleración: por qué 32 hilos no aceleran nada

```bash
.venv/bin/python benchmark.py
```

```
magnitudes de referencia
  T_trabajo  (1 hilo, sin sincronizar)      0.216 ms   =     0.72 us/tick
  T_semaforo (1 ida y vuelta)               63.363 us   =    87.9x el trabajo de un tick

 hilos      tiempo     ticks/s     S(n)  coste rel.   us/tick
     1    19.52 ms      15,368    0.011       90.3x      65.1
     8   142.26 ms       2,109    0.002      658.1x     474.2
    32   554.11 ms         541    0.000     2563.3x    1847.0
    64  1082.77 ms         277    0.000     5008.9x    3609.2
```

**Una sola ida y vuelta de un semáforo cuesta 88 veces más que los 32
movimientos completos de un tick.** El trabajo por tick son unas pocas
operaciones de punto flotante sobre un atributo; la coordinación cuesta
microsegundos. Por eso `S(n) = T_trabajo / T_n` está muy por debajo de 1 y
empeora al aumentar *n*.

Esto no es un defecto del código. Son dos consecuencias:

1. El trabajo por tick es tan pequeño que la orquestación lo domina.
2. El **GIL** ya serializa el bytecode de Python: los N hilos no se ejecutan
   en paralelo en la CPU, solo se reparten el tiempo. Añadir hilos no añade
   potencia de cálculo.

La conclusión defendible en la sustentación es precisamente esa: la
aceleración real es `S(n) ≤ 1`, y el coste crece linealmente con *n*. La
arquitectura con hilos está justificada por el modelo de dominio (un hilo por
invocador es el enunciado), no por el rendimiento.

---

## Registro de cambios

- `deepseek_python_20261005_8f30b2.py` — versión de partida, **sin modificar**.
  Se conserva en el commit de línea base (`fa78275`) para poder comparar el
  antes y el después durante la sustentación. Los diez defectos (D1–D10) se
  citan en la tabla de arriba contra ese archivo.