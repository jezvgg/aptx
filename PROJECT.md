# aptx v2 — «apt-пакеты без установки, без следов, с живыми путями»

Статус: **дизайн, к разработке не приступали**. Прототип v1 существует и работает частично — см. Приложение A.
Документ самодостаточен: его достаточно, чтобы начать разработку с нуля в новой сессии.

---

## 1. Зачем

Хочется запускать любую утилиту из Debian-репозитория (`smartctl`, `fio`, `ripgrep`, `cowsay`, …) **без установки**, при этом:

1. **Пути работают.** Программа находит свои данные (`/usr/share/<pkg>/...`), модули python, конфиги — как если бы пакет был установлен.
2. **Ничего не засоряется.** Ни в `$HOME`, ни в `/usr`, ни в `/etc`, ни в `/var`. Включая библиотеки (никаких «залипших» `.so` и `LD_LIBRARY_PATH`).
3. **Побочные эффекты убираются.** Если утилита создаёт кэш/конфиг/логи (`~/.cache/foo`, `/var/log/foo`) — после выхода от них не остаётся ничего.
4. **Единственное, что остаётся на диске** — сам стор с распакованными пакетами, и он удаляется одной командой.

Прототип v1 решает «скачать и запустить», но **не** решает (1) и (3): `cowsay` падает с `Could not find default.cow cowfile!`, потому что ищет `/usr/share/cowsay`, а он лежит в сторе.

### Не-цели

- Не замена apt: ничего не интегрируется в систему, `dpkg` не знает об этих пакетах.
- Не security-песочница: сеть и `/dev` доступны, seccomp не настраивается. Это инструмент гигиены, не изоляции злоумышленника.
- Не сборка из исходников (это Nix). Мы берём готовые `.deb`, но запускаем их переносимо.

---

## 2. Целевая система (проверено)

| Что | Значение |
|---|---|
| ОС | Debian 13 (trixie), ядро 7.2.11+deb13-amd64 |
| CPU/RAM | AMD Ryzen AI MAX+ 395 (Strix Halo, gfx1151), 117 ГБ RAM |
| Диски | `nvme0n1` YMTC PC41Q (Gen4 QLC, `/`, 932 ГБ свободно), `nvme1n1` ADATA S11 Pro («migration», данные пользователя) |
| bubblewrap | **0.12.0-1~deb13u1** (установлен) |
| unprivileged userns | включены: `kernel.unprivileged_userns_clone` не ограничен, `user.max_user_namespaces=481290`, AppArmor-ограничения нет |
| `/bin/sh` | `/usr/bin/dash` |
| Доступно в apt, но не установлено | `proot 5.1.0-1.3+b1`, `patchelf 0.18.0-1.4`, `fuse-overlayfs 1.14.0-1+b1` |
| Отсутствует | `podman`, `docker`, `nix`, `smartmontools` |
| Особенность | `nvme-cli` стоит, но в `/usr/sbin` — вне `PATH` пользователя |
| sudo | требует пароль; инструмент должен работать **без root** |

## 3. Что уже проверено экспериментально (не повторять!)

Ключевые результаты экспериментов с `bubblewrap`. Это фундамент архитектуры.

| Попытка | Команда (суть) | Результат |
|---|---|---|
| База | `bwrap --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp echo ok` | ✅ работает |
| Тень поверх ro-корня | `--ro-bind / / --ro-bind $T/demo /usr/share/demo` | ❌ `bwrap: Can't create file /usr/share/demo: Read-only file system` |
| tmpfs-прослойка в ro-корне | `--ro-bind / / --tmpfs /usr/share/zz --ro-bind …` | ❌ та же ошибка |
| Подмена `$HOME` в ro-корне | `--ro-bind / / --bind $H /home/fake` | ❌ `Can't create file /home/fake: Read-only file system` |
| overlayfs в userns | `unshare -rm; mount -t overlay …` | ❌ не монтируется |
| fake-root в userns | `--unshare-user --uid 0 --gid 0 --bind / / --ro-bind $T/s /usr/share/zzz` | ❌ `Permission denied` (без `newuidmap` uid 0 в ns = 1000, файлы root уходят в overflow) |
| **tmpfs-корень + точечные rebind** | `--tmpfs / --tmpfs /usr --ro-bind /usr/bin /usr/bin … --symlink usr/bin /bin` | ✅ бинарники живые |
| **Тень внутри tmpfs-каталога** | `--tmpfs / --tmpfs /usr --tmpfs /usr/share --ro-bind /usr/share/zoneinfo /usr/share/zoneinfo --ro-bind $T/s /usr/share/zzz` | ✅ `cat /usr/share/zzz/f.txt` → `SHADOWED` |

**Вывод (главное правило):** точку монтирования можно создать только в **записываемом** каталоге. Поэтому корень песочницы строится на `tmpfs`, а всё нужное с хоста в него перебиндивается. Первый вариант этого теста падал с `execvp /usr/bin/sh: No such file or directory` — забыли прибиндить `/lib64` (динамический линкер) и симлинки `/bin`,`/lib`,`/lib64`. **Обязательный минимум для запуска любого ELF:** `/usr/lib`, `/usr/lib64`, `/usr/bin` + симлинки `/bin → usr/bin`, `/lib → usr/lib`, `/lib64 → usr/lib64`.

**Вывод про overlayfs:** `fuse-overlayfs` в Debian есть (`1.14.0-1+b1`), но тянуть демон/пакет ради слияния каталогов не хочется; kernel-overlay в userns на этом ядре не поднимается. Поэтому слияние делаем **своими руками** (см. §5.4, симлинк-ферма).

**Вывод про proot:** `proot` (ptrace) умеет ровно то, что нужно (`-b pkg:/usr/share/x`), ценой замедления на syscall-тяжёлых нагрузках. Держим как fallback-бэкенд, но основной — bwrap (накладные 5–20 мс).

---

## 4. Архитектура

```
                ┌──────────────── aptx run cowsay "hi" ────────────────┐
                │                                                       │
  1. store ─────┤ apt-get download closure → dpkg -x → .manifest       │
                │                                                       │
  2. plan ──────┤ manifest × состояние хоста → список bind'ов и теней   │
                │                                                       │
  3. stage ─────┤ симлинк-ферма в $STAGE (слияние каталогов)            │
                │                                                       │
  4. exec ──────┤ bwrap: tmpfs-корень + binds + tmpfs-/$HOME → бинарь   │
                │                                                       │
  5. cleanup ───┤ rm -rf $STAGE; tmpfs умирает вместе с процессом       │
                └───────────────────────────────────────────────────────┘
```

### 4.1 Store (единственное постоянное состояние)

```
~/.cache/aptx/store/<pkg>/
├── .ready              # маркер завершённой распаковки
├── .deps               # замыкание (список пакетов, откуда взято)
├── .provides           # ВСЕ пути, которые дают все дебы замыкания (dpkg-deb -c)
├── .files              # исполняемые файлы ЗАПРОШЕННОГО пакета
├── .meta               # версия, arch, sha256 дебов, дата
├── .stage-hash         # хеш плана → кэш симлинк-фермы (см. 5.4)
├── usr/ bin/ sbin/ lib/ lib64/ etc/ var/ …   # распакованный корень
└── stage/              # кэш симлинк-фермы (опционально, регенерируемо)
```

Правила:
- Стор строится из **замыкания целиком** (`apt-cache depends --recurse`), уже установленные в системе пакеты **пропускаются** (`dpkg-query -W -f='${db:Status-Abbrev}'` начинается с `ii`) — их берём с хоста.
- `--recommends` — опция, по умолчанию выключено (иначе тянет пол-системы).
- `.provides` строится по **всем** дебам замыкания, а не только по запрошенному пакету: именно отсюда берётся список путей для теней.
- `aptx gc` удаляет записи старше N дней, `aptx rm-all` — всё.

### 4.2 План (plan)

Вход: `.provides` + состояние хоста. Выход: JSON/список вида

```json
{
  "shadow_dirs":  ["/usr/share/cowsay", "/usr/lib/cowsay"],       // не существуют на хосте → bind напрямую
  "merge_dirs":   ["/usr/bin", "/usr/share/man/man1"],            // существуют → симлинк-ферма
  "merge_files":  ["/usr/lib/x86_64-linux-gnu/libfoo.so.1"],      // файловый коллизион → файл поверх
  "etc_merge":    ["/etc/foo.conf"],                              // конфиги: пакет побеждает, host-версия видна как .host
  "python_dirs":  ["/usr/lib/python3/dist-packages"]              // отдельная политика
}
```

Приоритеты при слиянии (зафиксировать в коде и тестах):

| Ситуация | Политика |
|---|---|
| Путь есть только в пакете | bind пакета |
| Путь есть только на хосте | bind хоста |
| Путь есть в обоих, это файл | **пакет побеждает** (иначе смысл теряется) |
| Путь есть в обоих, это каталог данных (`/usr/share/<pkg>`) | пакет побеждает целиком |
| `/etc/<file>` в пакете и на хосте | пакет побеждает; host-копия доступна как `<file>.host` (для отладки) |
| `/usr/lib/<triplet>/*.so*` | пакет побеждает (ровно то, что нужно для библиотек) |

### 4.3 Песочница (bwrap)

Скелет команды (проверенная основа):

```bash
bwrap \
  --tmpfs /                        `# корень: пустой, записываемый → можно создавать точки монтирования` \
  --tmpfs /usr --tmpfs /etc --tmpfs /var --tmpfs /opt \
  --ro-bind /usr/bin       /usr/bin      \
  --ro-bind /usr/lib       /usr/lib      \
  --ro-bind /usr/lib64     /usr/lib64    \
  --ro-bind /usr/libexec   /usr/libexec  \
  --ro-bind /usr/share/zoneinfo /usr/share/zoneinfo \
  --ro-bind /usr/share/terminfo /usr/share/terminfo \
  --ro-bind /usr/share/locale   /usr/share/locale   \
  --ro-bind /etc/passwd /etc/passwd --ro-bind /etc/group /etc/group \
  --ro-bind /etc/hosts /etc/hosts --ro-bind /etc/resolv.conf /etc/resolv.conf \
  --ro-bind /etc/ssl /etc/ssl --ro-bind /etc/localtime /etc/localtime \
  --symlink usr/bin /bin --symlink usr/sbin /sbin \
  --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
  --ro-bind "$STAGE/usr" /usr2      `# ← либо тени, либо staging-корень (см. 4.4)` \
  --tmpfs /tmp --tmpfs /var/tmp --tmpfs /var/cache --tmpfs /var/log \
  --tmpfs /home/$USER --tmpfs /root \
  --setenv HOME /home/$USER \
  --setenv XDG_CACHE_HOME  /home/$USER/.cache \
  --setenv XDG_CONFIG_HOME /home/$USER/.config \
  --setenv XDG_DATA_HOME   /home/$USER/.local/share \
  --setenv XDG_STATE_HOME  /home/$USER/.local/state \
  --setenv TMPDIR /tmp \
  --proc /proc --dev /dev --dev-bind /dev/dri /dev/dri   `# /dev/dri только с --gpu` \
  --unshare-pid --unshare-ipc --unshare-uts --new-session --die-with-parent \
  --chdir "$PWD" -- "$BIN" "$@"
```

Тонкости:
- `--dev /dev` даёт «минимальный» `/dev`; для GPU/видео добавлять `--dev-bind /dev/dri /dev/dri`, для ROCm ещё `/dev/kfd`.
- `--unshare-net` — только под флагом `--offline`.
- Переменные окружения чистить через `--clearenv` + `--setenv` минимума (`PATH`, `HOME`, `TERM`, `LANG`, `USER`, `TZ`, `XDG_RUNTIME_DIR`), чтобы не протекли `LD_PRELOAD`/`PYTHONPATH` хоста.
- `--chdir` обязателен, иначе cwd = `/` (и `fio --output=out.txt` запишет не туда, куда ждёшь).

### 4.4 Слияние каталогов без overlayfs — симлинк-ферма

Проблема: `/usr/share` на хосте занят, а нам надо добавить `/usr/share/cowsay`. Ро-бинд хостового каталога запрещает создавать внутри точки монтирования.

Решение: собрать **свой** каталог в `$STAGE` (обычный tmpfs-каталог на хосте):

```bash
cp -as /usr/share/. "$STAGE/usr/share/"        # рекурсивная симлинк-ферма на хост
cp -as "$ROOT/usr/share/." "$STAGE/usr/share/" # сверху — пакет (перезаписывает симлинки)
```
(`cp -as` = «создать симлинки вместо копирования»; конфликтующие имена перезаписываются, порядок = приоритет).

Затем в песочницу биндится `$STAGE/usr/share` как `/usr/share`. Ферма кэшируется: пересобирается только если изменился `.stage-hash` (версия пакета + хеш списка путей хоста).

Почему так, а не overlay: overlay в userns не монтируется (проверено), `fuse-overlayfs` тянет зависимость; ферма — 10–50 мс на несколько thousand записей и полностью предсказуема.

Fallback-бэкенд `--backend=proot`: `proot -b "$ROOT/usr/share/cowsay:/usr/share/cowsay" -b /:/ …` — медленнее, но не требует сборки staging; удобно для отладки самого aptx.

### 4.5 Библиотеки

`LD_LIBRARY_PATH` **не используется вообще**. Вместо этого:
- из `.provides` берутся все `*.so*` и биндятся по их **точным** путям (`/usr/lib/x86_64-linux-gnu/libfoo.so.1`);
- динамический линкер находит их штатно, как в установленной системе;
- замыкание apt даёт одну версию каждой библиотеки → конфликтов нет по построению;
- версия glibc в сторе и на хосте из одного дистрибутива → ABI-расхождений нет (при кросс-релизных пакетах — предупреждение, см. §9).

### 4.6 Изоляция записи и «побочки»

| Место | Политика по умолчанию | Что даёт |
|---|---|---|
| `$HOME` | `--tmpfs` | кэши, конфиги, `.local/share` умирают с процессом |
| `/tmp`, `/var/tmp` | `--tmpfs` | временные файлы исчезают |
| `/var/cache`, `/var/log`, `/run` | `--tmpfs` | демоны/тулзы не пишут на диск |
| `/etc` | tmpfs-копия нужных файлов, ro | запись конфигов не доходит до хоста |
| `--persist[=NAME]` | `--bind $STORE/<pkg>/state /home/$USER` | для тех, кому нужен «как в системе» (кэш моделей, история) |
| `--ephemeral` | store тоже в tmpfs | ноль следов на диске ценой повторной загрузки |
| `--rw HOST:TARGET` | явный bind | точечно разрешить запись (например, `~/out:/out`) |

Важно: `$PWD` по умолчанию **виден только на чтение**; если программа пишет в cwd — нужен `--rw-cwd` (или биндить `$PWD` rw). Это осознанный выбор: «тихо записать в текущий каталог» — самый частый способ насорить.

---

## 5. CLI

```
aptx run <pkg> [--] [args…]     # скачать (если нужно) и запустить; автовыбор бинаря
aptx run <pkg> <bin> [args…]    # конкретный бинарь, если их несколько
aptx ls <pkg>                   # список исполняемых файлов пакета
aptx which <pkg> [bin]          # путь в сторе (без запуска)
aptx shell <pkg>                # интерактивный шелл в той же песочнице
aptx plan <pkg>                 # показать план: тени, слияния, биндов (отладка)
aptx store [path|size|du]       # состояние стора
aptx gc [--older-than 7d]
aptx rm-all
aptx doctor                     # проверка: bwrap, userns, /dev/kfd, версии
```

Флаги: `--offline`, `--gpu`, `--persist[=NAME]`, `--ephemeral`, `--rw HOST:TARGET`, `--rw-cwd`, `--recommends`, `--backend=bwrap|proot`, `--verbose`, `--no-sandbox` (аварийный режим = поведение v1).

Выход: код возврата программы; при ошибке — понятное сообщение и строка для `aptx plan` (что искали и где не нашли).

---

## 6. Этапы и критерии готовности

| Этап | Содержание | Готово, когда |
|---|---|---|
| **P0** | `doctor`, store, `.manifest` (`.provides/.deps/.files/.meta`), `ls/which/rm-all/gc` | `aptx ls cowsay` показывает `/usr/games/cowsay`; `rm-all` чистит; `doctor` зелёный |
| **P1** | песочница без теней: tmpfs-корень, базовые binds, изоляция `$HOME`/`/tmp` | `aptx run ripgrep rg --version`, `aptx run fio fio --name=t …` работают; после запуска `~` не изменился ни на файл (`find ~ -newer marker`) |
| **P2** | план + тени + симлинк-ферма | **`aptx run cowsay "hi"` печатает корову**; `fio --output=out.fio` пишет туда, куда просили; `ldd` не видит `LD_LIBRARY_PATH` |
| **P3** | политика записи: `--persist`, `--rw`, `--ephemeral`, `/etc` merge | пакет с конфигом в `/etc` запускается и не портит `/etc` хоста; `--persist` сохраняет состояние между запусками |
| **P4** | флаги `--gpu/--offline`, `aptx shell`, `--backend=proot` | `aptx shell fio` даёт шелл с fio в PATH; `--offline` рубит сеть; `--gpu` видит `/dev/dri` |
| **P5** | кэш фермы, `gc`, размеры, пиннинг версии (`aptx pin pkg=1.2.3`) | повторный запуск < 100 мс; `aptx store du` показывает размеры |
| **P6** | тесты (bats/shunit), README, `--help`, коды ошибок | набор из §7 проходит в CI-подобном скрипте |

Порядок работ: P0 → P1 → P2 — обязательный минимум; дальше по вкусу.

---

## 7. Приёмочные тесты

| # | Команда | Ожидание |
|---|---|---|
| 1 | `aptx run ripgrep rg --version` | версия, 0 код возврата |
| 2 | `aptx run fio fio --name=t --rw=read --size=8M --output=/tmp/o` | файл отчёта появляется **в песочнице**, в `$PWD` ничего не создаётся |
| 3 | `aptx run cowsay "hi"` | корова + код 0 (главный тест на пути) |
| 4 | `aptx run smartctl -a /dev/nvme0n1` | без `--gpu`/`--dev` — ошибка доступа; с `--dev-bind /dev` — работает |
| 5 | `aptx run python3-pkg-based-tool …` (пакет с `dist-packages`) | импорт модулей из пакета работает |
| 6 | любой запуск, затем `find $HOME -newer /tmp/marker` | пусто |
| 7 | `aptx run foo` дважды | второй запуск не качает и быстрый (<100 мс накладных) |
| 8 | `aptx run foo` при отсутствии сети | внятная ошибка про `apt-get download` |
| 9 | `aptx --ephemeral run ripgrep rg --version` | после выхода `~/.cache/aptx` пуст |
| 10 | `aptx plan cowsay` | печатает 5–10 строк: что тень, что merge, что bind |

---

## 8. Производительность (ожидания)

| Операция | Оценка |
|---|---|
| bwrap-обвязка | 5–20 мс |
| Симлинк-ферма (кэш) | < 5 мс; холодная 10–50 мс |
| `apt-get download` замыкания | секунды (сеть); кэш — 0 |
| `dpkg -x` | 0.2–2 с в зависимости от размера |
| proot-бэкенд | +30–70 % на fork/syscall-тяжёлых, незаметно на compute |

Холодный запуск маленькой утилиты — единицы секунд (доминирует скачивание), тёплый — десятки миллисекунд сверх самой программы.

---

## 9. Известные ограничения и подводные камни

1. **setuid-бинарники** (`passwd`, `mount`) не работают: `dpkg -x` не сохраняет suid, в userns прав нет.
2. **systemd-сервисы/демоны** — вне рамок (никакого systemd в песочнице).
3. **Модули ядра, `/sys`-запись, sysctl** — вне рамок.
4. **Скрипты с `#!/usr/bin/python3`**: нужен бинд `/usr/lib/python3/dist-packages` из пакета; если пакет тянет python3-модули со своим ABI — работает, пока версия python совпадает с хостовой.
5. **Кросс-релизные пакеты** (бэкпорты/стабильные из другого релиза): риск glibc-несовместимости. Решение — предупреждение, если равно `dpkg --print-architecture` и версия libc в сторе ≠ хостовой.
6. **`/usr/share` очень большой**: ферма по `/usr/share` целиком — тысячи симлинков. Оптимизация: фермить только те каталоги, где есть тени (обычно 1–5 штук), остальное биндить ro напрямую.
7. **`--dev /dev` vs реальные устройства**: по умолчанию устройства нет; `--gpu` открывает `/dev/dri` и `/dev/kfd` (llama.cpp, видеокодеки, mesa).
8. **X11/Wayland**: GUI-программам нужен `$XDG_RUNTIME_DIR` (wayland-socket) и `/tmp/.X11-unix`; по умолчанию не даём — это отдельный флаг `--gui`.
9. **`apt-get download` без root** работает, но использует хостовый `/var/lib/apt/lists` (только чтение). При `apt update` в это время — редкие гонки; лечится копированием lists в стор при ошибке.
10. **Многоархитектурные пакеты** (`:i386`) и `--reinstall` — отдельная ветка, в MVP не поддерживаются.
11. **Теневые конфликты**: два пакета с одним путём — «пакет побеждает» внутри его песочницы; глобального стора с пересечениями не бывает, потому что каждый запуск строит свой план.
12. **`--chdir` + `--rw-cwd`**: если cwd не биндится rw, относительные записи падают — это by design, но сообщение должно быть внятным («куда хочешь писать? используй --rw-cwd»).

---

## 10. Решения, которые уже зафиксированы

- Язык MVP: **bash** (как v1), с прицелом на перенос в Rust/Go после P2, если станет тесно.
- Без root, без демонов, без systemd-юнитов.
- Стор: `~/.cache/aptx` (можно `APTX_CACHE`), удаляется целиком `rm-all`.
- `LD_LIBRARY_PATH` не используется; библиотеки — binds по точным путям.
- Основной бэкенд — bwrap; proot — fallback и инструмент отладки.
- Слияние каталогов — симлинк-ферма (`cp -as`), не overlayfs (проверено: в userns не монтируется).
- Изоляция записи — tmpfs по умолчанию, «как в системе» — только через `--persist`.
- Не security-граница: об этом писать в README явно.

## 11. Открытые вопросы (решить в начале разработки)

1. Формат манифеста: плоские TSV (просто, grep-able) или JSON (удобнее для Rust-порта)? Предложение: TSV в MVP.
2. Нужно ли `aptx install`-подобное поведение («положить симлинки в `~/.local/bin` на запуск через песочницу») — удобно, но плодит следы; скорее да, под явным флагом.
3. Кэшировать ли staging-ферму в сторе (место vs скорость): предложено да, с `.stage-hash`.
4. `--recommends` по умолчанию: нет (иначе `cowsay` потянет пол-системы), но для некоторых пакетов нужен — предупреждать по факту.
5. Судьба `LD_PRELOAD`-шимов (переписывание путей вместо биндов) как третий бэкенд — вероятно, не нужен, но упомянуть в README.
6. Как вести себя, если пакет уже установлен в системе (`aptx` всё равно делает стор или запускает хостовый?) — сейчас: запускает хостовый и говорит об этом. Для v2 — флаг `--force-stage`.

---

## 12. Полезные команды для отладки

```bash
# что пакет даёт по файлам
apt-get download cowsay >/dev/null 2>&1 && dpkg-deb -c cowsay_*.deb | awk '$1 ~ /^-..x/ {print $6}'
# какие пути пакет даёт (для теней)
dpkg-deb -c pkg.deb | awk '{print $6}' | sed 's|^\.||'
# проверка, что видит песочница
bwrap … -- ls -la /usr/share/cowsay
# сравнение: до/после запуска в $HOME
touch /tmp/marker; aptx run foo; find "$HOME" -newer /tmp/marker -not -path "$HOME/.cache/aptx/*"
# strace по провалу (какой путь не найден)
strace -f -e trace=openat,stat,access -o /tmp/tr.txt aptx run foo ; grep -i "ENOENT.*cow" /tmp/tr.txt
```

---

## Приложение A. Прототип v1 (`~/.local/bin/aptx`, 2479 байт)

Работает: замыкание, скачивание, распаковка, выбор бинаря, `--rm-all/--clean`, `LD_LIBRARY_PATH`.
Не работает: пути (`cowsay`), изоляция (`$HOME` пишется как есть), слияние каталогов.

```bash
#!/usr/bin/env bash
# aptx — run an apt package's binary without installing it system-wide.
#   aptx <pkg>                  # stage + list binaries
#   aptx <pkg> [args...]        # single-binary packages
#   aptx <pkg> <binary> [args...]
#   aptx --rm-all | --clean
set -euo pipefail
CACHE="${APTX_CACHE:-$HOME/.cache/aptx}"
case "${1:-}" in
  --rm-all) rm -rf "$CACHE"; echo "aptx: cache cleared"; exit 0;;
  --clean)  find "$CACHE" -mindepth 1 -maxdepth 1 -mtime +7 -exec rm -rf {} + ; echo "aptx: old entries removed"; exit 0;;
esac
pkg="${1:?usage: aptx <pkg> [binary] [args...]}"; shift || true
root="$CACHE/$pkg"

if [ ! -e "$root/.ready" ]; then
  deps=$(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts \
          --no-breaks --no-replaces --no-enhances "$pkg" 2>/dev/null \
        | grep -E '^[a-zA-Z0-9]' | awk '{print $1}' | sed 's/:.*//' | sort -u)
  todo=""
  for d in $deps; do
    case "$d" in "<"*|"") continue;; esac
    [ "$(dpkg-query -W -f='${db:Status-Abbrev}' "$d" 2>/dev/null | head -c2 || true)" = "ii" ] && continue
    todo="$todo $d"
  done
  [ -z "${todo// /}" ] && { echo "aptx: $pkg and all deps already installed" >&2; exit 1; }
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
  echo "aptx: fetching$todo" >&2
  ( cd "$tmp" && apt-get download $todo >/dev/null 2>&1 ) || { echo "aptx: download failed" >&2; exit 1; }
  mkdir -p "$root"; : > "$root/.files"
  for deb in "$tmp"/*.deb; do
    dpkg -x "$deb" "$root"
    if [ "$(dpkg-deb -f "$deb" Package)" = "$pkg" ]; then
      dpkg-deb -c "$deb" | awk '$1 ~ /^-..x/ {print $6}' | sed 's|^\.||' >> "$root/.files"
    fi
  done
  : > "$root/.ready"
fi

bins=$(while read -r p; do [ -f "$root$p" ] && echo "$p"; done < "$root/.files")
[ -z "$bins" ] && { echo "aptx: no executables in $pkg" >&2; exit 1; }

if [ $# -gt 0 ] && grep -qxF "/$1" <(sed 's|.*/||' <<<"$bins" | sed 's|^|/|') 2>/dev/null; then
  rel=$(grep -E "(^|/)$1\$" <<<"$bins" | head -1); shift
elif grep -qE "(^|/)$pkg\$" <<<"$bins"; then rel=$(grep -E "(^|/)$pkg\$" <<<"$bins" | head -1)
elif [ "$(wc -l <<<"$bins")" -eq 1 ]; then rel="$bins"
else echo "aptx: $pkg has several binaries, pick one:" >&2; sed 's|.*/||;s/^/  - /' <<<"$bins" >&2; exit 1; fi

export PATH="$root/usr/bin:$root/usr/sbin:$root/bin:$root/sbin:$root/usr/games:$PATH"
export LD_LIBRARY_PATH="$root/usr/lib/x86_64-linux-gnu:$root/lib/x86_64-linux-gnu:$root/usr/lib:$root/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$root$rel" "$@"
```

Что из него переносим как есть: резолвинг замыкания, фильтр уже установленных, `apt-get download` + `dpkg -x`, `.files`, выбор бинаря, `--rm-all/--clean`.
Что выбрасываем: `LD_LIBRARY_PATH`, `PATH`-префикс, прямой `exec` бинаря.

## Приложение B. Контекст, не относящийся к aptx

- LLM-движков в Debian trixie нет (`llama.cpp`, `ollama`, `vllm` — отсутствуют в apt). Для них — podman с `ghcr.io/ggml-org/llama.cpp:server-vulkan` или nix (`llama-cpp-vulkan`). aptx для этого не подходит и не должен.
- На машине нет ROCm userspace, но есть Mesa RADV → Vulkan-путь рабочий из коробки.
- Диск: `~/.cache/aptx` лежит на `nvme0n1` (Gen4 QLC), при переносе `/home` на `nvme1n1` (TLC) стор переедет вместе с ним — это нормально, стор читается редко и пачками.
