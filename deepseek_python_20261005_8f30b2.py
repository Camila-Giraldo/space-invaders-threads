import pygame
import threading
import time
import random

# ---------------- Configuración ----------------
WIDTH, HEIGHT = 800, 600
FPS = 60
TICK_MS = 180                      # cada cuánto mueve la flota

INVADER_ROWS = 4
INVADER_COLS = 8
INVADER_COUNT = INVADER_ROWS * INVADER_COLS
INVADER_W, INVADER_H = 30, 20
INVADER_SPACING_X = 55
INVADER_SPACING_Y = 40
INVADER_TOP = 60
INVADER_LEFT = 100
INVADER_STEP = 6
INVADER_DROP = 20
INVADER_MARGIN = 30

MAX_PLAYER_BULLETS = 3
BULLET_W, BULLET_H = 4, 12
BULLET_SPEED = 12
PLAYER_W, PLAYER_H = 40, 20
PLAYER_SPEED = 6

NEGRO, BLANCO = (0, 0, 0), (255, 255, 255)
VERDE, AMARILLO = (0, 255, 0), (255, 255, 0)
CYAN, ROJO = (0, 255, 255), (255, 80, 80)

# ---------------- Estado compartido ----------------
class Estado:
    def __init__(self):
        self.lock = threading.Lock()
        self.fin = threading.Event()
        self.invasores = []
        for r in range(INVADER_ROWS):
            for c in range(INVADER_COLS):
                self.invasores.append({
                    "x": INVADER_LEFT + c * INVADER_SPACING_X,
                    "y": INVADER_TOP + r * INVADER_SPACING_Y,
                    "vivo": True,
                })
        self.direccion = 1          # 1 derecha, -1 izquierda
        self.bajar = False          # flag de un solo tick
        self.jugador_x = WIDTH // 2
        self.balas = []
        self.puntaje = 0
        self.ovni_x = -100
        self.ovni_activo = False

estado = Estado()

# BARRERA: 32 invasores + hilo principal
barrera = threading.Barrier(INVADER_COUNT + 1)

# SEMÁFORO: máximo 3 balas simultáneas del jugador
balas_sem = threading.Semaphore(MAX_PLAYER_BULLETS)

# ---------------- Hilos ----------------
def hilo_invasor(idx):
    """Cada invasor es un hilo de vida larga. Sincroniza con barrera en cada tick."""
    while not estado.fin.is_set():
        # --- esperar señal de inicio de tick ---
        try:
            barrera.wait()
        except threading.BrokenBarrierError:
            return
        if estado.fin.is_set():
            return

        with estado.lock:
            inv = estado.invasores[idx]
            if inv["vivo"]:
                if estado.bajar:
                    inv["y"] += INVADER_DROP
                else:
                    inv["x"] += INVADER_STEP * estado.direccion

        # --- avisar que ya movimos ---
        try:
            barrera.wait()
        except threading.BrokenBarrierError:
            return


def hilo_bala(x):
    """Bala efímera. El semáforo limita cuántas hay a la vez."""
    balas_sem.acquire()
    try:
        bala = {"x": x, "y": HEIGHT - 60, "activo": True}
        with estado.lock:
            estado.balas.append(bala)

        while bala["activo"] and not estado.fin.is_set():
            time.sleep(0.016)
            with estado.lock:
                bala["y"] -= BULLET_SPEED
                if bala["y"] < 0:
                    bala["activo"] = False
                    break

                # colisión con invasores
                for inv in estado.invasores:
                    if inv["vivo"] and \
                       inv["x"] <= bala["x"] <= inv["x"] + INVADER_W and \
                       inv["y"] <= bala["y"] <= inv["y"] + INVADER_H:
                        inv["vivo"] = False
                        bala["activo"] = False
                        estado.puntaje += 10
                        break

                # colisión con OVNI
                if estado.ovni_activo and bala["activo"] and \
                   estado.ovni_x <= bala["x"] <= estado.ovni_x + 40 and \
                   40 <= bala["y"] <= 60:
                    estado.ovni_activo = False
                    bala["activo"] = False
                    estado.puntaje += 100

        with estado.lock:
            if bala in estado.balas:
                estado.balas.remove(bala)
    finally:
        balas_sem.release()


def hilo_ovni():
    """Demonio: aparece cada 12-20s, cruza la pantalla y desaparece."""
    while not estado.fin.is_set():
        time.sleep(random.uniform(12, 20))
        if estado.fin.is_set():
            return

        with estado.lock:
            estado.ovni_activo = True
            estado.ovni_x = -40

        while not estado.fin.is_set():
            time.sleep(0.02)
            with estado.lock:
                if not estado.ovni_activo or estado.ovni_x > WIDTH + 40:
                    break
                estado.ovni_x += 4

        with estado.lock:
            estado.ovni_activo = False


# ---------------- Loop principal ----------------
def main():
    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("Space Invaders concurrente")
    clock = pygame.time.Clock()
    fuente = pygame.font.SysFont("consolas", 20)
    fuente_g = pygame.font.SysFont("consolas", 48)

    # Lanzar los 32 hilos de invasores
    hilos_inv = []
    for i in range(INVADER_COUNT):
        t = threading.Thread(target=hilo_invasor, args=(i,), daemon=True)
        t.start()
        hilos_inv.append(t)

    # Lanzar el OVNI demonio
    threading.Thread(target=hilo_ovni, daemon=True).start()

    ultimo_tick = time.time()
    running = True
    game_over = False

    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_SPACE and not game_over:
                    with estado.lock:
                        px = estado.jugador_x
                    threading.Thread(target=hilo_bala, args=(px,), daemon=True).start()

        if not game_over:
            # Mover jugador (hilo principal, lock solo para escribir)
            teclas = pygame.key.get_pressed()
            with estado.lock:
                if teclas[pygame.K_LEFT]:
                    estado.jugador_x = max(PLAYER_W // 2,
                                           estado.jugador_x - PLAYER_SPEED)
                if teclas[pygame.K_RIGHT]:
                    estado.jugador_x = min(WIDTH - PLAYER_W // 2,
                                           estado.jugador_x + PLAYER_SPEED)

            # ¿Toca tick?
            if (time.time() - ultimo_tick) * 1000 >= TICK_MS:
                ultimo_tick = time.time()

                # Fase previa: decidir dirección / bajar
                with estado.lock:
                    vivos = [inv for inv in estado.invasores if inv["vivo"]]
                    if vivos:
                        min_x = min(inv["x"] for inv in vivos)
                        max_x = max(inv["x"] for inv in vivos)
                        if max_x + INVADER_W >= WIDTH - INVADER_MARGIN and estado.direccion > 0:
                            estado.direccion = -1
                            estado.bajar = True
                        elif min_x <= INVADER_MARGIN and estado.direccion < 0:
                            estado.direccion = 1
                            estado.bajar = True
                        else:
                            estado.bajar = False

                # DOS barreras por tick: inicio y fin
                try:
                    barrera.wait(timeout=1.0)   # inicio: liberar a los invasores
                    barrera.wait(timeout=1.0)   # fin: esperar a que todos muevan
                except threading.BrokenBarrierError:
                    pass

                # Chequeos finales (fuera de la fase de movimiento)
                with estado.lock:
                    vivos = [inv for inv in estado.invasores if inv["vivo"]]
                    if not vivos:
                        game_over = True
                        estado.fin.set()
                    else:
                        for inv in vivos:
                            if inv["y"] + INVADER_H >= HEIGHT - 60:
                                game_over = True
                                estado.fin.set()
                                break

        # -------- Render --------
        screen.fill(NEGRO)
        with estado.lock:
            for inv in estado.invasores:
                if inv["vivo"]:
                    pygame.draw.rect(screen, VERDE,
                                     (inv["x"], inv["y"], INVADER_W, INVADER_H))
            for b in estado.balas:
                pygame.draw.rect(screen, AMARILLO,
                                 (b["x"], b["y"], BULLET_W, BULLET_H))
            if estado.ovni_activo:
                pygame.draw.rect(screen, CYAN, (estado.ovni_x, 40, 40, 20))
            pygame.draw.rect(screen, BLANCO,
                             (estado.jugador_x - PLAYER_W // 2,
                              HEIGHT - 50, PLAYER_W, PLAYER_H))
            puntaje = estado.puntaje
            balas_activas = len(estado.balas)

        screen.blit(fuente.render(f"Puntaje: {puntaje}", True, BLANCO), (10, 10))
        screen.blit(fuente.render(
            f"Balas: {balas_activas}/{MAX_PLAYER_BULLETS}", True, BLANCO), (10, 35))

        if game_over:
            txt = fuente_g.render("GAME OVER", True, ROJO)
            screen.blit(txt, (WIDTH // 2 - txt.get_width() // 2, HEIGHT // 2 - 40))

        pygame.display.flip()
        clock.tick(FPS)

    # -------- Cierre limpio --------
    estado.fin.set()
    try:
        barrera.abort()          # desbloquea a los invasores que esperan
    except Exception:
        pass
    for h in hilos_inv:
        h.join(timeout=0.5)
    pygame.quit()


if __name__ == "__main__":
    main()