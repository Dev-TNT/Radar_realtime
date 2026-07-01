"""
=====================================================================
 RADAR HC-SR04 + SERVO — Hiển thị bằng Pygame
=====================================================================
Đọc dữ liệu "angle,distance" từ Arduino (main.cpp bạn upload) qua
cổng Serial, vẽ radar quét bán nguyệt 0°→180° mượt mà, có:

  - Tia quét (sweep line) phát sáng, di chuyển mượt (nội suy góc)
  - Vệt mờ dần kiểu màn hình phosphor (persistence trail)
  - Chấm "echo" tại vị trí phát hiện vật thể, mờ dần theo thời gian
  - Vòng tròn khoảng cách + lưới góc + nhãn
  - Hiệu ứng gợn sóng (ping ring) khi phát hiện vật mới ở gần
  - HUD hiển thị góc / khoảng cách hiện tại + trạng thái kết nối

Cấu trúc code chia rõ từng phần (CONFIG / SerialReader / Echo /
Radar) nên rất dễ chèn thêm hiệu ứng mới — xem phần "THÊM HIỆU ỨNG
MỚI" ở cuối file.

--------------------------------------------------------------------
CÀI ĐẶT THƯ VIỆN
--------------------------------------------------------------------
    pip install pygame pyserial

--------------------------------------------------------------------
CHẠY CHƯƠNG TRÌNH
--------------------------------------------------------------------
    python radar_pygame.py                  # tự tìm cổng Serial
    python radar_pygame.py --port COM5      # chỉ định cổng (Windows)
    python radar_pygame.py --port /dev/ttyUSB0
    python radar_pygame.py --baud 9600
    python radar_pygame.py --sim            # chạy giả lập, KHÔNG cần Arduino
=====================================================================
"""

import argparse
import math
import sys
import threading
import time
from collections import deque

import pygame

try:
    import serial
    import serial.tools.list_ports
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False


# =====================================================================
# CONFIG — chỉnh ở đây, không cần đọc hiểu phần code bên dưới
# =====================================================================
class Config:
    WIDTH, HEIGHT = 1000, 650

    # Khoảng cách tối đa hiển thị trên radar (cm). Khớp tầm HC-SR04.
    MAX_DISTANCE_CM = 40

    # Số vòng tròn khoảng cách trên radar
    RING_COUNT = 4

    # Tốc độ nội suy tia quét (độ/giây) khi chưa có dữ liệu mới
    # -> giúp tia quét luôn MƯỢT dù Arduino gửi dữ liệu rời rạc (delay 20ms/bước)
    SWEEP_LERP_SPEED = 6.0     # hệ số easing (càng lớn theo càng sát giá trị thật)

    # Thời gian một điểm echo tồn tại trước khi mờ hẳn (giây)
    ECHO_LIFETIME = 2.2

    # Độ mờ dần của vệt quét (persistence). 0-255, càng nhỏ vệt càng dài
    TRAIL_FADE_ALPHA = 18

    FPS = 60

    # Màu sắc (kiểu màn hình radar cổ điển)
    COLOR_BG = (5, 15, 10)
    COLOR_GRID = (0, 90, 40)
    COLOR_GRID_DIM = (0, 55, 25)
    COLOR_SWEEP_CORE = (170, 255, 190)
    COLOR_SWEEP_GLOW = (40, 220, 100)
    # Vật cản (echo) và vùng bị che khuất phía sau nó -> tông đỏ
    COLOR_ECHO = (255, 70, 70)          # chấm tại vị trí vật cản
    COLOR_SHADOW = (200, 30, 30)        # dải bóng phía sau vật cản (bị che, sóng không tới được)
    COLOR_TEXT = (150, 255, 180)
    COLOR_TEXT_DIM = (60, 140, 90)
    COLOR_WARN = (255, 90, 90)

    # Độ rộng góc của dải bóng phía sau vật cản (độ), mô phỏng bề rộng chùm tia
    SHADOW_HALF_WIDTH_DEG = 1.6


# =====================================================================
# ĐỌC SERIAL TRONG THREAD RIÊNG — không làm khựng vòng lặp vẽ pygame
# =====================================================================
class SerialReader:
    """Đọc từng dòng 'angle,distance' từ Arduino trong 1 thread nền.
    Giá trị mới nhất được lưu vào self.angle / self.distance (thread-safe
    ở mức đủ dùng cho ứng dụng đơn giản kiểu này)."""

    def __init__(self, port=None, baud=9600):
        self.port = port
        self.baud = baud
        self.ser = None
        self.connected = False
        self.running = True

        self.angle = 0
        self.distance = None          # None = chưa có dữ liệu / ngoài tầm
        self.new_reading = False      # cờ báo có dữ liệu mới cho hiệu ứng ping

        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _try_connect(self):
        if not HAS_SERIAL:
            return False
        port = self.port
        if port is None:
            ports = list(serial.tools.list_ports.comports())
            if not ports:
                return False
            port = ports[0].device  # tự chọn cổng đầu tiên tìm thấy
        try:
            self.ser = serial.Serial(port, self.baud, timeout=1)
            time.sleep(2)  # đợi Arduino reset sau khi mở cổng Serial
            self.connected = True
            print(f"[Serial] Đã kết nối: {port} @ {self.baud} baud")
            return True
        except Exception as e:
            self.connected = False
            return False

    def _run(self):
        while self.running:
            if not self.connected:
                if not self._try_connect():
                    time.sleep(1.5)
                    continue

            try:
                line = self.ser.readline().decode("utf-8", errors="ignore").strip()
                if not line or "," not in line:
                    continue
                a_str, d_str = line.split(",", 1)
                angle = int(a_str)
                distance = int(d_str)

                with self._lock:
                    self.angle = max(0, min(180, angle))
                    # HC-SR04 trả 0 hoặc số rất lớn khi ngoài tầm/lỗi
                    if 0 < distance <= 400:
                        self.distance = distance
                    else:
                        self.distance = None
                    self.new_reading = True

            except Exception:
                self.connected = False
                try:
                    if self.ser:
                        self.ser.close()
                except Exception:
                    pass
                time.sleep(1.0)

    def read(self):
        """Lấy dữ liệu mới nhất, trả về (angle, distance, is_new)."""
        with self._lock:
            is_new = self.new_reading
            self.new_reading = False
            return self.angle, self.distance, is_new

    def stop(self):
        self.running = False
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass


class SimulatedReader:
    """Giả lập dữ liệu radar khi không có Arduino — để test giao diện."""

    def __init__(self):
        self.connected = True
        self._angle = 0
        self._dir = 1
        self._t0 = time.time()

    def start(self):
        return self

    def read(self):
        self._angle += self._dir * 2
        if self._angle >= 180:
            self._angle, self._dir = 180, -1
        elif self._angle <= 0:
            self._angle, self._dir = 0, 1

        # Vài "vật thể" giả lập ở một số góc
        t = time.time() - self._t0
        dist = None
        for center, width, base in [(60, 12, 18), (130, 8, 28)]:
            if abs(self._angle - center) < width:
                dist = int(base + 4 * math.sin(t * 2 + center))
        if dist is None and (self._angle % 40 < 3):
            dist = int(15 + 10 * math.sin(t))

        return self._angle, dist, True

    def stop(self):
        pass


# =====================================================================
# ECHO — một điểm phát hiện vật thể, tự mờ dần theo thời gian
# =====================================================================
class Echo:
    __slots__ = ("angle", "distance", "born")

    def __init__(self, angle, distance):
        self.angle = angle
        self.distance = distance
        self.born = time.time()

    def age(self):
        return time.time() - self.born

    def alpha(self, lifetime):
        remain = 1.0 - (self.age() / lifetime)
        return max(0.0, remain)


# =====================================================================
# RADAR — toàn bộ phần vẽ
# =====================================================================
class Radar:
    def __init__(self, screen, cfg: Config):
        self.screen = screen
        self.cfg = cfg
        self.center = (cfg.WIDTH // 2, cfg.HEIGHT - 40)
        self.radius = min(cfg.WIDTH // 2 - 40, cfg.HEIGHT - 100)

        # Mặt phẳng nền chứa vệt mờ dần (persistence trail)
        self.trail_surface = pygame.Surface((cfg.WIDTH, cfg.HEIGHT), pygame.SRCALPHA)

        # Font phải tạo TRƯỚC vì _build_grid() cần dùng để vẽ nhãn
        self.font = pygame.font.SysFont("consolas", 16)
        self.font_big = pygame.font.SysFont("consolas", 22, bold=True)

        # Lưới (grid) được vẽ sẵn 1 lần lên surface riêng để đỡ tốn CPU
        self.grid_surface = self._build_grid()

        self.echoes: deque[Echo] = deque(maxlen=300)
        self.pings = []  # hiệu ứng gợn sóng: list of dict(pos, born)

        self.display_angle = 0.0   # góc hiển thị đã nội suy (mượt)
        self.target_angle = 0.0    # góc thực từ Serial

    # ---------------------------------------------------------------
    def _polar_to_xy(self, angle_deg, dist_ratio):
        """angle_deg: 0 = phải (đông), 180 = trái (tây), quét qua đỉnh."""
        rad = math.radians(angle_deg)
        r = dist_ratio * self.radius
        x = self.center[0] + r * math.cos(rad)
        y = self.center[1] - r * math.sin(rad)
        return x, y

    def _build_grid(self):
        cfg = self.cfg
        surf = pygame.Surface((cfg.WIDTH, cfg.HEIGHT), pygame.SRCALPHA)
        cx, cy = self.center

        # Vòng tròn khoảng cách
        for i in range(1, cfg.RING_COUNT + 1):
            r = self.radius * i / cfg.RING_COUNT
            pygame.draw.circle(surf, cfg.COLOR_GRID_DIM, (cx, cy), int(r), 1)
            dist_val = int(cfg.MAX_DISTANCE_CM * i / cfg.RING_COUNT)
            label = self.font.render(f"{dist_val}cm", True, cfg.COLOR_TEXT_DIM)
            surf.blit(label, (cx + r - label.get_width() / 2, cy - label.get_height() - 2))

        # Tia góc mỗi 30 độ
        for a in range(0, 181, 30):
            x, y = self._polar_to_xy(a, 1.0)
            pygame.draw.line(surf, cfg.COLOR_GRID_DIM, (cx, cy), (x, y), 1)
            lx, ly = self._polar_to_xy(a, 1.06)
            label = self.font.render(f"{a}°", True, cfg.COLOR_TEXT_DIM)
            surf.blit(label, (lx - label.get_width() / 2, ly - label.get_height() / 2))

        # Đường chân trời (baseline)
        pygame.draw.line(surf, cfg.COLOR_GRID, (cx - self.radius, cy), (cx + self.radius, cy), 2)
        return surf

    def add_reading(self, angle, distance):
        self.target_angle = angle
        if distance is not None and distance <= self.cfg.MAX_DISTANCE_CM:
            self.echoes.append(Echo(angle, distance))
            # Vật ở gần (< 40% tầm) -> thêm hiệu ứng ping nổi bật
            if distance <= self.cfg.MAX_DISTANCE_CM * 0.4:
                x, y = self._polar_to_xy(angle, distance / self.cfg.MAX_DISTANCE_CM)
                self.pings.append({"pos": (x, y), "born": time.time()})

    def update(self, dt):
        # Nội suy góc quét để chuyển động luôn mượt (easing)
        diff = self.target_angle - self.display_angle
        self.display_angle += diff * min(1.0, self.cfg.SWEEP_LERP_SPEED * dt)

        # Dọn echo/ping đã hết hạn
        while self.echoes and self.echoes[0].age() > self.cfg.ECHO_LIFETIME:
            self.echoes.popleft()
        self.pings = [p for p in self.pings if time.time() - p["born"] < 0.8]

    # ---------------------------------------------------------------
    def draw(self):
        cfg = self.cfg

        # 1) Làm mờ dần lớp trail (hiệu ứng phosphor của màn radar cổ)
        fade = pygame.Surface((cfg.WIDTH, cfg.HEIGHT), pygame.SRCALPHA)
        fade.fill((*cfg.COLOR_BG, cfg.TRAIL_FADE_ALPHA))
        self.trail_surface.blit(fade, (0, 0))

        # 2) Vẽ tia quét (glow nhiều lớp) lên trail_surface để nó lưu vệt
        self._draw_sweep(self.trail_surface)

        # 3) Ghép các lớp lên màn hình chính
        self.screen.fill(cfg.COLOR_BG)
        self.screen.blit(self.trail_surface, (0, 0))
        self.screen.blit(self.grid_surface, (0, 0))

        self._draw_echoes()
        self._draw_pings()
        self._draw_hud()

    def _draw_sweep(self, surface):
        cfg = self.cfg
        end = self._polar_to_xy(self.display_angle, 1.0)

        # Vệt quạt mờ phía sau tia (tạo cảm giác "quét" thay vì chỉ 1 đường kẻ)
        trail_span = 14
        for i in range(trail_span, 0, -1):
            a = self.display_angle + (i if self.target_angle >= self.display_angle else -i) * 0.6
            p = self._polar_to_xy(a, 1.0)
            alpha = int(70 * (1 - i / trail_span))
            if alpha <= 0:
                continue
            col = (*cfg.COLOR_SWEEP_GLOW, alpha)
            pygame.draw.line(surface, col, self.center, p, 2)

        # Tia chính: nhiều lớp glow chồng lên nhau
        for width, alpha in [(9, 40), (5, 90), (2, 255)]:
            col = (*cfg.COLOR_SWEEP_CORE, alpha) if width == 2 else (*cfg.COLOR_SWEEP_GLOW, alpha)
            pygame.draw.line(surface, col, self.center, end, width)

        pygame.draw.circle(surface, (*cfg.COLOR_SWEEP_CORE, 255), self.center, 4)

    def _draw_echoes(self):
        cfg = self.cfg

        # Vẽ hết các dải bóng (shadow) lên 1 surface chung trước, rồi mới
        # vẽ các chấm vật cản đè lên trên -> chấm luôn nổi rõ trên bóng.
        shadow_surface = pygame.Surface((cfg.WIDTH, cfg.HEIGHT), pygame.SRCALPHA)

        for e in self.echoes:
            a = e.alpha(cfg.ECHO_LIFETIME)
            if a <= 0:
                continue
            ratio = min(1.0, e.distance / cfg.MAX_DISTANCE_CM)

            # --- Dải bóng: từ vị trí vật cản kéo dài ra tới rìa radar,
            #     mô phỏng vùng sóng siêu âm KHÔNG thể xuyên qua vật cản.
            half_w = cfg.SHADOW_HALF_WIDTH_DEG
            near_l = self._polar_to_xy(e.angle - half_w, ratio)
            near_r = self._polar_to_xy(e.angle + half_w, ratio)
            far_l = self._polar_to_xy(e.angle - half_w, 1.0)
            far_r = self._polar_to_xy(e.angle + half_w, 1.0)
            shadow_alpha = int(110 * a)
            if shadow_alpha > 0:
                pygame.draw.polygon(
                    shadow_surface,
                    (*cfg.COLOR_SHADOW, shadow_alpha),
                    [near_l, far_l, far_r, near_r],
                )

        self.screen.blit(shadow_surface, (0, 0))

        # --- Chấm đỏ tại đúng vị trí vật cản (điểm bị chắn)
        for e in self.echoes:
            a = e.alpha(cfg.ECHO_LIFETIME)
            if a <= 0:
                continue
            x, y = self._polar_to_xy(e.angle, min(1.0, e.distance / cfg.MAX_DISTANCE_CM))
            radius = 3 + 3 * a  # chấm to hơn khi còn mới, co lại khi mờ đi
            s = pygame.Surface((20, 20), pygame.SRCALPHA)
            pygame.draw.circle(s, (*cfg.COLOR_ECHO, int(255 * a)), (10, 10), radius)
            self.screen.blit(s, (x - 10, y - 10))

    def _draw_pings(self):
        cfg = self.cfg
        now = time.time()
        for p in self.pings:
            t = (now - p["born"]) / 0.8
            radius = int(4 + t * 26)
            alpha = int(200 * (1 - t))
            if alpha <= 0:
                continue
            s = pygame.Surface((60, 60), pygame.SRCALPHA)
            pygame.draw.circle(s, (*cfg.COLOR_ECHO, alpha), (30, 30), radius, 2)
            self.screen.blit(s, (p["pos"][0] - 30, p["pos"][1] - 30))

    def _draw_hud(self, status_text=""):
        cfg = self.cfg
        lines = [
            f"Góc: {self.display_angle:5.1f}°",
        ]
        latest = self.echoes[-1] if self.echoes else None
        if latest and latest.age() < 0.5:
            lines.append(f"Khoảng cách: {latest.distance} cm")
        else:
            lines.append("Khoảng cách: --")

        y = 12
        for line in lines:
            surf = self.font_big.render(line, True, cfg.COLOR_TEXT)
            self.screen.blit(surf, (14, y))
            y += 28

        if status_text:
            surf = self.font.render(status_text, True, cfg.COLOR_WARN)
            self.screen.blit(surf, (14, cfg.HEIGHT - 24))


# =====================================================================
# MAIN
# =====================================================================
def parse_args():
    p = argparse.ArgumentParser(description="Radar Pygame cho HC-SR04 + Servo")
    p.add_argument("--port", default=None, help="Cổng Serial, vd COM5 hoặc /dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--sim", action="store_true", help="Chạy giả lập, không cần Arduino")
    return p.parse_args()


def main():
    args = parse_args()
    cfg = Config()

    pygame.init()
    pygame.display.set_caption("Radar HC-SR04 — Pygame")
    screen = pygame.display.set_mode((cfg.WIDTH, cfg.HEIGHT))
    clock = pygame.time.Clock()

    if args.sim or not HAS_SERIAL:
        if not HAS_SERIAL and not args.sim:
            print("[!] Chưa cài pyserial (pip install pyserial) -> chạy chế độ giả lập.")
        reader = SimulatedReader().start()
    else:
        reader = SerialReader(args.port, args.baud).start()

    radar = Radar(screen, cfg)

    running = True
    last_time = time.time()
    while running:
        now = time.time()
        dt = now - last_time
        last_time = now

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False

        angle, distance, is_new = reader.read()
        if is_new:
            radar.add_reading(angle, distance)

        radar.update(dt)
        radar.draw()

        status = "" if getattr(reader, "connected", True) else "Đang tìm cổng Serial..."
        if status:
            radar._draw_hud(status)

        pygame.display.flip()
        clock.tick(cfg.FPS)

    reader.stop()
    pygame.quit()
    sys.exit()


if __name__ == "__main__":
    main()


# =====================================================================
# THÊM HIỆU ỨNG MỚI — chỉ cần sửa/thêm trong class Radar
# =====================================================================
# - Đổi màu / theme        -> sửa các COLOR_* trong class Config
# - Đổi độ dài vệt quét    -> chỉnh Config.TRAIL_FADE_ALPHA (nhỏ = vệt dài hơn)
# - Đổi độ mượt tia quét   -> chỉnh Config.SWEEP_LERP_SPEED
# - Thêm hiệu ứng mới      -> viết thêm 1 hàm _draw_xxx(self) trong class Radar
#                             rồi gọi nó trong Radar.draw()
# - Ví dụ: thêm rung màn hình khi phát hiện vật rất gần (<10cm) trong add_reading()
# =====================================================================