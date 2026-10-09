"""Estado compartido y entidades del juego.

Modulo sin pygame. Aqui viven:

* las entidades (`Invasor`, `Bala`, `Bomba`, `Escudo`, `Snapshot`),
* la clase `Estado`, que contiene TODOS los datos compartidos entre hilos,
* las primitivas de sincronizacion, cada una con un comentario que explica
  que problema concreto resuelve.

REGLA DE ORO DE ESTE ARCHIVO
---------------------------
Todo dato mutado por mas de un hilo se protege SIEMPRE con `estado.lock`.
La unica excepcion son los objetos de `threading` que ya son seguros por si
mismos (`Lock`, `Event`, `Semaphore`, `Barrier`), que se usan sin lock
adicional porque son la sincronizacion en si.

Hay exactamente un mutex (`estado.lock`). Los proyectiles son pocos (3 balas,
3 bombas) y cada seccion critica dura microsegundos, asi que un solo lock es
mas facil de demostrar que correctos que varios locks con un orden de
adquisicion que se pueda violar. `README.md` documenta la alternativa.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from random import Random

import config as C


# ---------------------------------------------------------------------------
# Contadores de telemetria
# ---------------------------------------------------------------------------
@dataclass
class Contadores:
    """Metricas del juego. Se incrementan siempre bajo `estado.lock`."""

    ticks: int = 0
    disparos_pedidos: int = 0
    disparos_aceptados: int = 0
    disparos_rechazados: int = 0  # pulsaciones sin cupo de bala
    invasores_muertos: int = 0
    ovnis_derribados: int = 0
    ovnis_vistos: int = 0
    bombas_lanzadas: int = 0
    bombas_destruidas: int = 0
    bombas_impactadas: int = 0
    vidas_perdidas: int = 0
    tiempo_tick_max_ms: int = 0  # el tick mas lento, para detectar stalls

    def copia(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


# ---------------------------------------------------------------------------
# Entidades
# ---------------------------------------------------------------------------
@dataclass
class Invasor:
    """Un invasor. Cada uno es poseido por exactamente un hilo."""

    idx: int
    fila: int
    columna: int
    x: float
    y: float
    vivo: bool = True

    @property
    def puntos(self) -> int:
        return C.PUNTOS_POR_FILA[self.fila]


@dataclass(eq=False)
class Bala:
    """Proyectil del jugador. Solo lo muta su propio hilo.

    `eq=False` es IMPORTANTE: con `@dataclass` a secas se genera `__eq__` por
    campos, y `list.remove(bala)` borraria el primer proyectil *igual en
    coordenadas*, no necesariamente el suyo. Con identidad, `remove` borra
    exactamente el suyo.
    """

    x: float
    y: float
    activa: bool = True


@dataclass(eq=False)
class Bomba:
    """Proyectil enemigo. Solo lo muta su propio hilo del pool."""

    x: float
    y: float
    activa: bool = True


class Escudo:
    """Bunker destructible, modelado como una rejilla de celdas.

    Cada celda es `True` si todavia hay material. Un escudo pierde celdas de
    forma irreversible, asi que basta un booleano por celda.

    `sucio` no es parte del juego: lo usa `render.py` para saber si tiene que
    volver a construir la superficie del escudo. Redibujar 4 escudos de 96
    celdas cada frame serian 384 `draw.rect`; con la cache son 4 `blit`.
    """

    __slots__ = ("x", "y", "celdas", "sucio")

    def __init__(self, x: int, y: int) -> None:
        self.x = x
        self.y = y
        self.celdas = [
            [C.ESCUDO_FORMA[f][c] == "#" for c in range(C.ESCUDO_CELDAS_X)]
            for f in range(C.ESCUDO_CELDAS_Y)
        ]
        self.sucio = True  # cache de render invalidada; lo gestiona `render.py`

    def celda_rota(self, px: float, py: float) -> bool:
        """True si el punto (px, py) cae en una celda ya destruida."""
        cx = int((px - self.x) // C.ESCUDO_CELDA)
        cy = int((py - self.y) // C.ESCUDO_CELDA)
        if not (0 <= cx < C.ESCUDO_CELDAS_X and 0 <= cy < C.ESCUDO_CELDAS_Y):
            return False
        return not self.celdas[cy][cx]

    def danar(self, px: float, py: float) -> bool:
        """Destruye la celda que contiene (px, py). True si habia material."""
        cx = int((px - self.x) // C.ESCUDO_CELDA)
        cy = int((py - self.y) // C.ESCUDO_CELDA)
        if not (0 <= cx < C.ESCUDO_CELDAS_X and 0 <= cy < C.ESCUDO_CELDAS_Y):
            return False
        if not self.celdas[cy][cx]:
            return False
        self.celdas[cy][cx] = False
        self.sucio = True
        return True


@dataclass
class Snapshot:
    """Copia del estado protegida, para dibujar SIN mantener el lock.

    El render nunca dibuja mientras el juego escribe. Este dataclass es la
    frontera: se rellena bajo el lock en una sola pasada rapida y luego se
    dibuja fuera, de modo que los hilos de la flota no se detienen durante
    el dibujado.
    """

    jugador_x: float
    vidas: int
    puntaje: int
    nivel: int
    invaders: list[tuple[float, float, int]]  # (x, y, fila)
    ovni: tuple[float, float] | None
    balas: list[tuple[float, float]]
    bombas: list[tuple[float, float]]
    escudos: list[Escudo]  # se comparten por referencia: el render no muta
    invulnerable: bool
    resultado: str
    nivel_superado: bool
    pausado: bool
    contadores: dict[str, int]


# ---------------------------------------------------------------------------
# Estado compartido
# ---------------------------------------------------------------------------
class Estado:
    """Todos los datos compartidos y todas las primitivas de sincronizacion.

    Se recrea entero en cada partida nueva (reinicio con R), de modo que un
    reinicio no arrastra ni permisos de semaforo ni referencias de hilos de la
    partida anterior.
    """

    def __init__(self, seed: int = 0, niveles: int = C.NIVELES_POR_DEFECTO) -> None:
        self.seed = seed
        self.niveles = niveles
        self.rng = Random(seed)  # solo para el hilo principal

        # ---------------- Primitiva 1: bloqueo (mutex) ----------------
        # Una unica seccion critica para todos los datos compartidos.
        self.lock = threading.Lock()

        # ---------------- Primitiva 2: evento (senal de parada) ----------------
        # `fin` es la senal de "todos a terminar". La consultan TODOS los hilos
        # en su bucle; el apagado ordenado la activa una vez.
        self.fin = threading.Event()

        # ---------------- Primitiva 3 y 4: semaforos del ciclo de tick -----
        # `permisos[i]` es el permiso de movimiento DEL HILO i. Son 32
        # semaforos privados en vez de uno compartido, y no es un capricho:
        # un semaforo compartido NO es justo. Si el principal libera 32
        # permisos de golpe y un hilo ya esta despierto, ese hilo puede
        # vaciarlos todos antes de que el planificador despierte a los otros
        # 31, que se quedan esperando. Con un semaforo privado por hilo, cada
        # uno solo puede tomar SU permiso, asi que el encuentro 1:1 es exacto.
        #
        # `listo_sem` si es compartido, y a proposito: al principal solo le
        # interesa un total de 32 avisos, no quien los dara.
        self.permisos = [threading.Semaphore(0) for _ in range(C.INVASOR_TOTAL)]
        self.listo_sem = threading.Semaphore(0)

        # ---------------- Primitiva 5: barrera (one-shot por nivel) --------
        # La flota entera se reune una vez al arrancar cada ronda. Se crea
        # nueva en cada nivel y NUNCA se reutiliza ni se le pasa timeout a los
        # hilos de juego: por eso no puede quedarse en estado roto.
        self.nivel = 0
        self.listos_nivel = threading.Barrier(C.INVASOR_TOTAL + 1)

        # ---------------- Semaforo de recursos: balas del jugador ---------
        # Cupo de 3 proyectiles. Se reserva con `acquire(blocking=False)`
        # ANTES de crear el hilo de la bala: si no hay cupo, la pulsacion se
        # descarta. Asi ningun hilo queda esperando turno.
        self.balas_sem = threading.Semaphore(C.MAX_BALAS)

        # ---------------- Semaforos de recursos: bombas enemigas -----------
        # `bombas_libres` es el cupo de proyectiles enemigos: lo reserva el
        # invasor (sin bloquear) y lo devuelve el hilo del pool al terminar.
        # `disparo_sem` transporta la peticion de disparo. Los dos juntos
        # garantizan <= MAX_BOMBAS bombas vivas sin que se acumulen tokens.
        self.bombas_libres = threading.Semaphore(C.MAX_BOMBAS)
        self.disparo_sem = threading.Semaphore(0)
        self.peticiones: deque[float] = deque()  # coordenadas x pedidas

        # ---------------- Datos del juego (protegidos por `lock`) ---------
        self.invasores = [
            Invasor(
                idx=f * C.INVASOR_COLUMNAS + c,
                fila=f,
                columna=c,
                x=C.INVASOR_LEFT + c * C.INVASOR_SEP_X,
                y=C.INVASOR_TOP + f * C.INVASOR_SEP_Y,
            )
            for f in range(C.INVASOR_FILAS)
            for c in range(C.INVASOR_COLUMNAS)
        ]

        self.direccion = 1  # 1 derecha, -1 izquierda
        self.bajar = False  # solo vale para el tick en curso
        self.step = C.INVASOR_STEP  # px por tick; sube con el nivel
        self.jugador_x = float(C.WIDTH // 2)
        self.vidas = C.VIDAS_INICIALES
        self.puntaje = 0
        self.balas: list[Bala] = []
        self.bombas: list[Bomba] = []
        self.escudos = [
            Escudo(x, C.ESCUDO_Y)
            for x in (
                (C.WIDTH - (C.ESCUDO_CANTIDAD * C.ESCUDO_W + (C.ESCUDO_CANTIDAD - 1) * C.ESCUDO_SEP))
                // 2
                + i * (C.ESCUDO_W + C.ESCUDO_SEP)
                for i in range(C.ESCUDO_CANTIDAD)
            )
        ]

        self.ovni_x = -100.0
        self.ovni_activo = False

        # Reloj del juego en milisegundos. Lo escribe el hilo principal cada
        # frame con pygame.time.get_ticks(); lo leen los hilos de bomba para
        # saber si el jugador sigue invulnerable. Vive aqui para que `hilos.py`
        # no tenga que importar pygame.
        self.tiempo_ms = 0

        # Marca de tiempo hasta la que el jugador es invulnerable.
        self.invulnerable_hasta = 0

        self.pausa = False

        # "jugando" | "victoria" | "derrota" | "invasion"
        self.resultado = "jugando"

        #: True entre el momento en que se limpia la flota y el aviso de ronda
        #: superada. No activa `fin`: los hilos siguen vivos para el siguiente
        #: nivel, que tiene su propia barrera de arranque.
        self.nivel_superado = False
        self.contadores = Contadores()

        # Registra los hilos para poder hacer join en el apagado.
        self.hilos: list[threading.Thread] = []

    # ------------------------------------------------------------------
    # Nivel
    # ------------------------------------------------------------------
    def nuevo_nivel(self) -> None:
        """Prepara la ronda siguiente. Lo llama el hilo principal, con lock."""
        with self.lock:
            self.nivel += 1
            self.nivel_superado = False
            self.resultado = "jugando"
            self.balas.clear()
            self.bombas.clear()
            self.peticiones.clear()
            for inv in self.invasores:
                inv.x = C.INVASOR_LEFT + inv.columna * C.INVASOR_SEP_X
                inv.y = C.INVASOR_TOP + inv.fila * C.INVASOR_SEP_Y
                inv.vivo = True
            for esc in self.escudos:
                esc.celdas = [
                    [C.ESCUDO_FORMA[f][cc] == "#" for cc in range(C.ESCUDO_CELDAS_X)]
                    for f in range(C.ESCUDO_CELDAS_Y)
                ]
                esc.sucio = True
            self.direccion = 1
            self.bajar = False
            self.step = C.INVASOR_STEP + (self.nivel - 1) * C.STEP_POR_NIVEL
            self.jugador_x = float(C.WIDTH // 2)
            self.invulnerable_hasta = 0
            self.ovni_activo = False
            # Barrera NUEVA: una por nivel, nunca reutilizada.
            self.listos_nivel = threading.Barrier(C.INVASOR_TOTAL + 1)
