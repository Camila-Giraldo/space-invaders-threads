"""Los hilos del juego y el decorador que los hace a prueba de fallos.

Modulo sin pygame.

Reparto de responsabilidades
-----------------------------
`HiloInvasor` x32   no demonio   mueven la flota, uno por tick
`HiloBala`          no demonio   efimero, uno por proyectil del jugador
`HiloBomba` x3      no demonio   pool fijo de proyectiles enemigos
`HiloOVNI`          demonio      tarea de servicio, perderla es aceptable
`HiloTelemetria`    demonio      tarea de servicio en segundo plano
`HiloDemonioPeligroso` demonio   demostracion opt-in de un demonio incorrecto

Por que unos son demonio y otros no: `join` es lo que GARANTIZA que un hilo
termine; `daemon=True` es lo que SACRIFICA esa garantia a cambio de que el
proceso pueda salir sin esperar. Cada hilo declara de que lado esta.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from random import Random

import config as C
from estado import Bala, Bomba

log = logging.getLogger("hilos")

#: Contador para dar nombre unico a cada hilo de bala.
_contador = itertools.count(1)


# ---------------------------------------------------------------------------
# Base: ningun hilo muere en silencio
# ---------------------------------------------------------------------------
class HiloSeguro(threading.Thread):
    """Base de todos los hilos del juego.

    Captura cualquier excepcion inesperada y la registra. Un hilo que muere
    marca `fin` y el apagado es ordenado y visible en el log, en vez de
    detener la sincronizacion en silencio.
    """

    daemon = False

    def __init__(self, estado, nombre: str) -> None:
        super().__init__(name=nombre, daemon=self.daemon)
        self.estado = estado

    def cuerpo(self) -> None:  # pragma: no cover - interfaz
        raise NotImplementedError

    def run(self) -> None:
        try:
            self.cuerpo()
        except threading.BrokenBarrierError:
            log.debug("[%s] barrera rota durante el apagado", self.name)
        except Exception:
            log.exception("[%s] excepcion inesperada; se detiene la partida", self.name)
            self.estado.fin.set()

    # -- utilidades de espera cooperativa -------------------------------
    def _dormir(self, segundos: float) -> bool:
        """Duerme en rebanadas. True si hay que terminar porque se puso `fin`.

        Dormir en rebanadas de 50 ms hace que la senal de parada se note de
        inmediato, aunque la espera completa sea mucho mas larga.
        """
        limite = time.monotonic() + segundos
        while True:
            restante = limite - time.monotonic()
            if restante <= 0:
                return False
            if self.estado.fin.is_set():
                return True
            time.sleep(min(0.05, restante))


# ---------------------------------------------------------------------------
# Flota de invasores
# ---------------------------------------------------------------------------
class HiloInvasor(HiloSeguro):
    """Un invasor = un hilo de vida larga.

    Ciclo de cada iteracion:

      1. Si arranco un nivel nuevo, se reune en la `listos_nivel` (barrera,
         una vez por nivel).
      2. Espera SU permiso de tick en `permisos[idx]` (semaforo privado).
         Este `acquire()` es lo que reemplaza al "dormir hasta el proximo tick".
      3. Mueve su propio invasor bajo el lock.
      4. Avisa en `listo_sem` que termino.

    El hilo NO muere nunca durante la partida. Un invasor ya muerto sigue
    existiendo: consume su permiso y no hace nada. Esa es la clave de que la
    sincronizacion sea a prueba de hilos caidos, porque el conteo de permisos
    siempre cuadra.
    """

    def __init__(self, estado, idx: int, retraso_ms: float = 0.0) -> None:
        super().__init__(estado, f"invasor-{idx:02d}")
        self.inv = estado.invasores[idx]
        # Un generador propio por hilo: partidas reproducibles con --seed.
        self.rng = Random((estado.seed * 1_000_003) ^ (idx * 7_919))
        #: Solo para el test de estres: simula una pausa larga del sistema.
        self.retraso_ms = retraso_ms

    def cuerpo(self) -> None:
        e = self.estado
        nivel_visto = 0

        while True:
            # --- 1. Barrera de arranque de nivel (una vez por ronda) -------
            with e.lock:
                nivel = e.nivel
                barrera = e.listos_nivel
            if nivel != nivel_visto:
                nivel_visto = nivel
                if e.fin.is_set():
                    return
                try:
                    # La barrera tiene semantica de un solo disparo y se crea
                    # nueva en cada nivel, asi que no puede quedar rota.
                    barrera.wait(timeout=C.TIMEOUT_BARRERA_NIVEL_S)
                except threading.BrokenBarrierError:
                    if not e.fin.is_set():
                        log.error("[%s] barrera de nivel %d rota", self.name, nivel)

            # --- 2. Permiso de tick ----------------------------------------
            # Permiso PRIVADO de este hilo: no hay forma de que otro lo robe.
            e.permisos[self.inv.idx].acquire()
            try:
                if e.fin.is_set():
                    return
                if self.retraso_ms:
                    # Fuera del lock a proposito: simula que el sistema se
                    # congela. Debe ser inocuo para la sincronizacion.
                    time.sleep(self.retraso_ms / 1000.0)

                # --- 3. Movimiento bajo el lock ---------------------------
                with e.lock:
                    if self.inv.vivo:
                        if e.bajar:
                            self.inv.y += C.INVASOR_DROP
                        else:
                            self.inv.x += e.step * e.direccion
                        self._pedir_disparo()
            finally:
                # --- 4. Aviso de "ya me movi" ---------------------------
                # En `finally` para que el permiso se devuelva SIEMPRE. Si un
                # hilo devolviera el permiso solo en el camino feliz, una
                # excepcion desalinearia el conteo para toda la partida.
                e.listo_sem.release()

    def _pedir_disparo(self) -> None:
        """Pide un proyectil enemigo. Se llama YA con el lock tomado.

        Nunca bloquea: reserva la plaza del pool con `blocking=False` y, si no
        hay, no dispara. Al no bloquear, el hilo invasor no puede retrasar el
        tick por culpa de las bombas.
        """
        e = self.estado
        e.contadores.disparos_pedidos += 1
        prob = C.PROB_DISPARO_POR_FILA[self.inv.fila]
        if prob <= 0 or self.rng.random() >= prob:
            return
        if e.bombas_libres.acquire(blocking=False):
            e.peticiones.append(self.inv.x + C.INVASOR_W / 2)
            e.disparo_sem.release()


# ---------------------------------------------------------------------------
# Proyectiles del jugador
# ---------------------------------------------------------------------------
class HiloBala(HiloSeguro):
    """Proyectil del jugador. Hilo efimero, creado y olvidado.

    El cupo de `balas_sem` lo reserva `Partida.disparar()` con
    `blocking=False` ANTES de crear este hilo. Por eso ningun hilo queda
    esperando turno en el semaforo: las pulsaciones sin cupo se descartan,
    no se encolan.
    """

    def __init__(self, estado, x: float) -> None:
        super().__init__(estado, f"bala-{next(_contador):03d}")
        self.bala = Bala(x=x, y=float(C.JUGADOR_Y - C.JUGADOR_H))

    def cuerpo(self) -> None:
        e = self.estado
        b = self.bala
        with e.lock:
            e.balas.append(b)

        try:
            while True:
                if e.fin.is_set():
                    break
                with e.lock:
                    if not b.activa:
                        break
                    b.y -= C.BALA_PX_POR_S * C.PASO_PROYECTIL
                    if b.y < 0:
                        b.activa = False
                        break
                    self._colisiones()
                time.sleep(C.PASO_PROYECTIL)
        finally:
            with e.lock:
                if b in e.balas:
                    e.balas.remove(b)
            # Solo este hilo devuelve el cupo de bala.
            e.balas_sem.release()

    def _colisiones(self) -> None:
        """Se llama con el lock tomado. Marca `b.activa = False` al impactar."""
        e = self.estado
        b = self.bala

        for esc in e.escudos:
            if esc.danar(b.x, b.y):
                b.activa = False
                return

        if (
            e.ovni_activo
            and e.ovni_x <= b.x <= e.ovni_x + C.OVNI_W
            and C.OVNI_Y <= b.y <= C.OVNI_Y + C.OVNI_H
        ):
            e.ovni_activo = False
            b.activa = False
            e.puntaje += C.PUNTOS_OVNI
            e.contadores.ovnis_derribados += 1
            log.info("[%s] OVNI derribado (+%d)", self.name, C.PUNTOS_OVNI)
            return

        for i, bo in enumerate(e.bombas):
            if abs(bo.x - b.x) <= (C.BOMBA_W + C.BALA_W) and abs(bo.y - b.y) <= (
                C.BOMBA_H + C.BALA_H
            ):
                # La bala destruye la bomba, pero NO devuelve `bombas_libres`:
                # el permiso es del hilo de la bomba, que lo devuelve en su
                # `finally`. Devolverlo aqui duplicaria el cupo.
                del e.bombas[i]
                bo.activa = False
                b.activa = False
                e.contadores.bombas_destruidas += 1
                return

        for inv in e.invasores:
            if (
                inv.vivo
                and inv.x <= b.x <= inv.x + C.INVASOR_W
                and inv.y <= b.y <= inv.y + C.INVASOR_H
            ):
                inv.vivo = False
                b.activa = False
                e.puntaje += inv.puntos
                e.contadores.invasores_muertos += 1
                log.debug("[%s] invasor %d muerto (+%d)", self.name, inv.idx, inv.puntos)
                return


# ---------------------------------------------------------------------------
# Pool de bombas enemigas
# ---------------------------------------------------------------------------
class HiloBomba(HiloSeguro):
    """Hilo del pool de proyectiles enemigos.

    Espera una peticion en `disparo_sem` (semaforo de conteo). La plaza del
    proyectil ya la reservo el invasor con `bombas_libres.acquire(False)`, asi
    que nunca hay mas de `MAX_BOMBAS` bombas vivas y nunca se acumulan tokens.

    Un pool de tamanho fijo no crea hilos por evento, y al no ser demonios se
    pueden cerrar con `join`.
    """

    def __init__(self, estado, numero: int) -> None:
        super().__init__(estado, f"bomba-{numero}")

    def _y_origen(self) -> float:
        """Borde inferior de la flota viva: de ahi salen las bombas."""
        e = self.estado
        vivos = [i.y for i in e.invasores if i.vivo]
        return max(vivos) + C.INVASOR_H if vivos else float(C.INVASOR_TOP)

    def cuerpo(self) -> None:
        e = self.estado
        while True:
            # Espera una peticion de disparo. Este `acquire()` es bloqueante a
            # proposito: el hilo duerme aqui, sin consumir CPU, hasta que un
            # invasor le reserve una plaza.
            e.disparo_sem.acquire()

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

            if e.fin.is_set():
                # Peticion real pero la partida ya termino: se devuelve la
                # plaza reservada y se sale.
                e.bombas_libres.release()
                return

            self._volar(x)

    def _volar(self, x: float) -> None:
        e = self.estado
        bomba = Bomba(x=x, y=self._y_origen())
        with e.lock:
            e.bombas.append(bomba)
            e.contadores.bombas_lanzadas += 1

        try:
            while True:
                if e.fin.is_set():
                    break
                with e.lock:
                    if not bomba.activa:
                        break
                    bomba.y += C.BOMBA_PX_POR_S * C.PASO_PROYECTIL
                    if bomba.y > C.HEIGHT:
                        bomba.activa = False
                        break
                    if self._impacto(bomba):
                        bomba.activa = False
                        break
                time.sleep(C.PASO_PROYECTIL)
        finally:
            with e.lock:
                if bomba in e.bombas:
                    e.bombas.remove(bomba)
            # El hilo dueno del permiso de plaza, siempre.
            e.bombas_libres.release()

    def _impacto(self, bomba: Bomba) -> bool:
        """Se llama con el lock tomado. True si la bomba debe desaparecer."""
        e = self.estado

        for esc in e.escudos:
            if esc.danar(bomba.x, bomba.y):
                e.contadores.bombas_impactadas += 1
                return True

        if e.tiempo_ms < e.invulnerable_hasta:
            return False  # el jugador esta en modo invulnerable: la bomba pasa

        if abs(bomba.x - e.jugador_x) <= (C.JUGADOR_W + C.BOMBA_W) / 2 and abs(
            bomba.y - C.JUGADOR_Y
        ) <= (C.JUGADOR_H + C.BOMBA_H) / 2:
            e.vidas -= 1
            e.contadores.vidas_perdidas += 1
            e.invulnerable_hasta = e.tiempo_ms + C.INVULNERABLE_MS
            log.info("[%s] impacto: quedan %d vidas", self.name, e.vidas)
            if e.vidas <= 0:
                e.resultado = "derrota"
                e.fin.set()
            return True
        return False


# ---------------------------------------------------------------------------
# Demonios
# ---------------------------------------------------------------------------
class HiloOVNI(HiloSeguro):
    """Demonio: el OVNI aparece cada 12-20 s, cruza y desaparece.

    Es una tarea de servicio. Si el programa termina mientras el OVNI esta en
    pantalla, no importa: se pierde el OVNI y ya. Por eso es demonio y por eso
    el proceso nunca espera a que termine.
    """

    daemon = True

    def __init__(self, estado) -> None:
        super().__init__(estado, "ovni")
        self.rng = Random(estado.seed ^ 0xBEEF)

    def cuerpo(self) -> None:
        e = self.estado
        while True:
            if self._dormir(self.rng.uniform(C.OVNI_MIN_MS, C.OVNI_MAX_MS) / 1000.0):
                return

            with e.lock:
                e.ovni_activo = True
                e.ovni_x = -float(C.OVNI_W)
                e.contadores.ovnis_vistos += 1

            while True:
                if e.fin.is_set():
                    with e.lock:
                        e.ovni_activo = False
                    return
                with e.lock:
                    if not e.ovni_activo or e.ovni_x > C.WIDTH + C.OVNI_W:
                        break
                    e.ovni_x += C.OVNI_PX_POR_S * C.PASO_PROYECTIL
                time.sleep(C.PASO_PROYECTIL)

            with e.lock:
                e.ovni_activo = False


class HiloTelemetria(HiloSeguro):
    """Demonio bien portado: vuelca los contadores al log cada N segundos.

    Comprueba `fin` y sale con education. Aun asi sigue siendo demonio, y esa
    es la leccion: un demonio puede ser obediente y aun asi no se le espera con
    `join`, porque el programa puede morir antes de que termine su ultimo volcado.
    """

    daemon = True

    def __init__(self, estado, intervalo_s: float = 2.0) -> None:
        super().__init__(estado, "telemetria")
        self.intervalo_s = intervalo_s

    def cuerpo(self) -> None:
        e = self.estado
        while not e.fin.is_set():
            if self._dormir(self.intervalo_s):
                break
            with e.lock:
                cont = e.contadores.copia()
            log.info("TELEMETRIA %s", cont)


class HiloDemonioPeligroso(HiloSeguro):
    """Demonio DELIBERADAMENTE incorrecto. Solo con `--demo-daemon-peligroso`.

    Tres errores a proposito, los tres Bernstein/Tanenbaum clsicos:

      1. Ignora `estado.fin`: no comprueba nunca la senal de parada.
      2. Toma el `Lock` compartido mientras el interprete se desmonta.
      3. Escribe en el sistema de logging durante el apagado, cuando los
         handlers ya pueden estar cerrados.

    Un hilo demonio no puede colgar el proceso principal, pero si puede dejar
    un hilo a medio escribir, ensuciar la salida o lanzar excepciones durante
    el apagado. Por eso va detras de un flag y apagado por defecto.
    """

    daemon = True

    def __init__(self, estado, periodo_s: float = 0.3) -> None:
        super().__init__(estado, "demonio-peligroso")
        self.periodo_s = periodo_s

    def cuerpo(self) -> None:
        e = self.estado
        n = 0
        while True:  # <-- nunca consulta e.fin: ese es el error nº1
            n += 1
            with e.lock:  # <-- error nº2: lock compartido en pleno apagado
                cont = e.contadores.copia()
            time.sleep(self.periodo_s)
            log.warning("DEMONIO-PELIGROSO vuelta %d (ignora 'fin'): %s", n, cont)
