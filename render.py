"""Dibujado del juego. El unico modulo que dibuja.

REGLA: el render NUNCA dibuja mientras otro hilo escribe. El flujo es siempre

    snapshot = tomar_snapshot(estado)   # una pasada rapida BAJO el lock
    dibujar(screen, snapshot)           # todo el dibujado FUERA del lock

En la version de partida, las 30 lineas de dibujado estaban dentro de
`with estado.lock`, de modo que cada frame congelaba a los 32 hilos de la
flota durante el dibujado (defecto D4). Aqui la frontera es el dataclass
`Snapshot`.
"""

from __future__ import annotations

import pygame

import config as C
from estado import Escudo, Estado, Snapshot

# ---------------------------------------------------------------------------
# Sprites: se dibujan una vez a superficies y luego solo se blitean.
# Dibujar 32 invasores celda a celda cada frame son ~2800 draw.rect por frame;
# blitear 32 superficies son 32 llamadas.
# ---------------------------------------------------------------------------
_SPRITES: dict[tuple[int, int], pygame.Surface] = {}
_FONDO_ESCUDOS: dict[int, pygame.Surface] = {}

#: Tres siluetas distintas segun la fila (0 = la mas alta).
_INVASOR_ART: dict[int, tuple[str, ...]] = {
    0: (
        "  ..XX..  ",
        "  .XXXX.  ",
        "  XXXXXX  ",
        "  X.XX.X  ",
        "XX.XXXX.XX",
    ),
    1: (
        "..XX....XX..",
        "...XXXXXX...",
        "..XXXXXXX..",
        ".XX.XXX.XX.",
        "X..XXXXX..X",
    ),
    2: (
        "..XX....XX..",
        ".X.XXXXXX.X.",
        "..XXXXXXX..",
        "XX.XXX.XX.XX",
        ".XXXXXXXXX.",
    ),
}

_JUGADOR_ART = (
    "..XX............XX..",
    "...XXX........XXX...",
    "...XXXXXXXXXXXXXX...",
    "XXXXXXXXXXXXXXXXXXXX",
    "XXXXXXXXXXXXXXXXXXXX",
    "XXXXXXXXXXXXXXXXXXXX",
)


class _Cache:
    """Fuentes cacheadas. Crear una `Font.render` por frame es caro."""

    def __init__(self) -> None:
        self.pequena = pygame.font.SysFont("consolas", 18)
        self.grande = pygame.font.SysFont("consolas", 46)
        self.memo: dict[tuple[str, tuple], pygame.Surface] = {}


_fuentes: _Cache | None = None


def init(ventana: pygame.Surface) -> None:
    """Prepara fuentes y sprites. Una vez al arrancar."""
    global _fuentes
    _fuentes = _Cache()
    _SPRITES.clear()
    _FONDO_ESCUDOS.clear()
    ventana.fill(C.COLOR_FONDO)


def _texto(screen: pygame.Surface, texto: str, color, pos) -> None:
    """Blitea un texto cacheado por su contenido."""
    assert _fuentes is not None
    clave = (texto, color)
    sup = _fuentes.memo.get(clave)
    if sup is None:
        sup = _fuentes.pequena.render(texto, True, color)
        _fuentes.memo[clave] = sup
    screen.blit(sup, pos)


# ---------------------------------------------------------------------------
# Sprites
# ---------------------------------------------------------------------------
def _sprite_invasor(fila: int) -> pygame.Surface:
    """Superficie de un invasor, cacheada por fila."""
    clave = (0, fila)
    sup = _SPRITES.get(clave)
    if sup is not None:
        return sup
    arte = _INVASOR_ART.get(min(fila, 2), _INVASOR_ART[2])
    # Los pixeles del arte se escalan para llenar la caja del invasor.
    px = max(1, C.INVASOR_W // max(len(arte[0]), 1))
    py = max(1, C.INVASOR_H // len(arte))
    sup = pygame.Surface((C.INVASOR_W, C.INVASOR_H), pygame.SRCALPHA)
    color = C.COLOR_POR_FILA[min(fila, len(C.COLOR_POR_FILA) - 1)]
    for f, linea in enumerate(arte):
        for c, ch in enumerate(linea):
            if ch == "X":
                sup.fill(color, (c * px, f * py, px, py))
    _SPRITES[clave] = sup
    return sup


def _sprite_jugador() -> pygame.Surface:
    sup = _SPRITES.get((1, 0))
    if sup is not None:
        return sup
    px = max(1, C.JUGADOR_W // len(_JUGADOR_ART[0]))
    py = max(1, C.JUGADOR_H // len(_JUGADOR_ART))
    sup = pygame.Surface((C.JUGADOR_W, C.JUGADOR_H), pygame.SRCALPHA)
    for f, linea in enumerate(_JUGADOR_ART):
        for c, ch in enumerate(linea):
            if ch == "X":
                sup.fill(C.COLOR_JUGADOR, (c * px, f * py, px, py))
    _SPRITES[(1, 0)] = sup
    return sup


def _fondo_escudo(escudo: Escudo) -> pygame.Surface:
    """Superficie del escudo, reconstruida solo cuando cambian sus celdas."""
    clave = id(escudo)
    sup = _FONDO_ESCUDOS.get(clave)
    if sup is not None and not escudo.sucio:
        return sup
    if sup is None:
        sup = pygame.Surface((C.ESCUDO_W, C.ESCUDO_H), pygame.SRCALPHA)
        _FONDO_ESCUDOS[clave] = sup
    sup.fill((0, 0, 0, 0))
    for f in range(C.ESCUDO_CELDAS_Y):
        for c in range(C.ESCUDO_CELDAS_X):
            if escudo.celdas[f][c]:
                pygame.draw.rect(
                    sup,
                    C.COLOR_ESCUDO,
                    (c * C.ESCUDO_CELDA, f * C.ESCUDO_CELDA, C.ESCUDO_CELDA, C.ESCUDO_CELDA),
                )
    escudo.sucio = False
    return sup


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------
def tomar_snapshot(estado: Estado) -> Snapshot:
    """Copia el estado protegido. Se hace TODO bajo el lock y en una pasada.

    Es la unica seccion critica del render, y dura microsegundos porque no
    dibuja nada.
    """
    with estado.lock:
        return Snapshot(
            jugador_x=estado.jugador_x,
            vidas=estado.vidas,
            puntaje=estado.puntaje,
            nivel=estado.nivel,
            invaders=[(i.x, i.y, i.fila) for i in estado.invasores if i.vivo],
            ovni=(estado.ovni_x, float(C.OVNI_Y)) if estado.ovni_activo else None,
            balas=[(b.x, b.y) for b in estado.balas],
            bombas=[(b.x, b.y) for b in estado.bombas],
            escudos=estado.escudos,  # por referencia: el render no los muta
            invulnerable=estado.tiempo_ms < estado.invulnerable_hasta,
            resultado=estado.resultado,
            nivel_superado=estado.nivel_superado,
            pausado=estado.pausa,
            contadores=estado.contadores.copia(),
        )


# ---------------------------------------------------------------------------
# Dibujado
# ---------------------------------------------------------------------------
def dibujar(screen: pygame.Surface, s: Snapshot) -> None:
    """Dibuja un frame. Se ejecuta SIN el lock tomado."""
    screen.fill(C.COLOR_FONDO)

    for esc in s.escudos:
        screen.blit(_fondo_escudo(esc), (esc.x, esc.y))

    if s.ovni is not None:
        x, y = s.ovni
        pygame.draw.rect(screen, C.COLOR_OVNI, (int(x), int(y), C.OVNI_W, C.OVNI_H))
        pygame.draw.circle(screen, C.BLANCO, (int(x) + 8, int(y) + 10), 3)
        pygame.draw.circle(screen, C.BLANCO, (int(x) + C.OVNI_W - 8, int(y) + 10), 3)

    for x, y, fila in s.invaders:
        screen.blit(_sprite_invasor(fila), (int(x), int(y)))

    for x, y in s.balas:
        pygame.draw.rect(screen, C.COLOR_BALA, (int(x), int(y), C.BALA_W, C.BALA_H))

    for x, y in s.bombas:
        pygame.draw.rect(screen, C.COLOR_BOMBA, (int(x), int(y), C.BOMBA_W, C.BOMBA_H))
        pygame.draw.rect(
            screen, C.NARANJA, (int(x) - 1, int(y) + C.BOMBA_H // 2, C.BOMBA_W + 2, 2)
        )

    # El jugador parpadea mientras es invulnerable.
    if not s.invulnerable or (pygame.time.get_ticks() // 90) % 2 == 0:
        screen.blit(
            _sprite_jugador(),
            (int(s.jugador_x - C.JUGADOR_W / 2), int(C.JUGADOR_Y)),
        )

    _hud(screen, s)
    # El `flip` lo hace `main.py`, despues de dibujar la superposicion.


def _hud(screen: pygame.Surface, s: Snapshot) -> None:
    _texto(screen, f"Puntaje: {s.puntaje}", C.COLOR_TEXTO, (10, 8))
    _texto(screen, f"Ronda {s.nivel}", C.COLOR_TEXTO, (10, 30))

    # Vidas: un icono reducido por cada vida restante.
    for i in range(max(0, s.vidas)):
        pygame.draw.rect(screen, C.COLOR_JUGADOR, (C.WIDTH - 24 - i * 20, 18, 14, 6))

    c = s.contadores
    _texto(
        screen,
        f"Balas {len(s.balas)}/{C.MAX_BALAS}   Bombas {len(s.bombas)}/{C.MAX_BOMBAS}",
        C.GRIS,
        (10, C.HEIGHT - 24),
    )
    _texto(screen, f"Ticks: {c['ticks']}", C.GRIS, (C.WIDTH - 150, C.HEIGHT - 24))


def superposicion(screen: pygame.Surface, s: Snapshot) -> None:
    """Avisos grandes: pausa, ronda superada y fin de partida."""
    centro = (C.WIDTH // 2, C.HEIGHT // 2)

    def grande(texto: str, color, y) -> None:
        sup = _fuentes.grande.render(texto, True, color)
        screen.blit(sup, (centro[0] - sup.get_width() // 2, y))

    if s.resultado == "victoria":
        grande("VICTORIA", C.COLOR_EXITO, centro[1] - 60)
        _texto(
            screen,
            f"Rondas: {s.nivel}   Puntos: {s.puntaje}",
            C.COLOR_TEXTO,
            (centro[0] - 80, centro[1] + 4),
        )
    elif s.resultado == "derrota":
        grande("GAME OVER", C.COLOR_PELIGRO, centro[1] - 60)
        _texto(
            screen,
            f"Ronda {s.nivel}   Puntos: {s.puntaje}",
            C.COLOR_TEXTO,
            (centro[0] - 80, centro[1] + 4),
        )
    elif s.resultado == "invasion":
        grande("INVASION", C.COLOR_PELIGRO, centro[1] - 60)
        _texto(
            screen,
            "La flota llego al final",
            C.COLOR_TEXTO,
            (centro[0] - 90, centro[1] + 4),
        )
    elif s.nivel_superado:
        grande("RONDA SUPERADA", C.COLOR_EXITO, centro[1] - 60)
        _texto(
            screen,
            f"Puntos: {s.puntaje}",
            C.COLOR_TEXTO,
            (centro[0] - 40, centro[1] + 4),
        )
    elif s.pausado:
        grande("PAUSA", C.COLOR_TEXTO, centro[1] - 60)

    _texto(
        screen,
        "R reinicia    ESC sale",
        C.GRIS,
        (centro[0] - 80, centro[1] + 40),
    )
