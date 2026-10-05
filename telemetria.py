"""Telemetria: configuracion de logging y volcado de los contadores.

El formato del log es el mismo que el del cuaderno `Hilos_en_Python.ipynb`
(celda 46): incluye `%(threadName)s`, que es justo lo que hace util el logging
cuando hay hilos, porque permite ver que hilo produjo cada linea.
"""

from __future__ import annotations

import logging
import sys

import config

LOG_FORMAT = "[%(levelname)-7s] %(threadName)-18s : %(message)s"
LOG_FORMAT_LARGO = "%(asctime)s [%(levelname)-7s] %(threadName)-18s : %(message)s"


def configurar(nivel_debug: bool = False, archivo: str | None = None) -> None:
    """Prepara el logging. Idempotente: se puede llamar varias veces."""
    nivel = logging.DEBUG if nivel_debug else logging.INFO
    root = logging.getLogger()
    root.setLevel(nivel)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = LOG_FORMAT_LARGO if nivel_debug else LOG_FORMAT
    consola = logging.StreamHandler(sys.stdout)
    consola.setFormatter(logging.Formatter(fmt))
    root.addHandler(consola)

    if archivo:
        fich = logging.FileHandler(archivo, encoding="utf-8")
        fich.setFormatter(logging.Formatter(LOG_FORMAT_LARGO))
        root.addHandler(fich)

    # SDL y pygame son ruidosos a nivel DEBUG.
    logging.getLogger("pygame").setLevel(logging.WARNING)


def resumen(estado) -> str:
    """Linea de una con el resumen de la partida."""
    with estado.lock:
        c = estado.contadores
        return (
            f"nivel={estado.nivel} resultado={estado.resultado} "
            f"puntaje={estado.puntaje} vidas={estado.vidas} "
            f"ticks={c.ticks} invasores_muertos={c.invasores_muertos}/"
            f"{len(estado.invasores)} bombas={c.bombas_lanzadas} "
            f"impactos={c.vidas_perdidas} ovnis={c.ovnis_derribados} "
            f"tick_max={c.tiempo_tick_max_ms}ms"
        )


def informe_ticks(estado) -> str:
    """Resumen de actividad por tick.

    Sirve para evidenciar que los 32 hilos + semaforos no rompen el ritmo: si el
    tick maximo se dispara, hay un problema de sincronizacion que investigar.
    """
    with estado.lock:
        c = estado.contadores
        ticks = max(1, c.ticks)
        return (
            f"ticks={c.ticks} "
            f"disparos_pedidos={c.disparos_pedidos} "
            f"rechazados_por_cupo={c.disparos_rechazados} "
            f"bombas_lanzadas={c.bombas_lanzadas} "
            f"balas_por_tick={c.disparos_aceptados / ticks:.2f} "
            f"(cupo={config.MAX_BALAS}) "
            f"tick_max={c.tiempo_tick_max_ms}ms"
        )
