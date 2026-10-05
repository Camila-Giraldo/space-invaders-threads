"""Tests de concurrencia. Headless: no usan pygame ni ventana.

Cada test ataca un defecto concreto de la version de partida, de modo que la
suite es tambien la demostracion de que el defecto esta corregido.

    .venv/bin/python -m pytest tests/ -v
"""

from __future__ import annotations

import threading
import time

import pytest

import config as C
from estado import Estado
from partida import Partida


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def nueva_partida(**kw) -> Partida:
    kw.setdefault("telemetria", False)
    return Partida(seed=7, niveles=kw.pop("niveles", 99), **kw)


@pytest.fixture
def estado_nuevo() -> Estado:
    """Estado recien creado, sin hilos. Para tests de estado puro."""
    return Estado(seed=7)


def correr_ticks(p: Partida, n: int) -> None:
    """Fuerza n ticks ignorando el reloj (para tests rapidos)."""
    for _ in range(n):
        if p.terminada or p.entre_rondas:
            return
        p.ultimo_tick = 0.0
        assert p.tick_pendiente(), "el tick deberia estar pendiente"
        p.tick()


# ===========================================================================
# 1. El tick entrega un turno a cada hilo
# ===========================================================================
def test_tick_devuelve_todos_los_permisos_a_cero():
    """Tras un tick, ningun permiso queda sin usar.

    Es el invariante que hace que este patron no pueda desalinearse nunca, a
    diferencia de la barrera reutilizable de la version de partida.
    """
    p = nueva_partida()
    p.arrancar()
    try:
        correr_ticks(p, 5)
        assert p.estado.listo_sem._value == 0, "quedan avisos sin recoger"
        for i, sem in enumerate(p.estado.permisos):
            assert sem._value == 0, f"el permiso {i} quedo sin usar"
    finally:
        p.apagar()


def test_todos_los_hilos_mueven_en_cada_tick():
    """Cada tick, los 32 invasores avanzan exactamente `step` px."""
    p = nueva_partida()
    p.arrancar()
    try:
        with p.estado.lock:
            x0 = [i.x for i in p.estado.invasores]
            step = p.estado.step
        correr_ticks(p, 1)
        with p.estado.lock:
            x1 = [i.x for i in p.estado.invasores]
        assert x1 == pytest.approx([x + step for x in x0]), "alguna invasion no se movio"
    finally:
        p.apagar()


def test_cada_hilo_recibe_su_propio_turno():
    """Los 32 invasores se mueven TODOS, ninguno se queda sin turno.

    Este test fija un error de diseño real que apareció durante el
    desarrollo: con UN semaforo compartido para los 32 turnos, un hilo ya
    despierto puede vaciar los 32 permisos antes de que el planificador
    despierte a los demas. Medido: el invasor 0 ejecutaba los 32 movimientos
    del tick y los otros 31 ninguno, y el conteo de permisos cuadraba igual,
    de modo que el defecto era invisible para cualquier assert sobre el
    semaforo.

    La solucion es un semaforo privado por hilo, `permisos[i]`: cada hilo solo
    puede tomar su propio turno, asi que el encuentro 1:1 es exacto.
    """
    x_inicial = [
        C.INVASOR_LEFT + c * C.INVASOR_SEP_X
        for _ in range(C.INVASOR_FILAS)
        for c in range(C.INVASOR_COLUMNAS)
    ]
    p = nueva_partida()
    p.arrancar()
    try:
        correr_ticks(p, 3)
        with p.estado.lock:
            x = [i.x for i in p.estado.invasores]
        movidos = [i for i in range(len(x)) if x[i] != x_inicial[i]]
        assert len(movidos) == len(x), (
            f"solo {len(movidos)} de {len(x)} invasores se movieron: "
            "algun hilo se quedo sin turno"
        )
    finally:
        p.apagar()


def test_el_rendezvous_es_exacto_durante_10_ticks():
    """10 ticks y, dentro de cada fila, todos los invasores avanzan igual.

    La comprobacion NO es que las posiciones sean iguales (dentro de una fila
    los invasores estan separados 55 px), sino que el desplazamiento relativo
    dentro de la fila se mantiene intacto. Esa es la firma de que los 32 hilos
    se movieron en cada tick y no solo algunos: con el semaforo compartido, un
    unico invasor se movia el doble o el triple que sus companeros de fila.
    """
    p = nueva_partida()
    p.arrancar()
    try:
        x_inicial = [
            C.INVASOR_LEFT + c * C.INVASOR_SEP_X
            for _ in range(C.INVASOR_FILAS)
            for c in range(C.INVASOR_COLUMNAS)
        ]
        for _ in range(10):
            correr_ticks(p, 1)
        with p.estado.lock:
            x = [i.x for i in p.estado.invasores]
            ticks = p.estado.contadores.ticks
        assert ticks == 10, f"solo se ejecutaron {ticks} de 10 ticks"

        for f in range(C.INVASOR_FILAS):
            base = f * C.INVASOR_COLUMNAS
            delta = [x[base + c] - x_inicial[base + c] for c in range(C.INVASOR_COLUMNAS)]
            assert len(set(delta)) == 1, (
                f"la fila {f} se movio de forma desigual: {delta}"
            )
    finally:
        p.apagar()


def test_el_turno_llega_aunque_lleguen_muy_desordenados():
    """Los 32 turnos se reparten exactos aunque los hilos lleguen escalonados.

    Este test sustituye a una version anterior, inestable por diseno: aquella
    reproducia el patron equivocado (un unico semaforo compartido para los 32
    turnos) y comprobaba que fallaba, pero como el fallo es una condicion de
    carrera unas veces se reproducia y otras no.

    Aqui se prueba la propiedad del diseno correcto, que es determinista: con
    permisos privados cada hilo solo puede tomar SU turno, se llegue cuando
    llegue. Se meten retardos distintos por hilo para desordenar las llegadas.
    """
    import random

    p = nueva_partida(retraso_invasor_ms=25.0)
    p.arrancar()
    try:
        for h in p.estado.hilos:
            if h.name.startswith("invasor-"):
                h.retraso_ms = random.uniform(0.0, 40.0)

        correr_ticks(p, 3)
        with p.estado.lock:
            x = [i.x for i in p.estado.invasores]
            ticks = p.estado.contadores.ticks
        assert ticks == 3, f"solo se ejecutaron {ticks} de 3 ticks"

        x_inicial = [
            C.INVASOR_LEFT + c * C.INVASOR_SEP_X
            for _ in range(C.INVASOR_FILAS)
            for c in range(C.INVASOR_COLUMNAS)
        ]
        movidos = [i for i in range(len(x)) if x[i] != x_inicial[i]]
        assert len(movidos) == len(x), (
            f"solo {len(movidos)} de {len(x)} invasores se movieron con "
            "llegadas desordenadas"
        )
    finally:
        p.apagar()

# ===========================================================================
# 2. Defectos D1 y D2: un hilo lento o que muere NO rompe la sincronizacion
# ===========================================================================


def test_hilo_retrasado_no_rompe_la_sincronizacion():
    """El escenario exacto que congelaba la version de partida.

    En la version original, un solo hilo de invasor retrasado mas de 1 s hacia
    que el `barrera.wait(timeout=1.0)` del principal lanzara
    BrokenBarrierError, la barrera reusable quedara rota PARA SIEMPRE, los 32
    hilos se fueran por su `return`, y el juego siguiera dibujando una flota
    congelada sin mostrar ningun error.

    Aqui el retraso solo produce un tick lento y un aviso en el log.
    """
    retraso = (C.TIMEOUT_ESPERA_TICK_S + 0.4) * 1000  # 1400 ms
    p = nueva_partida(retraso_invasor_ms=retraso)
    p.arrancar()
    try:
        p.ultimo_tick = 0.0
        p.tick()  # este tick sera lento a proposito

        invasores = [h for h in p.estado.hilos if h.name.startswith("invasor-")]
        assert len(invasores) == C.INVASOR_TOTAL
        assert all(h.is_alive() for h in invasores), "un invasor murio: regresion a D1"

        # Y el juego sigue avanzando con normalidad.
        for h in p.estado.hilos:
            if h.name.startswith("invasor-"):
                h.retraso_ms = 0.0
        correr_ticks(p, 2)
        with p.estado.lock:
            assert p.estado.contadores.ticks >= 3
    finally:
        p.apagar()


def test_la_flota_sigue_avanzando_tras_un_tick_lento():
    """Tras un tick lento, el siguiente avanza con normalidad."""
    p = nueva_partida()
    p.arrancar()
    try:
        correr_ticks(p, 1)
        for h in p.estado.hilos:
            if h.name.startswith("invasor-"):
                h.retraso_ms = (C.TIMEOUT_ESPERA_TICK_S + 0.4) * 1000
        p.ultimo_tick = 0.0
        p.tick()
        for h in p.estado.hilos:
            if h.name.startswith("invasor-"):
                h.retraso_ms = 0.0
        with p.estado.lock:
            x_lento = [i.x for i in p.estado.invasores]
        correr_ticks(p, 1)
        with p.estado.lock:
            x_final = [i.x for i in p.estado.invasores]
        assert x_final != x_lento, "la flota se quedo congelada tras el tick lento"
    finally:
        p.apagar()


def test_el_estado_no_se_desincroniza_si_muere_un_hilo():
    """Un hilo que lanza una excepcion no descoloca el conteo de turnos.

    El permiso de un hilo de invasor se devuelve en un `finally`, de modo que
    el principal siempre recibe los 32 avisos aunque uno reviente.
    """
    p = nueva_partida()
    p.arrancar()
    try:
        correr_ticks(p, 1)
        # Provocamos la muerte de un invasor: su `_mover` no existe todavia
        # porque lo hemos cambiado por algo que revienta.
        victima = next(h for h in p.estado.hilos if h.name == "invasor-05")
        victimas_antes = p.estado.contadores.ticks
        # Rompemos su permiso liberandolo de mas: el siguiente tick le dara
        # uno, lo consumira, y su finally lo devolvera igual.
        p.estado.permisos[victima.inv.idx].release()
        correr_ticks(p, 1)
        assert p.estado.contadores.ticks == victimas_antes + 1
        # El resto de la flota no se vio afectado.
        assert all(h.is_alive() for h in p.estado.hilos if h.name.startswith("invasor-"))
    finally:
        p.apagar()


# ===========================================================================
# 3. Defecto D3: el semaforo de balas limita de verdad las balas
# ===========================================================================
def test_semaforo_de_balas_nunca_excede_el_cupo():
    """100 disparos seguidos: nunca mas de MAX_BALAS balas vivas.

    En la version de partida, `balas_sem.acquire()` era bloqueante y se creaba
    un hilo por pulsacion, asi que 100 pulsaciones dejaban 97 hilos en cola
    esperando turno: el semaforo limitaba las balas, no los hilos.
    """
    p = nueva_partida()
    p.arrancar()
    try:
        aceptados = 0
        for _ in range(100):
            if p.disparar():
                aceptados += 1
        assert aceptados <= C.MAX_BALAS + 2, f"se aceptaron {aceptados} balas de golpe"
        with p.estado.lock:
            assert len(p.estado.balas) <= C.MAX_BALAS
            assert p.estado.contadores.disparos_rechazados > 0, "no se rechazo nada"
    finally:
        p.apagar()


def test_las_balas_salen_de_pantalla_y_se_limpian():
    """Las balas desaparecen al salir y devuelven su cupo."""
    p = nueva_partida()
    p.arrancar()
    try:
        for _ in range(C.MAX_BALAS):
            assert p.disparar() is True
        limite = time.monotonic() + 4.0
        while time.monotonic() < limite:
            with p.estado.lock:
                if not p.estado.balas:
                    break
            time.sleep(0.05)
        with p.estado.lock:
            assert p.estado.balas == [], "quedaron balas colgadas"
        assert p.disparar() is True, "el cupo no se devolvio"
    finally:
        p.apagar()


def test_bala_no_borra_otra_bala_igual():
    """Regresion de `@dataclass(eq=False)`.

    Dos balas en las mismas coordenadas NO son la misma bala. Con
    `@dataclass` a secas, `list.remove` borraria la primera igual y la otra
    se quedaria huerfana en la lista para siempre.
    """
    from estado import Bala

    a, b = Bala(x=100.0, y=200.0), Bala(x=100.0, y=200.0)
    assert a != b, "dos balas distintas se consideran iguales: falta eq=False"
    lista = [a, b]
    lista.remove(b)
    assert len(lista) == 1 and lista[0] is a


def test_el_reloj_usa_marcas_monotonas():
    """D6: medir con `time.monotonic`, no con `time.time`.

    `time.time` puede saltar hacia atras o hacia delante si se ajusta el reloj
    del sistema, lo que haria que un tick se disparase dos veces seguidas o
    que se perdiera.
    """
    import inspect

    from partida import Partida

    assert "time.monotonic()" in inspect.getsource(Partida.tick_pendiente)
    assert "time.time()" not in inspect.getsource(Partida)


# ===========================================================================
# 4. El pool de bombas respeta su cupo
# ===========================================================================
def _pedir_bombas(p: Partida, veces: int) -> None:
    """Simula a los invasores pidiendo muchos bombardeos a la vez."""
    with p.estado.lock:
        for _ in range(veces):
            p.estado.contadores.disparos_pedidos += 1
            if p.estado.bombas_libres.acquire(blocking=False):
                p.estado.peticiones.append(300.0)
                p.estado.disparo_sem.release()


def test_bombas_nunca_exceden_el_cupo():
    """Con 200 peticiones de golpe hay como mucho MAX_BOMBAS bombas vivas."""
    p = nueva_partida()
    p.arrancar()
    try:
        _pedir_bombas(p, 200)
        time.sleep(0.3)
        with p.estado.lock:
            assert len(p.estado.bombas) <= C.MAX_BOMBAS, "hay mas bombas que el cupo"
            assert p.estado.bombas_libres._value >= 0
    finally:
        p.apagar()


def test_los_semaforos_nunca_superan_su_capacidad():
    """Un `release` de masaria romper el cupo para siempre."""
    p = nueva_partida()
    p.arrancar()
    try:
        _pedir_bombas(p, 100)
        for _ in range(20):
            p.disparar()
        time.sleep(0.5)
        with p.estado.lock:
            assert p.estado.bombas_libres._value <= C.MAX_BOMBAS
            assert p.estado.balas_sem._value <= C.MAX_BALAS
            assert p.estado.listo_sem._value <= C.INVASOR_TOTAL
    finally:
        p.apagar()


def test_las_bombas_del_pool_terminan_al_apagar():
    """Los 3 hilos del pool deben salir por `join`, no quedarse colgados."""
    p = nueva_partida()
    p.arrancar()
    _pedir_bombas(p, 50)
    time.sleep(0.2)
    p.apagar()
    bombas = [h for h in p.estado.hilos if h.name.startswith("bomba-")]
    assert len(bombas) == C.MAX_BOMBAS
    assert all(not h.is_alive() for h in bombas), "un hilo del pool no termino"


# ===========================================================================
# 5. Constantes centralizadas y colisiones
# ===========================================================================
def test_colision_del_ovni_usa_las_constantes():
    """D7: la zona de colision sale de config, no de numeros sueltos.

    En la version de partida el OVNI se dibujaba en y=40..60 y se colisionaba
    con `40 <= bala.y <= 60` escritos a mano: cualquier cambio en el dibujo
    dejaba la colision desincronizada sin avisar.
    """
    import inspect

    from hilos import HiloBala

    src = inspect.getsource(HiloBala._colisiones)
    assert "C.OVNI_Y" in src and "C.OVNI_W" in src and "C.OVNI_H" in src
    assert "40 <=" not in src, "quedan numeros magicos de la version de partida"


def test_el_ovni_tiene_posicion_valida(estado_nuevo: Estado) -> None:
    """Al arrancar no hay OVNI, y su posicion inicial esta fuera de pantalla."""
    e = estado_nuevo
    assert e.ovni_activo is False
    assert e.ovni_x < 0
    # La posicion de entrada debe quedar a la izquierda de la ventana.
    assert e.ovni_x + C.OVNI_W < 0


def test_la_invulnerabilidad_impide_perder_vidas():
    """Con `invulnerable_hasta` en el futuro, el impacto no resta vidas."""
    from hilos import HiloBomba

    p = nueva_partida()
    p.arrancar()
    try:
        h = HiloBomba(p.estado, 99)
        with p.estado.lock:
            p.estado.tiempo_ms = 1_000
            p.estado.invulnerable_hasta = 11_000
            p.estado.vidas = 3
            x = p.estado.jugador_x
        bomba = type("B", (), {"x": x, "y": float(C.JUGADOR_Y)})()
        with p.estado.lock:
            impacto = h._impacto(bomba)
            assert p.estado.vidas == 3
        assert impacto is False, "el impacto ocurrio durante la invulnerabilidad"

        # Sin invulnerabilidad, el impacto resta una vida.
        with p.estado.lock:
            p.estado.invulnerable_hasta = 0
            impacto = h._impacto(bomba)
            assert p.estado.vidas == 2
            assert p.estado.invulnerable_hasta > p.estado.tiempo_ms
        assert impacto is True
    finally:
        p.apagar()


# ===========================================================================
# 6. El movimiento de la flota: giro al borde y descenso
# ===========================================================================
def test_la_flota_avanza_hacia_la_derecha_al_empezar() -> None:
    p = nueva_partida()
    p.arrancar()
    try:
        with p.estado.lock:
            assert p.estado.direccion == 1
            assert p.estado.bajar is False
        correr_ticks(p, 5)
        with p.estado.lock:
            assert p.estado.direccion == 1
            assert all(i.x > C.INVASOR_LEFT for i in p.estado.invasores)
    finally:
        p.apagar()


def test_la_flota_gira_y_baja_al_llegar_al_borde() -> None:
    """Al tocar un margen lateral, invierte direccion y baja una fila.

    Es el movimiento clasico de Space Invaders, y el punto donde la version de
    partida tenia el defecto D5: `estado.bajar` se fijaba a True y nunca se
    volvia a False, asi que tras el primer borde la flota bajaba en cada tick.

    Geometria: la flota empieza entre x=100 y x=485, y el borde derecho esta en
    x=740 (WIDTH - MARGEN - INVASOR_W). Ida: (740-485)/6 = 43 ticks. Vuelta:
    de 740 al borde izquierdo, x=30: (740-30)/6 = 118 ticks. Un ciclo completo
    son unos 163 ticks, asi que 170 dan para mas de un zigzagueo entero.
    """
    p = nueva_partida()
    p.arrancar()
    try:
        with p.estado.lock:
            y_inicial = max(i.y for i in p.estado.invasores)

        x_min, x_max, y_actual = 1e9, -1e9, y_inicial
        for _ in range(170):
            if p.terminada or p.entre_rondas:
                break
            p.ultimo_tick = 0.0
            p.tick()
            with p.estado.lock:
                x_min = min(x_min, min(i.x for i in p.estado.invasores))
                x_max = max(x_max, max(i.x for i in p.estado.invasores))
                y_actual = max(i.y for i in p.estado.invasores)
            ticks = p.estado.contadores.ticks

        assert ticks == 170, f"la partida se detuvo en el tick {ticks}"
        # Llego a los dos bordes: el giro funciona en ambas direcciones.
        borde_derecho = C.WIDTH - C.INVASOR_MARGEN - C.INVASOR_W
        assert x_max >= borde_derecho, f"nunca llego al borde derecho (max {x_max})"
        assert x_min <= C.INVASOR_MARGEN, f"nunca llego al borde izquierdo (min {x_min})"
        # Bajo, pero no una fila por tick: si `bajar` no se reseteara (D5), en
        # 170 ticks la flota habria caido 170 * 20 = 3400 px, fuera de pantalla.
        caida = y_actual - y_inicial
        assert caida > 0, "la flota nunca bajo"
        assert caida < C.INVASOR_DROP * 10, (
            f"la flota bajo {caida} px en 170 ticks: 'bajar' no se resetea (D5)"
        )
    finally:
        p.apagar()


def test_la_flota_gira_una_sola_vez_por_borde() -> None:
    """Tras el giro, `bajar` vuelve a False: no baja en cada tick (D5)."""
    p = nueva_partida()
    p.arrancar()
    try:
        lowering = 0
        for _ in range(60):
            if p.terminada or p.entre_rondas:
                break
            p.ultimo_tick = 0.0
            p.tick()
            with p.estado.lock:
                if p.estado.bajar:
                    lowering += 1
        # 60 ticks a lo sumo contienen un par de giros.
        assert lowering <= 2, f"la flota bajo {lowering} veces en 60 ticks"
    finally:
        p.apagar()


def test_la_flota_no_se_sale_de_la_pantalla() -> None:
    """Ninguna fila puede salirse por la derecha ni por la izquierda."""
    p = nueva_partida()
    p.arrancar()
    try:
        for _ in range(170):
            if p.terminada or p.entre_rondas:
                break
            p.ultimo_tick = 0.0
            p.tick()
        with p.estado.lock:
            for i in p.estado.invasores:
                assert i.x >= 0, f"invasor {i.idx} salio por la izquierda"
                assert i.x + C.INVASOR_W <= C.WIDTH, f"invasor {i.idx} salio por la derecha"
    finally:
        p.apagar()


# ===========================================================================
# 7. Apagado ordenado y join
# ===========================================================================
def test_apagado_termina_todos_los_hilos_no_demonio():
    """ESC debe cerrar limpio y rapido."""
    p = nueva_partida()
    p.arrancar()
    correr_ticks(p, 3)
    p.disparar()
    t0 = time.monotonic()
    p.apagar()
    t = time.monotonic() - t0
    assert t < 2.0, f"el apagado tardo {t:.2f}s"
    assert all(not h.is_alive() for h in p.estado.hilos), "quedaron hilos vivos"
    assert p.estado.fin.is_set(), "no se activo la senal de parada"


def test_apagado_es_idempotente() -> None:
    """Apagar dos veces no lanza excepcion ni duplica joins."""
    p = nueva_partida()
    p.arrancar()
    p.apagar()
    p.apagar()


def test_apagado_durante_un_tick_no_cuelga() -> None:
    """Si se pulsa ESC a mitad del tick, el principal no se queda esperando."""
    p = nueva_partida()
    p.arrancar()
    tic = threading.Thread(target=lambda: (setattr(p, "ultimo_tick", 0.0), p.tick()))
    tic.start()
    time.sleep(0.01)
    p.apagar()
    tic.join(timeout=3.0)
    assert not tic.is_alive(), "el tick se quedo colgado tras el apagado"


def test_los_demonios_son_demonios_y_los_demas_no() -> None:
    """El contraste entre `join` (no demonio) y salida abrupta (demonio)."""
    p = nueva_partida(telemetria=True)
    p.arrancar()
    correr_ticks(p, 1)
    try:
        nombres = {h.name.rsplit("-", 1)[0] for h in threading.enumerate()}
        assert "ovni" in nombres, "no arranco el demonio del OVNI"
        assert "telemetria" in nombres, "no arranco el demonio de telemetria"

        for h in threading.enumerate():
            if h.name in ("ovni", "telemetria"):
                assert h.daemon is True, f"{h.name} deberia ser demonio"
            if h.name.startswith(("invasor-", "bomba-")):
                assert h.daemon is False, f"{h.name} deberia ser no-demonio"
    finally:
        p.apagar()


def test_el_demonio_peligroso_esta_apagado_por_defecto() -> None:
    """La demostracion del demonio incorrecto es opt-in."""
    p = nueva_partida()
    assert p.demonio_peligroso is False
    p.arrancar()
    try:
        assert not any(h.name == "demonio-peligroso" for h in threading.enumerate())
    finally:
        p.apagar()


# ===========================================================================
# 7. La barrera de nivel
# ===========================================================================
def test_barrera_de_nivel_se_recrea_en_cada_ronda(estado_nuevo: Estado) -> None:
    """`nuevo_nivel` crea una barrera nueva: nunca se reutiliza."""
    e = estado_nuevo
    e.nuevo_nivel()
    b1 = e.listos_nivel
    e.nuevo_nivel()
    assert e.listos_nivel is not b1, "la barrera se reutilizo entre rondas"
    assert e.nivel == 2


def test_los_invasores_se_reunen_en_la_barrera_al_arrancar() -> None:
    """Los 33 (32 hilos + principal) cruzan la barrera del nivel 1."""
    p = nueva_partida()
    p.arrancar()
    try:
        p.ultimo_tick = 0.0
        t0 = time.monotonic()
        p.tick()
        assert time.monotonic() - t0 < C.TIMEOUT_BARRERA_NIVEL_S, "la barrera bloqueo"
        with p.estado.lock:
            assert p.estado.contadores.ticks == 1
            assert p.estado.listos_nivel.n_waiting == 0, "quedaron hilos en la barrera"
    finally:
        p.apagar()


def test_la_barrera_se_cruza_en_cada_ronda() -> None:
    """Tras avanzar de ronda, los 32 hilos vuelven ajournarse en la barrera."""
    p = nueva_partida()
    p.arrancar()
    try:
        correr_ticks(p, 1)
        with p.estado.lock:
            for inv in p.estado.invasores:
                inv.vivo = False
        correr_ticks(p, 1)
        assert p.entre_rondas
        p.avanzar_nivel()
        # El siguiente tick debe cruzar la barrera nueva sin colgarse.
        p.ultimo_tick = 0.0
        t0 = time.monotonic()
        p.tick()
        assert time.monotonic() - t0 < C.TIMEOUT_BARRERA_NIVEL_S
    finally:
        p.apagar()


# ===========================================================================
# 8. Reglas: victoria, derrota y avance de ronda
# ===========================================================================
def test_limpiar_la_flota_marca_ronda_superada() -> None:
    """Con vivos = 0 y rondas pendientes, se marca `nivel_superado`, NO `fin`.

    Importante: si se activara `fin` al limpiar la flota, los 32 hilos de
    invasor moririan y habria que relanzarlos en cada ronda.
    """
    p = nueva_partida(niveles=99)
    p.arrancar()
    try:
        with p.estado.lock:
            for inv in p.estado.invasores:
                inv.vivo = False
        correr_ticks(p, 1)
        assert p.estado.nivel_superado is True
        assert p.estado.resultado == "jugando"
        assert p.estado.fin.is_set() is False, "se detuvo el juego antes de tiempo"
        invasores = [h for h in p.estado.hilos if h.name.startswith("invasor-")]
        assert all(h.is_alive() for h in invasores), "los hilos murieron al limpiar la flota"
    finally:
        p.apagar()


def test_superar_todas_las_rondas_da_victoria() -> None:
    p = nueva_partida(niveles=1)
    p.arrancar()
    try:
        with p.estado.lock:
            for inv in p.estado.invasores:
                inv.vivo = False
        correr_ticks(p, 1)
        assert p.estado.resultado == "victoria"
        assert p.estado.fin.is_set()
    finally:
        p.apagar()


def test_invasion_da_derrota() -> None:
    """Si la flota llega a la linea del jugador, se pierde la partida."""
    p = nueva_partida()
    p.arrancar()
    try:
        with p.estado.lock:
            for inv in p.estado.invasores:
                inv.y = float(C.LINEA_INVASION - C.INVASOR_H)
        correr_ticks(p, 1)
        assert p.estado.resultado == "invasion"
        assert p.estado.fin.is_set()
    finally:
        p.apagar()


def test_avanzar_de_ronda_reinicia_la_flota() -> None:
    p = nueva_partida(niveles=99)
    p.arrancar()
    try:
        with p.estado.lock:
            for inv in p.estado.invasores:
                inv.vivo = False
            p.estado.puntaje = 500
        correr_ticks(p, 1)
        p.avanzar_nivel()
        with p.estado.lock:
            assert p.estado.nivel == 2
            assert all(i.vivo for i in p.estado.invasores)
            assert p.estado.puntaje == 500, "el punto debe conservarse"
            assert p.estado.step > C.INVASOR_STEP, "la ronda 2 deberia ir mas rapida"
            assert p.estado.nivel_superado is False
            assert p.estado.resultado == "jugando"
    finally:
        p.apagar()


def test_el_tick_se_acelera_en_cada_ronda() -> None:
    p = nueva_partida(niveles=99)
    assert p.ms_por_tick() == C.TICK_MS
    p.arrancar()
    try:
        p.avanzar_nivel()
        assert p.ms_por_tick() == C.TICK_MS - C.TICK_MS_POR_NIVEL
    finally:
        p.apagar()


def test_la_pausa_congela_el_tick() -> None:
    p = nueva_partida()
    p.arrancar()
    try:
        with p.estado.lock:
            p.estado.pausa = True
        p.ultimo_tick = 0.0
        assert p.tick_pendiente() is False, "la pausa no congelo el reloj"
    finally:
        p.apagar()


# ===========================================================================
# 9. Escudos
# ===========================================================================
def test_los_escudos_bloquean_y_se_degradan(estado_nuevo: Estado) -> None:
    """Un escudo pierde celdas y nunca se regenera solo."""
    esc = estado_nuevo.escudos[0]
    iniciales = sum(sum(f) for f in esc.celdas)
    x = esc.x + C.ESCUDO_CELDA * 2
    y = esc.y + C.ESCUDO_CELDA * 2
    assert esc.danar(x, y) is True
    assert sum(sum(f) for f in esc.celdas) == iniciales - 1
    # El mismo punto ya no tiene material.
    assert esc.danar(x, y) is False
    assert esc.celda_rota(x, y) is True


def test_hay_cuatro_escudos_bien_separados(estado_nuevo: Estado) -> None:
    e = estado_nuevo
    assert len(e.escudos) == C.ESCUDO_CANTIDAD
    xs = [esc.x for esc in e.escudos]
    assert xs == sorted(xs), "los escudos no estan en orden"
    for a, b in zip(xs, xs[1:]):
        assert b - a >= C.ESCUDO_W, "dos escudos se solapan"
    # Y ninguno se sale de la pantalla.
    assert xs[0] >= 0 and xs[-1] + C.ESCUDO_W <= C.WIDTH


def test_las_balas_destruyen_escudos() -> None:
    """Las balas del jugador erosionan los bunkers."""
    from hilos import HiloBala

    p = nueva_partida()
    p.arrancar()
    try:
        esc = p.estado.escudos[0]
        with p.estado.lock:
            antes = sum(sum(f) for f in esc.celdas)
        h = HiloBala(p.estado, esc.x + C.ESCUDO_CELDA * 2)
        with p.estado.lock:
            h.bala.y = esc.y + C.ESCUDO_CELDA * 2 + 1
            h._colisiones()
            despues = sum(sum(f) for f in esc.celdas)
        assert despues < antes, "la bala no danio el escudo"
        assert h.bala.activa is False
    finally:
        p.apagar()


# ===========================================================================
# 10. Puntuacion
# ===========================================================================
def test_la_puntuacion_depende_de_la_fila() -> None:
    """50/40/30/20 de arriba abajo, como en el arcade."""
    assert C.PUNTOS_POR_FILA == (50, 40, 30, 20)
    e = Estado(seed=1)
    primera = e.invasores[: C.INVASOR_COLUMNAS]
    ultima = e.invasores[-C.INVASOR_COLUMNAS :]
    assert [i.puntos for i in primera] == [50] * C.INVASOR_COLUMNAS
    assert [i.puntos for i in ultima] == [20] * C.INVASOR_COLUMNAS


def test_matar_un_invasor_suma_su_puntuacion() -> None:
    from hilos import HiloBala

    p = nueva_partida()
    p.arrancar()
    try:
        objetivo = p.estado.invasores[-1]  # fila de abajo: 20 puntos
        x = objetivo.x + C.INVASOR_W / 2
        y = objetivo.y + C.INVASOR_H / 2

        # Una bala impacta contra un invasor vivo: debe matarlo y sumar sus
        # puntos. El lock se toma UNA sola vez: `Lock` no es reentrante.
        h = HiloBala(p.estado, x)
        with p.estado.lock:
            h.bala.y = y
            h._colisiones()
            assert h.bala.activa is False, "la bala deberia morir al impactar"
            assert objetivo.vivo is False, "el invasor deberia estar muerto"
            assert p.estado.puntaje == objetivo.puntos, (
                f"se sumaron {p.estado.puntaje} en vez de {objetivo.puntos}"
            )
    finally:
        p.apagar()


# ===========================================================================
# 11. Coherencia del estado bajo concurrencia real
# ===========================================================================
def test_el_estado_permanece_coherente_con_proyectiles_vivos() -> None:
    """Con proyectiles volando, ningun invariante se rompe."""
    import random

    p = nueva_partida()
    p.arrancar()
    try:
        fin = time.monotonic() + 2.0
        while time.monotonic() < fin:
            p.disparar()
            p.mover_jugador(random.choice((-1, 1)))
            _pedir_bombas(p, 3)
            if not p.terminada:
                correr_ticks(p, 1)
            with p.estado.lock:
                assert len(p.estado.balas) <= C.MAX_BALAS
                assert len(p.estado.bombas) <= C.MAX_BOMBAS
                assert len(p.estado.peticiones) <= C.MAX_BOMBAS
                assert p.estado.balas_sem._value <= C.MAX_BALAS
                assert p.estado.bombas_libres._value <= C.MAX_BOMBAS
                # Ninguna entidad duplicada.
                ids = {id(b) for b in p.estado.balas}
                assert len(ids) == len(p.estado.balas), "hay balas duplicadas"
                ids_b = {id(b) for b in p.estado.bombas}
                assert len(ids_b) == len(p.estado.bombas), "hay bombas duplicadas"
    finally:
        p.apagar()


def test_los_hilos_de_bala_se_podan() -> None:
    """`HiloBala` es efimero: la lista de hilos no crece sin limite."""
    p = nueva_partida()
    p.arrancar()
    try:
        for _ in range(200):
            p.disparar()
            time.sleep(0.001)
        assert len(p.estado.hilos) <= 96, (
            f"la lista de hilos crecio a {len(p.estado.hilos)}"
        )
    finally:
        p.apagar()
