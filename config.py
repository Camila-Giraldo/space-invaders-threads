"""Constantes de configuracion de Space Invaders concurrente.

Modulo intencionalmente sin dependencias: NO importa pygame. De este modo
`config`, `estado`, `hilos`, `partida`, `telemetria` y `benchmark` se pueden
ejecutar y probar en modo headless (sin ventana), y solo `render`/`main`
tocan la libreria grafica.

Convencion de nombres en español para las constantes y en ingles para los
identificadores de clase y metodo, igual que en la version de partida.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Ventana y ritmo
# --------------------------------------------------------------------------
WIDTH = 800
HEIGHT = 600
FPS = 60

#: Milisegundos entre dos ticks de la flota. Es el "latido" del juego.
TICK_MS = 180

#: Cuanto baja el tick por nivel (el juego se acelera al avanzar de ronda).
TICK_MS_POR_NIVEL = 30

#: Cuanto avanza la flota por tick en cada nivel.
STEP_POR_NIVEL = 1

#: Numero de rondas que se pueden superar antes de ganar la partida.
NIVELES_POR_DEFECTO = 3

# --------------------------------------------------------------------------
# Flota de invasores
# --------------------------------------------------------------------------
INVASOR_FILAS = 4
INVASOR_COLUMNAS = 8
INVASOR_TOTAL = INVASOR_FILAS * INVASOR_COLUMNAS  # 32 hilos de invasor

INVASOR_W = 30
INVASOR_H = 20
INVASOR_SEP_X = 55
INVASOR_SEP_Y = 40
INVASOR_TOP = 60
INVASOR_LEFT = 100
INVASOR_STEP = 6
INVASOR_DROP = 20
INVASOR_MARGEN = 30  # margen lateral antes de cambiar de direccion

#: Si la flota llega a esta altura, el jugador pierde por invasion.
LINEA_INVASION = HEIGHT - 60

#: Puntos por fila, indexadas desde la fila 0 (la mas alta). Clasico del arcade.
PUNTOS_POR_FILA = (50, 40, 30, 20)

#: Probabilidad de que un invasor pida un disparo en cada tick.
#: Las filas de abajo disparan mas, como en el arcade original.
PROB_DISPARO_POR_FILA = (0.004, 0.008, 0.014, 0.022)

# --------------------------------------------------------------------------
# Proyectiles del jugador
# --------------------------------------------------------------------------
MAX_BALAS = 3  # capacidad del semaforo `balas_sem`
BALA_W = 4
BALA_H = 12
BALA_PX_POR_S = 260.0

#: Segundos entre pasos de simulacion de un proyectil.
#: El proyectil avanza `velocidad * PASO_PROYECTIL` pixeles en cada paso,
#: de modo que su rapidez no depende de la precision de time.sleep().
PASO_PROYECTIL = 1.0 / 120.0

# --------------------------------------------------------------------------
# Bombas enemigas
# --------------------------------------------------------------------------
#: Numero de hilos del pool de bombas == numero de bombas simultaneas.
MAX_BOMBAS = 3
BOMBA_W = 4
BOMBA_H = 12
BOMBA_PX_POR_S = 180.0

# --------------------------------------------------------------------------
# Nave del jugador
# --------------------------------------------------------------------------
JUGADOR_W = 40
JUGADOR_H = 20
JUGADOR_PX_POR_S = 320.0
JUGADOR_Y = HEIGHT - 50
VIDAS_INICIALES = 3

#: Milisegundos de invulnerabilidad tras recibir un impacto.
INVULNERABLE_MS = 1500

#: Las teclas se consultan por frame; se limita el desplazamiento por frame.
JUGADOR_PX_POR_FRAME = 8

# --------------------------------------------------------------------------
# Escudos
# --------------------------------------------------------------------------
ESCUDO_CANTIDAD = 4
ESCUDO_CELDAS_X = 12
ESCUDO_CELDAS_Y = 8
ESCUDO_CELDA = 6
ESCUDO_W = ESCUDO_CELDAS_X * ESCUDO_CELDA  # 72
ESCUDO_H = ESCUDO_CELDAS_Y * ESCUDO_CELDA  # 48
ESCUDO_SEP = 56
ESCUDO_Y = 470

#: Patron de celdas intactas por fila (True = material presente).
ESCUDO_FORMA: tuple[str, ...] = (
    "    ####    ",
    "   ######   ",
    "  ########  ",
    "  ########  ",
    "  ########  ",
    "###      ###",
    "###      ###",
    "###      ###",
)

# --------------------------------------------------------------------------
# OVNI (hilo demonio)
# --------------------------------------------------------------------------
OVNI_W = 40
OVNI_H = 20
OVNI_Y = 40
OVNI_PX_POR_S = 90.0
OVNI_MIN_MS = 12_000
OVNI_MAX_MS = 20_000
PUNTOS_OVNI = 100

# --------------------------------------------------------------------------
# Sincronizacion
# --------------------------------------------------------------------------
#: Timeout de la espera del principal a los invasores. Es una red de
#: seguridad, NUNCA una mecanica de juego: si se dispara, se registra en el log.
TIMEOUT_ESPERA_TICK_S = 1.0

#: Timeout de la barrera de arranque de nivel. Mismo criterio.
TIMEOUT_BARRERA_NIVEL_S = 3.0

#: Timeout de los `join` del apagado ordenado.
TIMEOUT_JOIN_S = 1.0

# --------------------------------------------------------------------------
# Paleta
# --------------------------------------------------------------------------
NEGRO = (0, 0, 0)
BLANCO = (255, 255, 255)
VERDE = (0, 255, 0)
VERDE_OSCURO = (0, 140, 0)
AMARILLO = (255, 255, 0)
CYAN = (0, 255, 255)
ROJO = (255, 80, 80)
GRIS = (120, 120, 120)
GRIS_OSCURO = (60, 60, 60)
AZUL = (80, 120, 255)
MAGENTA = (255, 0, 200)
NARANJA = (255, 160, 40)

#: Un color por fila de invasores, de arriba abajo.
COLOR_POR_FILA = (MAGENTA, CYAN, VERDE, AMARILLO)

COLOR_ESCUDO = (60, 200, 120)
COLOR_BALA = AMARILLO
COLOR_BOMBA = ROJO
COLOR_OVNI = CYAN
COLOR_JUGADOR = BLANCO

COLOR_FONDO = NEGRO
COLOR_TEXTO = BLANCO
COLOR_PELIGRO = ROJO
COLOR_EXITO = VERDE
