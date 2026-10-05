"""Ciclo de vida de una partida: crear, arrancar, tick por tick y apagar.

Modulo sin pygame, a proposito. Todo el juego (reglas, flota, proyectiles,
condiciones de victoria y derrota) se puede ejecutar y probar sin ventana, y
`main.py` se limita a leer el teclado y a dibujar.

Donde vive cada primitiva de sincronizacion
------------------------------------------
    threading.Lock       `estado.lock`      -> estado.py
    threading.Event      `estado.fin`       -> estado.py
    threading.Semaphore  `permisos[i]`     -> estado.py  (turno de tick)
    threading.Semaphore  `listo_sem`       -> estado.py  ("ya me movi")
    threading.Barrier    `listos_nivel`     -> estado.py  (arranque de ronda)
    threading.Semaphore  `balas_sem`        -> estado.py  (cupo del jugador)
    threading.Semaphore  `bombas_libres`    -> estado.py  (cupo enemigo)
    threading.Semaphore  `disparo_sem`      -> estado.py  (peticion de tiro)
    Thread.join          `Partida.apagar`   -> aqui
    daemon=True          hilos de servicio  -> hilos.py
"""

from __future__ import annotations

import logging
import threading
import time

import config as C
from estado import Estado
from hilos import (
    HiloBala,
    HiloBomba,
    HiloDemonioPeligroso,
    HiloInvasor,
    HiloOVNI,
    HiloTelemetria,
)

log = logging.getLogger("partida")

#: Por encima de este numero de referencias a hilos, se podan los ya terminados.
#`HiloBala` es efimero: sin esta poda, `hilos` crecia sin limite en una partida
# larga y `apagar()` intentaba hacer join de miles de hilos ya muertos.
PODA_HILOS = 64


class Partida:
    """Una partida completa: estado + hilos + reloj de ticks.

    Reiniciar (tecla R) crea una `Partida` nueva, con estado y semaforos
    nuevos. Es la forma mas limpia de no arrastrar permisos, referencias de
    hilos ni invasores muertos de la partida anterior.
    """

    def __init__(
        self,
        seed: int = 0,
        niveles: int = C.NIVELES_POR_DEFECTO,
        *,
        retraso_invasor_ms: float = 0.0,
        demonio_peligroso: bool = False,
        telemetria: bool = True,
    ) -> None:
        self.estado = Estado(seed=seed, niveles=niveles)
        self.retraso_invasor_ms = retraso_invasor_ms
        self.demonio_peligroso = demonio_peligroso
        self.quiere_telemetria = telemetria
        self.en_juego = False
        self.ultimo_tick = time.monotonic()
        #: Nivel cuya barrera de arranque aun no se ha cruzado.
        self.nivel_pendiente = 0

    # ------------------------------------------------------------------
    # Arranque y apagado
    # ------------------------------------------------------------------
    def arrancar(self) -> None:
        """Prepara el nivel 1 y lanza todos los hilos."""
        e = self.estado
        e.nuevo_nivel()  # nivel 1 + barrera nueva

        # --- Hilos no demonio: se garantiza su terminacion con join ---
        for i in range(C.INVASOR_TOTAL):
            h = HiloInvasor(e, i, retraso_ms=self.retraso_invasor_ms)
            h.start()
            e.hilos.append(h)
        for n in range(C.MAX_BOMBAS):
            h = HiloBomba(e, n)
            h.start()
            e.hilos.append(h)

        # --- Hilos demonio: tareas de servicio, el proceso no los espera ---
        ovni = HiloOVNI(e)
        ovni.start()
        log.debug("demonio arrancado: %s", ovni.name)
        if self.quiere_telemetria:
            tel = HiloTelemetria(e)
            tel.start()
            log.debug("demonio arrancado: %s", tel.name)
        if self.demonio_peligroso:
            dp = HiloDemonioPeligroso(e)
            dp.start()
            log.warning("demonio peligroso arrancado: %s (opt-in, incorrecto a proposito)", dp.name)

        self.en_juego = True
        log.info(
            "partida iniciada: %d hilos de invasor + %d de bomba (no demonio), "
            "%d demonios de servicio",
            C.INVASOR_TOTAL,
            C.MAX_BOMBAS,
            2,
        )

    def apagar(self) -> None:
        """Apagado ordenado. Idempotente.

        El orden importa: primero la senal `fin`, despues se devuelven los
        permisos de tick para despertar a quien estuviese bloqueado en
        `permisos[i]` (un `acquire()` bloqueante no se despierta solo),
        y por ultimo los `join`.
        """
        if not self.en_juego:
            return
        e = self.estado
        log.info("apagando la partida...")
        e.fin.set()

        # Despertar a quien este bloqueado en un `acquire()`: un semaforo no
        # se despierta solo con la senal `fin`.
        for sem in e.permisos:
            sem.release()
        for _ in range(C.MAX_BOMBAS):
            e.disparo_sem.release()
        e.listos_nivel.abort()  # libera a quien este esperando en la barrera

        supervivientes = []
        for h in e.hilos:
            h.join(timeout=C.TIMEOUT_JOIN_S)
            if h.is_alive():
                supervivientes.append(h.name)
                log.error("[%s] no termino en %.1fs", h.name, C.TIMEOUT_JOIN_S)
            else:
                log.debug("[%s] terminado", h.name)

        if supervivientes:
            log.error("hilos que sobrevivieron al join: %s", ", ".join(supervivientes))
        else:
            log.info("los %d hilos no-demonio terminaron correctamente", len(e.hilos))
        self.en_juego = False

    # ------------------------------------------------------------------
    # Entrada
    # ------------------------------------------------------------------
    def mover_jugador(self, dx: int) -> None:
        """Mueve al jugador. `dx` en {-1, 0, 1}. Lo llama el hilo principal."""
        if dx == 0:
            return
        with self.estado.lock:
            self.estado.jugador_x = max(
                C.JUGADOR_W / 2,
                min(C.WIDTH - C.JUGADOR_W / 2, self.estado.jugador_x + dx),
            )

    def disparar(self) -> bool:
        """Intenta lanzar una bala. False si no hay cupo.

        El cupo se reserva con `blocking=False` ANTES de crear el hilo. Asi el
        semaforo limita de verdad las balas vivas: no se encolan pulsaciones
        pendientes, que era el defecto D3 de la version de partida (20
        pulsaciones creaban 20 hilos y 17 quedaban en cola disparando en
        rafaga).
        """
        e = self.estado
        with e.lock:
            if not e.balas_sem.acquire(blocking=False):
                e.contadores.disparos_rechazados += 1
                return False
            x = e.jugador_x
            e.contadores.disparos_aceptados += 1

        h = HiloBala(e, x)
        h.start()
        e.hilos.append(h)
        if len(e.hilos) > PODA_HILOS:
            # `HiloBala` es efimero: se podan los ya terminados para que la
            # lista no crezca sin limite.
            e.hilos = [t for t in e.hilos if t.is_alive()]
        return True

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------
    def tick_pendiente(self) -> bool:
        """True si ya paso el tiempo del tick."""
        e = self.estado
        if e.pausa or e.resultado != "jugando" or e.nivel_superado:
            return False
        return (time.monotonic() - self.ultimo_tick) * 1000 >= self.ms_por_tick()

    def ms_por_tick(self) -> int:
        """Latido del nivel actual. Se acelera un poco en cada ronda."""
        return max(60, C.TICK_MS - max(0, self.estado.nivel - 1) * C.TICK_MS_POR_NIVEL)

    def tick(self) -> None:
        """Un tick completo de la flota.

        Este es el corazon del sincronismo y sustituye a las dos barreras
        reutilizables de la version de partida:

            fase 1   un `release()` por hilo  -> los 32 reciben su permiso
            fase 2   32 `acquire()`           -> se espera a que los 32 terminen

        El permiso se devuelve en un `finally` dentro del hilo de invasor, de
        modo que el conteo cuadra siempre, incluso si un hilo lanza una
        excepcion. Por eso el timeout de la fase 2 puede avisar por log sin
        desalinear nada.
        """
        e = self.estado
        t0 = time.monotonic()

        # --- fase 1: dar permiso de tick, uno a cada hilo ---
        # Cada hilo recibe SU permiso. Con un semaforo compartido, un hilo ya
        # despierto podria vaciar los 32 permisos y dejar a los otros 31 sin
        # moverse; con permisos privados el encuentro 1:1 es exacto.
        #
        # El orden importa: los permisos se sueltan ANTES de esperar en la
        # barrera de nivel, porque los hilos solo llegan a ella DESPUES de
        # consumir su turno. Al revés, el principal esperaria 3 s a 32 hilos
        # que estan bloqueados en su `acquire()`, y la barrera se romperia.
        for sem in e.permisos:
            sem.release()

        # --- barrera de nivel: una vez por ronda ---
        if self.nivel_pendiente != e.nivel:
            self.nivel_pendiente = e.nivel
            try:
                e.listos_nivel.wait(timeout=C.TIMEOUT_BARRERA_NIVEL_S)
            except threading.BrokenBarrierError:
                log.error("barrera de nivel %d rota", e.nivel)
            else:
                log.info("nivel %d: los %d hilos listos", e.nivel, C.INVASOR_TOTAL)

        # --- fase 2: esperar a que los 32 terminen ---
        for i in range(C.INVASOR_TOTAL):
            if e.fin.is_set():
                # La partida termino a mitad del tick (derrota, ESC). No hay
                # nada que sincronizar; los permisos sobrantes se van con el
                # Estado, que se recrea en el siguiente reinicio.
                break
            if not e.listo_sem.acquire(timeout=C.TIMEOUT_ESPERA_TICK_S):
                # Red de seguridad, y se REGISTRA en vez de ignorarse. Con el
                # `finally` del hilo invasor el conteo sigue cuadrando, asi que
                # este aviso nunca desalinea la sincronizacion.
                log.warning(
                    "tick %d: el hilo %02d no respondio en %.1fs",
                    e.contadores.ticks,
                    i,
                    C.TIMEOUT_ESPERA_TICK_S,
                )

        self._decidir_giro()
        self._revisar_estado()

        with e.lock:
            e.contadores.ticks += 1
            transcurrido = int((time.monotonic() - t0) * 1000)
            if transcurrido > e.contadores.tiempo_tick_max_ms:
                e.contadores.tiempo_tick_max_ms = transcurrido

        self.ultimo_tick = time.monotonic()

    # ------------------------------------------------------------------
    # Reglas
    # ------------------------------------------------------------------
    def _decidir_giro(self) -> None:
        """Calcula `direccion` y `bajar` para el siguiente tick.

        Los invasores NO se auto-dirigen: cada uno lee estos dos valores ya
        calculados, bajo el lock. Concentrar aqui la decision evita la
        condicion de carrera clasica de que 32 hilos escriban `direccion` a la
        vez, y es la razon de que la decision se tome una sola vez por tick.
        """
        with self.estado.lock:
            e = self.estado
            vivos = [i for i in e.invasores if i.vivo]
            if not vivos:
                e.bajar = False
                return
            min_x = min(i.x for i in vivos)
            max_x = max(i.x for i in vivos)
            if max_x + C.INVASOR_W >= C.WIDTH - C.INVASOR_MARGEN and e.direccion > 0:
                e.direccion = -1
                e.bajar = True
            elif min_x <= C.INVASOR_MARGEN and e.direccion < 0:
                e.direccion = 1
                e.bajar = True
            else:
                e.bajar = False

    def _revisar_estado(self) -> None:
        """Ronda superada, derrota por invasion o por impacto.

        Importante: al superar una ronda NO se activa `fin`. Los hilos siguen
        vivos y se reunen en la barrera nueva del siguiente nivel. Activar `fin`
        aqui los mataria y habria que relanzar 32 hilos en cada ronda.
        """
        e = self.estado
        if e.resultado != "jugando":
            return
        with e.lock:
            if e.nivel_superado:
                return
            vivos = [i for i in e.invasores if i.vivo]
            if not vivos:
                if e.nivel >= e.niveles:
                    e.resultado = "victoria"
                    e.fin.set()
                    log.info("VICTORIA: %d rondas superadas", e.niveles)
                else:
                    e.nivel_superado = True
                    log.info("ronda %d superada (%d pts)", e.nivel, e.puntaje)
            elif any(i.y + C.INVASOR_H >= C.LINEA_INVASION for i in vivos):
                e.resultado = "invasion"
                e.fin.set()
                log.warning("INVASION: la flota llego a la linea del jugador")

    # ------------------------------------------------------------------
    # Avance de ronda
    # ------------------------------------------------------------------
    def avanzar_nivel(self) -> None:
        """Prepara la ronda siguiente. Lo llama el principal entre ticks."""
        e = self.estado
        e.nivel_superado = False
        e.resultado = "jugando"
        e.nuevo_nivel()
        log.info("=== ronda %d: %d px por tick, tick de %d ms ===", e.nivel, e.step, self.ms_por_tick())

    def alternar_pausa(self) -> bool:
        """Invierte la pausa y devuelve el valor nuevo.

        Se expone como metodo y no como `estado.pausa = ...` para que ningun
        escritor del juego tenga que acordarse del lock.
        """
        with self.estado.lock:
            self.estado.pausa = not self.estado.pausa
            return self.estado.pausa

    @property
    def terminada(self) -> bool:
        # Aunque hoy solo el hilo principal lee y escribe estos tres campos,
        # se leen bajo el lock: el coste es un acquire por frame y la garantia
        # de que nadie accedera al estado compartido sin el lock.
        with self.estado.lock:
            return self.estado.resultado in ("victoria", "derrota", "invasion")

    @property
    def entre_rondas(self) -> bool:
        with self.estado.lock:
            return self.estado.nivel_superado
