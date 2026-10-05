"""Punto de entrada: bucle de pygame, teclado y apagado.

Este modulo es deliberadamente tonto: lee el teclado, pide ticks a
`Partida` y dibuja el `Snapshot`. Ninguna regla de juego vive aqui, y nada de
lo que hay aqui bloquea a los hilos: el dibujado ocurre sin el lock.

    python main.py                       partida normal de 3 rondas
    python main.py --seed 42             partida reproducible
    python main.py --niveles 5           5 rondas
    python main.py --debug               log a nivel DEBUG, con fecha
    python main.py --log partida.log     vuelca tambien a un fichero
    python main.py --retraso-invasor 1300 test de estres de sincronizacion
    python main.py --demo-daemon-peligroso   demostracion opt-in
"""

from __future__ import annotations

import argparse
import logging
import sys

# El driver SDL se fija antes de importar pygame para poder correr sin ventana.
if "--headless" in sys.argv:
    import os

    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame  # noqa: E402  (import tardio a proposito)

import config as C  # noqa: E402
import render  # noqa: E402
import telemetria  # noqa: E402
from partida import Partida  # noqa: E402

log = logging.getLogger("main")

AVISO_RONDA_MS = 1800  # tiempo en pantalla del aviso de ronda superada


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Space Invaders concurrente (hilos, semaforos, barreras y demonios)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--seed", type=int, default=0, help="semilla del generador aleatorio")
    p.add_argument("--niveles", type=int, default=C.NIVELES_POR_DEFECTO, help="rondas para ganar")
    p.add_argument("--debug", action="store_true", help="log a nivel DEBUG")
    p.add_argument("--log", metavar="ARCHIVO", help="volcar tambien el log a un fichero")
    p.add_argument(
        "--retraso-invasor",
        type=float,
        default=0.0,
        metavar="MS",
        help="retraso artificial en un hilo de invasor (test de estres)",
    )
    p.add_argument(
        "--demo-daemon-peligroso",
        action="store_true",
        help="lanzar el demonio incorrecto de ejemplo (opt-in, puede ensuciar la salida)",
    )
    p.add_argument("--sin-telemetria", action="store_true", help="no lanzar el hilo demonio de telemetria")
    p.add_argument("--headless", action="store_true", help="sin ventana, para CI y capturas")
    p.add_argument("--frames", type=int, default=0, help="parar tras N frames (0 = infinito)")
    return p.parse_args(argv)


def nueva_partida(args: argparse.Namespace) -> Partida:
    return Partida(
        seed=args.seed,
        niveles=args.niveles,
        retraso_invasor_ms=args.retraso_invasor,
        demonio_peligroso=args.demo_daemon_peligroso,
        telemetria=not args.sin_telemetria,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    telemetria.configurar(nivel_debug=args.debug, archivo=args.log)

    # Solo se inicializan los modulos que el juego usa de verdad: display y
    # font. `pygame.init()` inicializaria tambien el mixer, y en una maquina
    # sin tarjeta de sonido ALSA se queda reintentando en un bucle que puede
    # bloquear el arranque. Este juego no tiene audio.
    pygame.display.init()
    pygame.font.init()
    pantalla = pygame.display.set_mode((C.WIDTH, C.HEIGHT))
    pygame.display.set_caption("Space Invaders concurrente - hilos y sincronizacion")
    reloj = pygame.time.Clock()
    render.init(pantalla)

    partida = nueva_partida(args)
    partida.arrancar()
    log.info("controles: <- -> moverse | ESPACIO disparar | P pausa | R reiniciar | ESC salir")

    corriendo = True
    frames = 0
    aviso_ronda_desde = 0  # 0 = no hay aviso en pantalla

    try:
        while corriendo:
            for evento in pygame.event.get():
                if evento.type == pygame.QUIT:
                    corriendo = False
                elif evento.type == pygame.KEYDOWN:
                    if evento.key == pygame.K_ESCAPE:
                        corriendo = False
                    elif evento.key == pygame.K_r and partida.terminada:
                        log.info("reiniciando...")
                        partida.apagar()
                        partida = nueva_partida(args)
                        partida.arrancar()
                        aviso_ronda_desde = 0
                    elif evento.key == pygame.K_p and not partida.terminada:
                        log.info("pausa=%s", partida.alternar_pausa())
                    elif evento.key == pygame.K_SPACE and not partida.terminada:
                        # Disparo por pulsacion, como en el arcade. El cupo lo
                        # impone el semaforo `balas_sem`; si esta lleno, la
                        # pulsacion se descarta (no se encola).
                        partida.disparar()

            if not partida.terminada:
                teclas = pygame.key.get_pressed()
                dx = int(bool(teclas[pygame.K_RIGHT])) - int(bool(teclas[pygame.K_LEFT]))
                if dx:
                    partida.mover_jugador(dx * C.JUGADOR_PX_POR_FRAME)

                if partida.tick_pendiente():
                    partida.tick()

            # Aviso de ronda superada: se congela la simulacion un momento.
            if partida.entre_rondas:
                if aviso_ronda_desde == 0:
                    aviso_ronda_desde = pygame.time.get_ticks()
                    log.info("ronda %d completada, preparando la siguiente...", partida.estado.nivel)
                elif pygame.time.get_ticks() - aviso_ronda_desde > AVISO_RONDA_MS:
                    aviso_ronda_desde = 0
                    partida.avanzar_nivel()
            else:
                aviso_ronda_desde = 0

            # El reloj del juego lo escribe el principal una vez por frame; lo
            # leen los hilos de bomba para saber si el jugador es invulnerable.
            with partida.estado.lock:
                partida.estado.tiempo_ms = pygame.time.get_ticks()

            s = render.tomar_snapshot(partida.estado)
            render.dibujar(pantalla, s)
            # El estado de la superposicion se lee del snapshot, no del estado
            # vivo: asi el render no necesita el lock y no hay dos lecturas
            # distintas del mismo frame.
            if s.nivel_superado or s.resultado != "jugando" or s.pausado:
                render.superposicion(pantalla, s)

            # El flip va despues de la superposicion, no dentro de `dibujar`.
            pygame.display.flip()
            reloj.tick(C.FPS)

            frames += 1
            if args.frames and frames >= args.frames:
                log.info("alcanzado el limite de %d frames", args.frames)
                break
    finally:
        partida.apagar()
        log.info("resumen: %s", telemetria.resumen(partida.estado))
        log.info("ticks:    %s", telemetria.informe_ticks(partida.estado))
        pygame.quit()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
