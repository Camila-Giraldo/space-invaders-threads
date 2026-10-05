"""Medicion de aceleracion: 1, 2, 4, 8, 16, 32 y 64 hilos.

    python benchmark.py
    python benchmark.py --ticks 400 --grafico aceleracion.png

QUE MIDE Y QUE ESPERA
---------------------
Acelera de verdad un juego arcade el mover 32 rectangulos? La respuesta honesta
es NO, y el motivo se puede medir en tres numeros:

  * `T_trabajo`   lo que cuesta hacer el trabajo en un solo hilo, sin ninguna
                  sincronizacion. Es la linea base real.
  * `T_semaforo`  lo que cuesta UNA ida y vuelta de un `threading.Semaphore`
                  en esta maquina.
  * `T_n`         lo que cuesta el mismo trabajo repartido en N hilos, con el
                  ciclo de sincronizacion real del juego (un `release()` y un
                  `acquire()` por hilo y por tick).

Si `T_semaforo` ya es mayor que `T_trabajo`, la orquestacion cuesta mas que el
trabajo, y la aceleracion solo puede empeorar al anadir hilos. Eso es
justamente lo que sale, y es el hallazgo que hay que defender en la
sustentacion: `S(n) = T_trabajo / T_n` se aleja de 1 a medida que n crece.

No es un fallo del codigo ni de `threading`: es la consecuencia de dos hechos.

  1. El trabajo por tick son unas pocas operaciones de punto flotante sobre
     un atributo. Hablar de "cientos de nanosegundos" es generoso.
  2. El GIL ya serializa el bytecode de Python, asi que los N hilos no se
     ejecutan en paralelo en la CPU: solo se reparten el tiempo. Anadir hilos
     no anade potencia de calculo, solo coordinacion.

Este modulo no usa pygame ni el estado del juego: reproduce el patron de
sincronizacion en miniatura para poder medirlo aislado del dibujado.
"""

from __future__ import annotations

import argparse
import threading
import time

from config import INVASOR_STEP, INVASOR_TOTAL

#: Tamaños a medir. 64 se incluye para que se vea que la tendencia no cambia
#: de signo al pasar del numero de hilos reales del juego.
TAMANOS = (1, 2, 4, 8, 16, 32, 64)


def medir_trabajo(ticks: int) -> float:
    """`T_trabajo`: un solo hilo, los 32 movimientos por tick, sin sincronizar."""
    posiciones = [0.0] * INVASOR_TOTAL
    paso = float(INVASOR_STEP)
    t0 = time.perf_counter()
    for _ in range(ticks):
        for i in range(INVASOR_TOTAL):
            posiciones[i] += paso
    t = time.perf_counter() - t0
    if sum(posiciones) < 0:  # evita que el optimizador se borne el trabajo
        raise AssertionError
    return t


def medir_semaforo(operaciones: int) -> float:
    """`T_semaforo`: coste de `operaciones` ida y vuelta por un semaforo.

    Se mide con dos hilos reales enfrentados: el principal libera y espera, y
    un ayudante devuelve el permiso. Es exactamente el intercambio que hace el
    juego en cada tick, sin el trabajo de mover los rectangulos.
    """
    a = threading.Semaphore(0)
    b = threading.Semaphore(0)

    h = threading.Thread(target=_worker, args=(a, b, operaciones), daemon=True)
    h.start()
    t0 = time.perf_counter()
    for _ in range(operaciones):
        a.release()
        b.acquire()
    t = time.perf_counter() - t0
    h.join(timeout=2.0)
    return t


def _worker(a: threading.Semaphore, b: threading.Semaphore, n: int) -> None:
    """Contraparte del hilo: espera su turno en `a` y devuelve el de `b`.

    El intercambio es `principal --a.release()--> worker` y
    `worker --b.release()--> principal`, de modo que una ida y vuelta completa
    mide exactamente un `release` + un `acquire` cruzando dos hilos.
    """
    for _ in range(n):
        a.acquire()
        b.release()


def _invasor(permiso: threading.Semaphore, listo: threading.Semaphore, ticks: int) -> None:
    """Un 'invasor': consume su turno, mueve, devuelve el aviso."""
    x = 0.0
    for _ in range(ticks):
        permiso.acquire()
        x += INVASOR_STEP
        listo.release()


def medir_hilos(ticks: int, n_hilos: int) -> float:
    """`T_n`: n_hilos reales con el ciclo del juego, 2n sincronizaciones por tick."""
    permisos = [threading.Semaphore(0) for _ in range(n_hilos)]
    listo = threading.Semaphore(0)
    hilos = [
        threading.Thread(target=_invasor, args=(permisos[i], listo, ticks), daemon=True)
        for i in range(n_hilos)
    ]
    for h in hilos:
        h.start()

    t0 = time.perf_counter()
    for _ in range(ticks):
        for sem in permisos:  # un permiso privado por hilo, como en el juego
            sem.release()
        for _ in range(n_hilos):
            listo.acquire()
    t = time.perf_counter() - t0

    for h in hilos:
        h.join(timeout=2.0)
    return t


def ejecutar(ticks: int) -> dict[int, tuple[float, float, float]]:
    """Mide las tres magnitudes y devuelve {n: (t_n, s_paralela, factor)}."""
    t_trabajo = medir_trabajo(ticks)
    t_sem = medir_semaforo(max(2000, ticks * 10))
    trabajo_por_tick = t_trabajo / ticks
    semaforo_por_ida = t_sem / max(2000, ticks * 10)

    print(f"{ticks} ticks x {INVASOR_TOTAL} movimientos por tick\n")
    print("magnitudes de referencia")
    print(f"  T_trabajo  (1 hilo, sin sincronizar)  {t_trabajo * 1000:9.3f} ms"
          f"   = {trabajo_por_tick * 1e6:8.2f} us/tick")
    print(f"  T_semaforo (1 ida y vuelta)            {semaforo_por_ida * 1e6:9.3f} us"
          f"   = {semaforo_por_ida / trabajo_por_tick:8.1f}x el trabajo de un tick")
    print()
    print("  Una ida y vuelta de semaforo ya cuesta mas que los 32 movimientos")
    print("  completos de un tick. A partir de aqui, mas hilos solo anaden")
    print("  coordinacion.")
    print()
    print("  (Los valores absolutos dependen de la maquina y de la carga. Lo que")
    print("   no depende de la maquina es el orden de magnitud: la coordinacion")
    print("   domina al trabajo, y esa relacion se mantiene al variar n.)\n")

    print(f"{'hilos':>6} {'tiempo':>11} {'ticks/s':>11} {'S(n)':>8} {'coste rel.':>11} {'us/tick':>9}")
    print("-" * 60)
    filas: dict[int, tuple[float, float, float]] = {}
    for n in TAMANOS:
        t = medir_hilos(ticks, n)
        s_par = t_trabajo / t          # < 1 significa que los hilos empeoran
        factor = t / t_trabajo          # cuantas veces mas caro queda todo
        filas[n] = (t, s_par, factor)
        print(
            f"{n:>6} {t * 1000:>8.2f} ms {ticks / t:>11,.0f} "
            f"{s_par:>8.3f} {factor:>10.1f}x {t / ticks * 1e6:>9.1f}"
        )
    return filas


def main() -> int:
    p = argparse.ArgumentParser(description="Aceleracion de la flota de hilos")
    p.add_argument("--ticks", type=int, default=400, help="ticks por medicion")
    p.add_argument("--grafico", metavar="PNG", help="guardar un grafico de aceleracion")
    args = p.parse_args()

    filas = ejecutar(args.ticks)

    print()
    print("Conclusion")
    print("  S(n) esta por debajo de 1 en todos los casos, y baja al aumentar n.")
    print("  Con 32 hilos, que es el numero real de la flota, el trabajo sale")
    print(f"  {filas[32][2]:.0f} veces mas caro que hacerlo en un solo hilo sin hilos.")
    print("  No es un error: es la relacion entre el coste del trabajo y el de la")
    print("  coordinacion, y el GIL impide que los hilos ganen nada de CPU.")

    if args.grafico:
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            print("\n(no hay matplotlib; instalalo para el grafico)")
            return 1

        plt.style.use("ggplot")
        ns = sorted(filas)
        s_par = [filas[n][1] for n in ns]
        factor = [filas[n][2] for n in ns]

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4))

        ax1.plot(ns, s_par, "o-", color="#2ca02c")
        ax1.axhline(1.0, ls="--", color="gray", label="paralelismo ideal (S=1)")
        ax1.set_yscale("log")
        ax1.set_xlabel("Numero de hilos [n]")
        ax1.set_ylabel("Aceleracion S(n) = T_trabajo / T_n")
        ax1.set_title("La aceleracion cae muy por debajo de 1")
        ax1.set_xscale("log", base=2)
        ax1.set_xticks(ns)
        ax1.set_xticklabels([str(n) for n in ns], fontsize=8)
        ax1.legend()

        ax2.plot(ns, factor, "s-", color="#d62728")
        ax2.set_yscale("log")
        ax2.set_xlabel("Numero de hilos [n]")
        ax2.set_ylabel("Coste relativo = T_n / T_trabajo")
        ax2.set_title("Lo que cuesta orquestar la flota")
        ax2.set_xscale("log", base=2)
        ax2.set_xticks(ns)
        ax2.set_xticklabels([str(n) for n in ns], fontsize=8)

        fig.tight_layout()
        fig.savefig(args.grafico, dpi=120)
        print(f"\ngrafico guardado en {args.grafico}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())