# NVIDIA P-State Switcher

[![Лицензия: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://python.org)
[![PyQt6](https://img.shields.io/badge/PyQt-6-green.svg)](https://riverbankcomputing.com/software/pyqt/)
[![CI](https://github.com/uladziislau/nvidia-pstate-switcher/actions/workflows/ci.yml/badge.svg)](https://github.com/uladziislau/nvidia-pstate-switcher/actions/workflows/ci.yml)

> **English**: [README.md](README.md)

Трей для NVIDIA на Linux: P-состояния GPU, governor процессора и профили
питания KDE в одном меню — плюс счётчик потребления GPU / CPU package / DRAM.

Один файл, без демонов и без базы данных. Всё состояние читается из sysfs,
`nvidia-smi` и D-Bus.

---

## Зачем

GSP-прошивка NVIDIA может держать GPU в P0 даже на статичном рабочем столе и
потреблять ~45 Вт вместо ~21 Вт. Причина — прошивка считает `TEST_ONLY`
атомарные KMS-коммиты нагрузкой; KWin генерирует их по два на кадр.

Пока баг не исправлен, единственный способ — вручную ограничить P-состояние.
Этот трей делает переключение быстрым и обратимым, а заодно показывает, чего
стоит каждый режим в ваттах.

## Возможности

| Раздел меню | Что делает |
|---|---|
| **Power Profile** | Переключение профиля KDE (`power-saver` / `balanced` / `performance`) через D-Bus PowerProfiles; профиль синхронно применяет свой GPU P-state и governor |
| **CPU Governor** | Список берётся из `scaling_available_governors` (включая `schedutil`); выбранный governor **закрепляется**, чтобы автоматика его не откатывала |
| **GPU P-State** | P0 / P2 / P3 / P5 / P8 и Auto (управление драйвером), сохраняется между перезагрузками |
| **Status** | Текущий governor, активный пин, профиль, P-state, мощность GPU, CPU package и DRAM — мгновенно и среднее за минуту |
| **Run at startup** | Автозапуск вместе с рабочим столом (файл `.desktop` создаётся сам) |

Иконка в трее показывает текущий P-state и перерисовывается без внешних
ассетов; подсказка при наведении — полная сводка по питанию.

## Требования

- Linux с видеокартой NVIDIA и проприетарным драйвером (`nvidia-smi`)
- Python **3.10+**
- [`nvidia-pstate`](https://github.com/sasha0552/nvidia-pstate) — CLI/библиотека
  управления P-состояниями (MIT, ставится через pip)
- PyQt6 — только для трея; `--dump-state` и управление из CLI работают без него
- `gdbus` (glib2) — для профилей питания KDE; `cpupower` — для смены governor

### Установка зависимостей

| Дистрибутив | PyQt6 |
|---|---|
| Fedora | `sudo dnf install python3-pyqt6 kernel-tools` |
| Arch / CachyOS | `sudo pacman -S python-pyqt6 cpupower` |
| openSUSE | `sudo zypper install python3-pyqt6 cpupower` |
| Debian / Ubuntu | `sudo apt install python3-pyqt6 linux-cpupower` |
| pip (любой) | `pip install pyqt6` |

```bash
pip install --user nvidia-pstate     # или: pipx install nvidia-pstate
```

## Установка

```bash
git clone https://github.com/uladziislau/nvidia-pstate-switcher
cd nvidia-pstate-switcher
sudo ./install.sh                     # в /usr/local
./install.sh --prefix "$HOME/.local"  # или только для текущего пользователя
```

Установщик ставит один файл, иконку и каталог для пина governor
(`/etc/nvidia-pstate-switcher`, если запущен с правами root). Он ничего не
перезаписывает в вашем конфиге и идемпотентен.

Минимальный вариант — вручную, без репозитория:

```bash
curl -LO https://raw.githubusercontent.com/uladziislau/nvidia-pstate-switcher/main/nvidia-pstate-switcher.py
chmod +x nvidia-pstate-switcher.py
sudo install -m 0755 nvidia-pstate-switcher.py /usr/local/bin/nvidia-pstate-switcher
```

> **GNOME**: нужен `gnome-shell-extension-appindicator`, иначе иконки в трее нет.

## Использование

### Трей

```bash
nvidia-pstate-switcher
```

Правый клик — меню, двойной клик — немедленное обновление. Повторный запуск не
создаёт второй процесс, а подсвечивает уже работающий.

### CLI

```bash
nvidia-pstate-switcher --dump-state            # JSON: состояние машины
nvidia-pstate-switcher --pstate 5              # P5 (idle), и выйти
nvidia-pstate-switcher --pstate 16             # вернуть управление драйверу
nvidia-pstate-switcher --governor schedutil    # поставить и закрепить governor
nvidia-pstate-switcher --unpin-governor        # снять закрепление
nvidia-pstate-switcher --profile power-saver   # профиль KDE и выйти
nvidia-pstate-switcher --version
```

`--dump-state` печатает версию, governor, доступные governor'ы, пин, профиль,
данные GPU, доступность RAPL и найденные зависимости. Это же используется
тестами и удобно для мониторинга из скриптов:

```bash
nvidia-pstate-switcher --dump-state | jq .gpu.power_w
```

## Пин governor: почему выбор «сбрасывается»

Многие системы держат сторож, который возвращает `schedutil` (или `powersave`)
при любом отклонении — например, чтобы профиль «Экономия» из
power-profiles-daemon не зафиксировал минимальную частоту. Такой сторож
перезаписывает и ваш ручной выбор — выглядит как «трей не работает».

Механика пина:

1. Вы выбираете governor в меню → он применяется и записывается в
   `/etc/nvidia-pstate-switcher/governor-pin` (путь переопределяется
   `PSTATE_PIN_FILE`).
2. Трей раз в 2 секунды сверяет фактический governor с пином и возвращает
   закреплённое значение, если кто-то его изменил.
3. Сторонние сторожа должны уважать пин: если файл существует и содержит
   валидное имя, автоматика не должна переключать governor. Готовый пример —
   `examples/cpufreq-guard.sh`.

Снять закрепление: пункт **Follow system (unpin)** в меню или
`--unpin-governor`. Файл пина удаляется, автоматика снова главная.

Если каталог `/etc/nvidia-pstate-switcher` недоступен на запись (установка без
root), трей всё равно удерживает выбранный governor в рамках сессии и сообщает
об этом уведомлением.

### Права на смену governor

Запись в `scaling_governor` требует root. Используется `sudo -n cpupower
frequency-set -g <name>`; чтобы это работало без запроса пароля, добавьте
правило sudoers:

```
uladzislau ALL=(root) NOPASSWD: /usr/bin/cpupower
```

Если ни sudo, ни права на sysfs недоступны, трей покажет предупреждение вместо
тихого отказа.

## Конфигурация

`~/.config/nvidia-pstate-switcher.conf`:

```json
{
  "pstate": "0",
  "autostart": true,
  "governor": "schedutil",
  "pstates": ["0", "2", "3", "5", "8", "12", "15"]
}
```

| Ключ | Смысл |
|---|---|
| `pstate` | P-state, применяемый при старте (`16` = Auto) |
| `autostart` | Зеркало наличия файла автозапуска |
| `governor` | Закреплённый governor (дублирует файл пина) |
| `pstates` | Необязательный список P-состояний для меню — если ваша карта поддерживает другой набор |

## P-состояния

| P-State | Описание | Пояснение |
|---|---|---|
| P0 | Max perf | Максимальная производительность |
| P2 | Balanced | Сбалансированный режим |
| P3 | Medium | Среднее энергосбережение |
| P5 | Idle | Простой / низкое потребление |
| P8 | Deep idle | Глубокий простой |
| Auto (16) | Driver control | Решает драйвер |

Потребление зависит от модели GPU; фактические значения всегда видны в
подсказке трея и в `nvidia-smi`.

## Виджет Plasma (опционально)

`plasmoid/org.nvidia.pstate.monitor` — компактный апплет для панели KDE:
показывает P-state и потребление GPU, данные берёт из `nvidia-smi`.

```bash
kpackagetool6 --type Plasma/Applet --install plasmoid/org.nvidia.pstate.monitor
```

Виджет считается экспериментальным: он не использует общий с треем код и
проверяется только вручную.

## Ограничения

- Это обход, а не исправление бага GSP-прошивки NVIDIA (**#5474539**).
- RAPL-счётчики читаются только на Intel; на AMD блок питания CPU в меню будет
  пустым, остальное работает.
- Смена governor требует root (см. выше); профили KDE — нет.
- Wayland-трей зависит от окружения: KDE и GNOME (с расширением) проверены,
  остальные — по мере возможности.

## Расследование

Инструмент вырос из разбора того, почему GTX 1660 SUPER остаётся в P0 на
Wayland:

- корень — баг GSP-прошивки NVIDIA #5474539: `TEST_ONLY` атомарные KMS-коммиты
  считаются нагрузкой;
- KWin генерирует 2 коммита на кадр (1 `TEST_ONLY` + 1 `REAL`) ≈ 126 ioctl/с при
  63 FPS, а порог прошивки — ~102–114 коммитов/с;
- прототип LD_PRELOAD-фильтра (возвращать 0 на `TEST_ONLY`) уменьшал число
  коммитов вдвое, но давал артефакты рендеринга;
- патч KWin в одну строку (`return true` в `drm_commit.cpp:test()`) решает
  проблему, но требует пересборки `kwin`;
- Warp Terminal держит CUDA-контекст и добавляет ~45 Вт независимо от P-state.

## Разработка

```bash
python3 -m venv .venv && .venv/bin/pip install pytest ruff
.venv/bin/ruff check .
.venv/bin/pytest -q
./nvidia-pstate-switcher.py --dump-state
```

CI прогоняет byte-compile, ruff, тесты на Python 3.10–3.13, проверку CLI и
shellcheck для установщиков. Тесты не требуют GPU, Qt или D-Bus: всё внешнее
подменяется каталогами и парсерами.

## Лицензия

MIT. См. [LICENSE](LICENSE). Зависимость `nvidia-pstate` распространяется под
MIT её автором ([sasha0552](https://github.com/sasha0552/nvidia-pstate)).
