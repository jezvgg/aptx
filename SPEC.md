# aptx v2 — спецификация

Статус: **утверждено к реализации**, итерация P0–P6.
Основание: `PROJECT.md` (дизайн + проверенные эксперименты). Этот документ фиксирует
**решения**, принятые при уточнении, и переводит дизайн в исполнимые требования.

---

## 1. Цель и границы

Запускать утилиты из Debian-репозитория без установки, с тремя гарантиями:

| # | Гарантия | Проверяется |
|---|---|---|
| G1 | **Живые пути** — программа находит свои данные (`/usr/share/<pkg>`, модули python, конфиги) | `aptx run cowsay "hi"` печатает корову |
| G2 | **Ноль следов** — ни в `$HOME`, ни в `/usr`, ни в `/etc`, ни в `/var` | `find $HOME -newer /tmp/marker` пусто |
| G3 | **Один способ убрать всё** — `aptx rm-all` | после него `~/.cache/aptx` отсутствует |

Вне границ (не реализуется и не обещается):

- замена apt: `dpkg` не знает об этих пакетах;
- security-песочница: сеть и `/dev` доступны, seccomp не настраивается. **Это инструмент
  гигиены, а не изоляции злоумышленника** — писать это в README явно;
- сборка из исходников (это Nix);
- setuid-бинарники, systemd-сервисы, модули ядра, запись в `/sys`, sysctl;
- многоархитектурные пакеты (`:i386`) и `--reinstall` в MVP.

---

## 2. Зафиксированные решения

Решения из `PROJECT.md` §10 и из уточняющих вопросов. Менять — только через ADR.

| Область | Решение | Почему |
|---|---|---|
| Язык | **Python ≥ 3.11** | Выбор пользователя; stdlib покрывает subprocess/pathlib/json |
| Библиотеки CLI | **click** (разбор аргументов, справка) + **rich** (прогресс, таблицы) | Выбор пользователя: «не нужно городить свои велосипеды». click штатно выражает контракт §3.2 через `allow_interspersed_args=False`, rich даёт готовый прогресс-бар загрузки |
| Распространение | один `pyproject.toml`, `[project.scripts] aptx` | Работает и `uv tool install .` (постоянная команда), и `uvx --from . aptx …` (без следов) |
| Объём итерации | **P0–P6** | Выбор пользователя |
| Манифест | **гибрид**: списки путей — TSV, метаданные — JSON | TSV grep-able и строится одной строкой awk; JSON нужен для структуры |
| Форма CLI | subcommand (`aptx run <pkg>`) + **сокращение** `aptx <pkg>` | Сокращение — по просьбе пользователя: `aptx cowsay "hi"`. Двусмысленности нет: имя команды проверяется первым, и только неизвестный токен, не начинающийся с `-`, считается именем пакета |
| Разделение флагов | **флаги aptx только до имени пакета**, всё после — программе | Предсказуемо: `aptx run --gpu cowsay -a -b` |
| Язык интерфейса | **английский** (сообщения, `--help`, ошибки) | Стандарт для unix-утилит, не ломает копипаст в issue |
| `install` | **shim-скрипт** в `~/.local/bin`, не симлинк на бинарь | Симлинк на бинарь в сторе = поведение v1, пути не работают |
| Выбор бинаря | несколько бинарей → тот, чьё имя совпадает с именем пакета; если такого нет — ошибка со списком | `cowsay` содержит `cowsay`, `cowthink`, `cowsay_random`; угадывать чужое имя нельзя, а одноимённое — очевидный дефолт |
| `install` при нескольких бинарях | **только явно**: `aptx install fio fio` | Не засорять `~/.local/bin` |
| Бэкенд | **bwrap** основной, **proot** fallback, `--no-sandbox` аварийный | §3: bwrap 5–20 мс накладных, proot +30–70 % на syscall |
| Слияние каталогов | **симлинк-ферма**, не overlayfs | §3: overlay в userns на этом ядре не монтируется |
| Библиотеки | `LD_LIBRARY_PATH` **не используется**, либы биндятся по точным путям | §4.5: линкер находит их штатно |
| Стор | `~/.cache/aptx` (`APTX_CACHE`), удаляется целиком | §10 |
| Root | **не нужен для обычной работы**; `sudo aptx …` поддерживается для админских утилит | `sudo` ставит `USER=root` — `$HOME` внутри песочницы тогда `/root`, а не `/home/root`. Устройства из командной строки пробрасываются автоматически (§3.3). Под root user namespace не создаётся и добавляется `--cap-add ALL`: ioctl к устройству проверяет начальный user namespace (§6.2) |

---

## 3. CLI

### 3.1 Общая форма

```
aptx [GLOBAL] <command> [ARGS]
aptx [GLOBAL] <pkg> [args...]    # сокращение для `aptx run <pkg>`

commands:
  run      Download (if needed) and run a package binary in a sandbox
  install  Stage a package and create command shims in the bin dir
  remove   Remove shims and staged data
  list     List installed packages and their shims
  ls       List executables provided by a package (stages it)
  which    Print the store path of a package binary (no run)
  shell    Open an interactive shell inside the package sandbox
  plan     Show the bind/merge plan (debug)
  store    Inspect the store (path | size | du)
  gc       Remove store entries unused for N days
  rm-all   Remove the entire store and all shims
  pin      Pin a package to a specific version
  unpin    Drop a pin (same as `aptx pin <pkg> --clear`)
  doctor   Check the environment
```

Глобальные флаги (до команды): `-v/--verbose`, `-q/--quiet`, `--no-color`, `--version`, `-h/--help`.

**Сокращение.** Первый токен ищется среди команд; если команды с таким именем нет и токен
не начинается с `-`, он считается именем пакета и команда выполняется как `aptx run`:

```
aptx cowsay "hi"                    # то же, что aptx run cowsay "hi"
aptx cowsay -f tux "hi"             # флаги cowsay, не aptx
aptx --nonsense                     # не пакет: «unknown option», код 2
aptx lss                            # имя команды не найдено → пакет `lss` → код 65
```

Флаги aptx в сокращённой форме недоступны: `aptx --gpu cowsay` — это глобальный флаг,
а `--gpu` после имени пакета уходит программе. Для флагов aptx используйте `aptx run --gpu cowsay`.

### 3.2 Правило разбора аргументов

**Всё до имени пакета — флаги aptx. Всё после — аргументы программы.** `--` не обязателен,
но поддерживается и отбрасывается.

```
aptx run --gpu cowsay "hi"          # --gpu для aptx, "hi" для cowsay
aptx run cowsay --gpu               # --gpu уходит cowsay как аргумент
aptx run -- cowsay "hi"             # эквивалентно предыдущему без --
aptx run cowsay cowsay --help       # --help уходит cowsay
```

Исключение: у `install`/`remove`/`ls`/`which`/`pin`/`gc` все позиционные аргументы — данные,
флагов aptx можно ставить где угодно (они не конфликтуют с аргументами программы, которой там нет).

### 3.3 `aptx run`

```
aptx run [FLAGS] <pkg> [<bin>] [args…]
```

- `<pkg>` — имя пакета Debian.
- `<bin>` — необязателен. Может быть именем (`cowsay`), относительным (`usr/games/cowsay`)
  или абсолютным (`/usr/games/cowsay`) путём. Резолвится по `.files.tsv`.
- Если `<bin>` не задан: ровно один исполняемый файл у пакета → он; если их
  несколько — тот, чьё имя совпадает с именем пакета; иначе — ошибка `EX_USAGE`
  со списком и подсказкой.
- Если одноимённых файлов несколько (`git` содержит и `/usr/bin/git`, и
  `/usr/lib/git-core/git`), выбирается самый короткий путь, при равенстве —
  лексикографически меньший. Порядок детерминированный, поэтому `aptx run git`
  работает без уточнений.
- Первый токен после `<pkg>` считается `<bin>` только если он есть в
  `.files.tsv`. Иначе он уходит программе как обычный аргумент — это прямое
  следствие правила протаскивания: `aptx run cowsay not-a-bin hi` печатает
  корову с текстом `not-a-bin hi`, а не падает.

Флаги:

| Флаг | Действие |
|---|---|
| `--offline` | `--unshare-net`: сеть внутри песочницы отключена |
| `--gpu` | `--dev-bind /dev/dri /dev/dri`, `--dev-bind-try /dev/kfd /dev/kfd` |
| `--gui` | плюс `/tmp/.X11-unix`, `$XDG_RUNTIME_DIR` (wayland), `DISPLAY`, `WAYLAND_DISPLAY` |
| `--persist[=NAME]` | `$HOME` в песочнице = `$STORE/<pkg>/state/default` для голого флага, `$CACHE/homes/NAME` для `=NAME` (общий для всех пакетов), состояние переживает запуск |
| `--ephemeral` | стор целиком в tmpfs: скачать → запустить → всё исчезло |
| `--rw HOST:TARGET` | явный rw-bind, повторяемый |
| `--rw-cwd` | `$PWD` биндится rw вместо ro |
| `--dev-bind SRC[:DST]` | проброс устройства/каталога устройств, повторяемый |
| `--dev` | пробросить весь хостовый `/dev` (`--dev-bind /dev /dev`) вместо чистого devtmpfs |
| `--recommends` | тянуть Recommends при резолве замыкания |
| `--backend {bwrap,proot}` | по умолчанию `bwrap` |
| `--no-sandbox` | аварийный режим: прямой exec с PATH-префиксом (поведение v1) |
| `--force-stage` | стейджить пакет, даже если он установлен в системе |
| `--dry-run` | напечатать команду песочницы и выйти, ничего не запуская |

Устройства, названные в командной строке программы, пробрасываются автоматически:
`aptx run nvme-cli smart-log /dev/nvme1n1` видит `/dev/nvme1n1` без `--dev-bind` — путь,
который пользователь сам написал, и есть запрошенный доступ. Действует только для
существующих на хосте char/block-устройств и отключается флагом `--dev`.

**Порядок монтирования важен.** `--dev /dev` ставит свежий devtmpfs и затирает всё,
что примонтировано в `/dev` раньше. Поэтому `--proc /proc` и `--dev /dev` (или
`--dev-bind /dev /dev` при `--dev`) идут ДО биндов устройств.

Поведение при уже установленном пакете: если пакет `ii` в `dpkg`, aptx пишет в stderr
`aptx: note: cowsay is already installed; running the host binary (use --force-stage to override)`
и запускает хостовый бинарь напрямую.

Выход: код возврата программы. Ошибки самого aptx — §9.

### 3.4 `aptx install` / `remove` / `list`

```
aptx install <pkg> [<bin>…]      # стор + shim(ы)
aptx remove <pkg>… | --all
aptx list
```

`install`:
1. строит (или переиспользует) запись стора;
2. создаёт shim в `$BIN_DIR` (`~/.local/bin`, переопределяется `APTX_BIN`);
3. shim — shell-скрипт:

```sh
#!/bin/sh
# aptx shim for cowsay (package cowsay, bin /usr/games/cowsay)
# Remove with: aptx remove cowsay
exec /home/jezv/.local/bin/aptx run cowsay cowsay "$@"
```

Абсолютный путь к aptx фиксируется при установке (`shutil.which("aptx")`, fallback —
`sys.argv[0]`). При запуске shim ищет `aptx` в `PATH`; если не находит — использует
зафиксированный путь.

4. если shim уже существует и он не наш — отказ с `EX_CANTCREAT`, если не `--force`.

`remove`: удаляет shims пакета (только те, что помечены `# aptx shim for`), затем запись стора.
`--all` — все пакеты.

`list`: таблица `PACKAGE  VERSION  SIZE  BINS  SHIMS`, плюс секция `orphan shims`
(shim, чей пакет пропал из стора) и `unshimmed` (пакет в сторе без shims).

### 3.5 Остальные команды

```
aptx ls <pkg>              # список исполняемых файлов пакета (стейджит при необходимости)
aptx which <pkg> [<bin>]   # путь в сторе, ничего не запускает; код 66, если не найдено
aptx shell <pkg> [--] [args...]   # /bin/sh в песочнице пакета; всё после -- уходит шеллу;
                                  # тот же набор флагов, что у run. Пример:
                                  #   aptx shell cowsay -- -c 'echo $HOME'
aptx plan <pkg>            # тени / merge-точки / бинды / конфликты (5–15 строк)
aptx store path            # печатает $STORE
aptx store size            # суммарный размер
aptx store du              # размеры по пакетам, по убыванию
aptx gc [--older-than 7d]  # удаляет записи, не использованные N дней
aptx rm-all                # стор + все shims
aptx pin <pkg>=<version>   # закрепить версию; aptx pin <pkg> --clear
aptx doctor                # окружение: bwrap, userns, apt, dpkg, стор, /dev/kfd, /dev/dri
```

`--older-than` принимает `7d`, `24h`, `30m`, `2w`. По умолчанию `7d`.

`aptx ls`/`which` без аргументов и `aptx store` без подкоманды — `EX_USAGE` с подсказкой.

### 3.6 `--help`

Справку генерирует click из docstring'ов команд: `aptx --help` — список команд с
однострочными описаниями, `aptx <cmd> --help` — полная справка по команде с флагами и
примерами. Голый `aptx` печатает ту же справку и выходит с кодом 0.
Все тексты — английские.

Флаги aptx в режиме протаскивания (`run`, `shell`) объявлены с
`allow_interspersed_args=False`, поэтому click сам перестаёт разбирать опции на первом
позиционном аргументе: `aptx run cowsay --dry-run` передаёт `--dry-run` программе,
а `aptx run --dry-run cowsay` — нет. `--` click съедает сам.

---

## 4. Стор

### 4.1 Раскладка

```
$APTX_CACHE (default ~/.cache/aptx)
├── pins.json                    # {"cowsay": "3.03+dfsg2-8"}
├── homes/                       # --persist=NAME: один $HOME на все пакеты
│   └── NAME/
└── store/
    └── cowsay/
        ├── .ready               # маркер завершённой распаковки (пишется последним)
        ├── .meta.json           # метаданные (см. 4.2)
        ├── .deps.tsv            # замыкание
        ├── .provides.tsv        # ВСЕ пути всех дебов замыкания
        ├── .files.tsv           # исполняемые файлы ЗАПРОШЕННОГО пакета
        ├── .stage-hash          # хеш плана → кэш фермы
        ├── .last-used           # mtime обновляется на каждом запуске
        ├── stage/               # кэш симлинк-фермы (регенерируемо)
        ├── state/               # --persist (голый флаг)
        └── usr/ bin/ sbin/ lib/ etc/ var/ opt/ …   # распакованный корень
```

`stage/`, `state/` — единственные каталоги, которые aptx создаёт сам; всё остальное —
результат `dpkg -x`.

### 4.2 Форматы

`.provides.tsv` — все пути замыкания, с указанием пакета-владельца:

```
kind<TAB>path<TAB>pkg
d	/usr/share/cowsay	cowsay
f	/usr/games/cowsay	cowsay
l	/usr/share/doc/cowsay	doc-base
```

`kind` ∈ `d` (каталог), `f` (файл), `l` (симлинк). Строки сортируются по пути
(детерминированный `.stage-hash`).

`.files.tsv` — исполняемые файлы запрошенного пакета, по одному на строку:

```
/usr/games/cowsay
```

`.deps.tsv` — замыкание:

```
name<TAB>version<TAB>arch<TAB>sha256
libc6	2.41-12	amd64	3f2a…
```

`.meta.json`:

```json
{
  "pkg": "cowsay",
  "version": "3.03+dfsg2-8",
  "arch": "amd64",
  "created": "2026-11-02T00:20:11Z",
  "aptx_version": "0.1.0",
  "backend": "bwrap",
  "recommends": false,
  "closure": 42,
  "host_skipped": 118
}
```

Правило записи: `.ready` создаётся **последним**. Отсутствие `.ready` = битая запись,
она удаляется и перестраивается.

### 4.3 Построение записи

1. **Резолв замыкания.** `apt-cache depends --recurse --no-suggests --no-conflicts
   --no-breaks --no-replaces --no-enhances [--no-recommends] <pkg>`. Разбор: берутся
   строки без ведущего пробела (имя пакета) и строки с 4 пробелами под `<virtual>`
   (конкретный провайдер). Отбрасываются `<…>`, суффиксы `:any`, `:amd64`.
2. **Фильтр установленного.** `dpkg-query -W -f='${db:Status-Abbrev}\t${Version}\n' <pkg>`;
   статус, начинающийся с `ii`, — пропускаем (берём с хоста). Счётчик — в `host_skipped`.
3. **Пиннинг.** Если есть `pins.json[pkg]`, соответствующий пакет качается как `pkg=version`.
4. **Скачивание.** `apt-get download` во временном каталоге с прогресс-баром
   (см. «Прогресс загрузки» ниже). Ошибка → `EX_UNAVAILABLE` с подсказкой
   «no network? try `aptx run --offline` with a staged package, or check `apt-get update`».
5. **Распаковка.** `dpkg -x <deb> <root>` для каждого деба.
6. **Манифест.** Для каждого деба: `dpkg-deb -c` → `.provides.tsv`;
   `dpkg-deb -f Package Version Architecture` → `.deps.tsv`; `sha256sum` → `.deps.tsv`.
   Для запрошенного пакета: `dpkg-deb -c` с фильтром режима `^-..x` → `.files.tsv`.
7. **Финализация.** `.meta.json`, затем атомарно `.ready` (`Path.touch()` после всех записей).

Если запись уже готова, но `.meta.json` говорит `"recommends": false`, а запуск
пришёл с `--recommends`, замыкание пересобирается: кэш, построенный по другой
политике резолва, не подходит. Обратный случай (`--recommends` в кэше, без флага
в запуске) кэш устраивает — лишние пакеты не мешают. Битый `.meta.json` трактуется
как «recommends включены», чтобы ошибка метаданных не вызывала повторную загрузку.

`--recommends` следует Recommends ТРАНЗИТИВНО (как `apt-cache depends --recurse`):
для `neovim` это 928 пакетов и 1.2 ГБ против 10 пакетов и 7.5 МБ без флага.

Наличие `.ready` не гарантирует целостность распакованного корня: если файл
пропал из стора, `run` проверяет `store_path(binpath)` и падает с `EX_DATAERR`
(65) и подсказкой `--force-stage`, а не отдаёт пользователю `bwrap: execvp …:
No such file or directory`.

Запись строится в `<store>/<pkg>.tmp.<pid>` и переименовывается в `<store>/<pkg>` в конце.
Параллельный запуск двух aptx на один пакет не ломает стор: проигравший гонку
`rename` видит готовую запись победителя и переиспользует её, а не удаляет.

**Прогресс загрузки.** Сначала `apt-get download --print-uris -y -- <pkgs…>` даёт имена
файлов и их размеры (`'<uri>' <file> <bytes> <hash>`), то есть точный суммарный объём ДО
скачивания. Затем `apt-get download -- <pkgs…>` запускается через `subprocess.Popen` с
выводом в `tempfile.TemporaryFile` (файл, а не пайп — дедлок на полном буфере
невозможен), и раз в 150 мс сумма `st_size` файлов в каталоге загрузки обновляет
прогресс-бар `rich` (`DownloadColumn`, `TransferSpeedColumn`, `TimeRemainingColumn`).
Если `--print-uris` не дал размеров (нет сети, кэш apt), бар тихо отключается и загрузка
идёт через обычный `proc.run`. Загрузчиком остаётся `apt-get`: он уважает
прокси, зеркала и ретраи из конфигурации apt, чего `urllib` не даёт.

### 4.4 Версии и `gc`

- `.last-used` — файл, чей mtime обновляется при каждом `run`/`install`/`which`/`ls`/`shell`.
- `aptx gc` удаляет записи, у которых `mtime(.last-used)` старше `--older-than`,
  и **всегда** — записи без `.ready`.
- `aptx rm-all` удаляет `$APTX_CACHE` целиком плюс все shims.

---

## 5. План

### 5.1 Вход и выход

Вход: `.provides.tsv` записи стора + фактическое состояние хоста (`os.path.lexists`).
Выход: структура, которую печатает `aptx plan` и по которой строится песочница.

```json
{
  "pkg": "cowsay",
  "version": "3.03+dfsg2-8",
  "merge_points": {
    "/usr/games":         {"pkg_children": ["cowsay"]},
    "/usr/share":         {"pkg_children": ["cowsay"]},
    "/usr/share/man/man1":{"pkg_children": ["cowsay.1.gz"]}
  },
  "shadow_dirs":  ["/usr/share/cowsay"],
  "merge_dirs":   ["/usr/games", "/usr/share", "/usr/share/man/man1"],
  "merge_files":  ["/usr/games/cowsay", "/usr/share/man/man1/cowsay.1.gz"],
  "etc_merge":    [],
  "python_dirs":  [],
  "direct_binds": [],
  "conflicts":    []
}
```

### 5.2 Алгоритм

Для каждого пути `p` из `.provides.tsv`:

1. `par = dirname(p)`.
2. Если `par` **не существует** на хосте — поднимаемся вверх, пока родитель не существует;
   получаем **shadow root** `r` (наивысший несуществующий предок `p`).
   Тогда merge-точка — `dirname(r)`, а в ферму добавляется один ребёнок `basename(r)`.
3. Если `par` **существует** на хосте — merge-точка `par`, ребёнок `basename(p)`.

Ферма строится **только на один уровень**: symlink `cowsay → /run/aptx/root/usr/share/cowsay`
разрешается в полное поддерево стора. Рекурсивная ферма не нужна.

Классификация для вывода:

| Поле | Условие |
|---|---|
| `shadow_dirs` | shadow root, которого нет на хосте (поддерево целиком из пакета) |
| `merge_dirs` | merge-точка (каталог существует и на хосте, и в пакете) |
| `merge_files` | путь пакета, чей `basename` уже занят на хосте (файловая коллизия) |
| `etc_merge` | merge-точка под `/etc` |
| `python_dirs` | merge-точка `/usr/lib/python3*/dist-packages` или `site-packages` |
| `direct_binds` | merge-точка `/` (пакет добавляет каталог верхнего уровня, которого нет на хосте) |

### 5.3 Политика приоритетов (зафиксирована в коде и тестах)

| Ситуация | Политика |
|---|---|
| Путь есть только в пакете | symlink на пакет |
| Путь есть только на хосте | symlink на хост |
| Путь есть в обоих, это файл | **пакет побеждает** |
| Путь есть в обоих, это каталог | **пакет побеждает целиком** |
| `/etc/<file>` в пакете и на хосте | пакет побеждает; host-копия доступна как `<file>.host` |
| `/usr/lib/<triplet>/*.so*` | пакет побеждает |

`.host`-копии создаются **только** для `etc_merge` и только когда имя занято.

### 5.4 Кэш фермы

Ключ `.stage-hash` = sha256 от:

```
aptx_version | pkg | version | sorted(merge_point + "\0" + child for …)
```

То есть при изменении состава каталогов хоста (появился/исчез ребёнок в `/usr/share`)
хеш меняется и ферма перестраивается. Скан каталогов для хеша дешевле создания symlink'ов,
поэтому хеш считается всегда, а сборка — только при расхождении.

Ферма живёт в `$STORE/<pkg>/stage/`. Если `.stage-hash` совпал — переиспользуется.

Перестройка держит эксклюзивный `flock` на каталоге записи стора и после
получения блокировки перепроверяет хеш: два одновременных `aptx run` на один
пакет не мешают друг другу, проигравший гонку видит готовую ферму победителя и
ничего не удаляет.

---

## 6. Песочница

### 6.1 Внутренние пути (контракт)

| Путь в песочнице | Что это |
|---|---|
| `/run/aptx/root` | ro-bind всего распакованного корня стора |
| `/run/aptx/host<dir>` | ro-bind хостового каталога для merge-точки `<dir>` |
| `$STAGE<dir>` | ферма merge-точки, биндится в `<dir>` |

Симлинки фермы абсолютные и указывают на эти пути, поэтому ферма переносима между
запусками и кэшируется.

### 6.2 Скелет команды

```bash
bwrap \
  --tmpfs / \
  --tmpfs /usr --tmpfs /usr/bin --tmpfs /usr/sbin --tmpfs /usr/lib \
  --tmpfs /usr/lib64 --tmpfs /usr/libexec --tmpfs /usr/share --tmpfs /usr/local \
  --tmpfs /etc --tmpfs /var --tmpfs /var/cache --tmpfs /var/log --tmpfs /var/tmp \
  --tmpfs /var/lib --tmpfs /run --tmpfs /opt \
  --ro-bind "$STORE_ROOT" /run/aptx/root \
  # для каждой merge-точки mp:
  --ro-bind /host<mp> /run/aptx/host<mp> \
  --ro-bind "$STAGE<mp>" <mp> \
  # для каждой базовой директории, не покрытой merge-точкой:
  --ro-bind-try /usr/bin /usr/bin  … \
  --ro-bind /etc/passwd /etc/passwd --ro-bind /etc/group /etc/group \
  --ro-bind-try /etc/nsswitch.conf /etc/nsswitch.conf \
  --ro-bind /etc/hosts /etc/hosts --ro-bind-try /etc/resolv.conf /etc/resolv.conf \
  --ro-bind-try /etc/ssl /etc/ssl --ro-bind-try /etc/localtime /etc/localtime \
  --ro-bind-try /etc/ld.so.cache /etc/ld.so.cache \
  --ro-bind-try /etc/ld.so.conf /etc/ld.so.conf --ro-bind-try /etc/ld.so.conf.d /etc/ld.so.conf.d \
  --ro-bind-try /etc/alternatives /etc/alternatives \
  --symlink usr/bin /bin --symlink usr/sbin /sbin \
  --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
  --tmpfs "/home/$USER" --tmpfs /root \
  --ro-bind "$PWD" "$PWD"          # или --bind, если --rw-cwd
  --proc /proc --dev /dev \        # ОБЯЗАТЕЛЬНО до биндов устройств
  # далее --dev-bind/--dev-bind-try для /dev/dri, /dev/kfd, --dev-bind и
  # устройств, названных в аргументах программы
  --clearenv --setenv PATH … --setenv HOME … \
  --unshare-pid --unshare-ipc --unshare-uts --die-with-parent \
  --unshare-user \                 # только не под root
  --cap-add ALL \                  # только под root
  --chdir "$PWD" -- "$BIN" "$@"
```

Под root user namespace **не создаётся**, а вместо него добавляется
`--cap-add ALL`. Причина: ioctl к устройству спрашивает `capable()`, а та
проверяет **начальный** user namespace — вложенный снимает привилегию, как бы ни
был велик uid. Заодно root без `CAP_DAC_OVERRIDE` не может пройти в чужой
домашний каталог с правами `700`. bwrap по умолчанию сбрасывает все
capabilities, поэтому их приходится просить обратно. Изоляция файловой системы
(частный mount namespace) при этом сохраняется, но границей безопасности
песочница под root не является — `aptx doctor` так и пишет.

Флаги по условиям: `--unshare-net` (`--offline`), `--dev-bind /dev/dri /dev/dri` +
`--dev-bind-try /dev/kfd /dev/kfd` (`--gpu`), `--new-session` (только неинтерактивный запуск —
иначе ломается job control в `aptx shell`).

### 6.3 Окружение

`--clearenv`, затем:

```
PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin:/usr/local/games:/usr/games
HOME=/home/$USER
USER, LOGNAME, SHELL=/bin/sh
TERM (из хоста, default xterm-256color)
LANG, LC_ALL (из хоста, default C.UTF-8)
TZ (если задан на хосте)
TMPDIR=/tmp
XDG_CACHE_HOME=$HOME/.cache  XDG_CONFIG_HOME=$HOME/.config
XDG_DATA_HOME=$HOME/.local/share  XDG_STATE_HOME=$HOME/.local/state
```

`/usr/games` в `PATH` обязателен: там `cowsay`. `LD_PRELOAD`, `PYTHONPATH`, `LD_LIBRARY_PATH`
хоста не протекают — это прямое следствие `--clearenv`.

### 6.4 Изоляция записи

| Место | По умолчанию | Флаг |
|---|---|---|
| `$HOME` | tmpfs | `--persist` → bind `state/default` или `$CACHE/homes/NAME` |
| `/tmp`, `/var/tmp` | tmpfs | — |
| `/var/cache`, `/var/log`, `/var/lib`, `/run` | tmpfs | — |
| `/etc` | tmpfs + ro-копии нужных файлов | — |
| `$PWD` | ro-bind | `--rw-cwd` |
| произвольный путь | — | `--rw HOST:TARGET` |
| устройства | нет | `--gpu`, `--dev-bind`, `--dev`, устройства из аргументов программы |
| стор | на диске | `--ephemeral` → tmpfs |

`--ephemeral`: стор копируется в `tmpfs` (`mktemp -d` под `$TMPDIR`), запуск идёт оттуда,
после выхода каталог удаляется. Ничего не остаётся на диске.

### 6.5 proot (fallback)

```
proot -0 -r / -b <store_root>/usr/share/cowsay:/usr/share/cowsay -b … -w "$PWD" -- "$BIN" "$@"
```

Используется, если bwrap недоступен или задан `--backend=proot`. Отличия: нет
`--unshare-*`, изоляция `$HOME` эмулируется `-b $STAGE/home:/home/$USER`. Медленнее на
syscall-тяжёлых нагрузках — предупреждение в `--verbose`.

### 6.6 `--no-sandbox`

Прямой `exec` бинаря из стора с `PATH`-префиксом и `LD_LIBRARY_PATH` (поведение v1).
Пути не работают, следы остаются. Предназначен только для отладки и для случая
«bwrap сломался, надо срочно запустить». Пишет в stderr предупреждение.

---

## 7. Shims

`aptx install cowsay` создаёт `$BIN_DIR/cowsay`:

```sh
#!/bin/sh
# aptx shim for cowsay (package: cowsay, bin: /usr/games/cowsay)
# Managed by aptx. Remove with: aptx remove cowsay
APTX_BIN="/home/jezv/.local/bin/aptx"
if ! command -v aptx >/dev/null 2>&1; then
  exec "$APTX_BIN" run cowsay cowsay "$@"
fi
exec aptx run cowsay cowsay "$@"
```

Правила:

- файл создаётся с режимом `0755`;
- идемпотентность: повторный `install` перезаписывает **свой** shim;
- чужой файл на этом месте (нет маркера `# Managed by aptx`) → `EX_CANTCREAT`, если не `--force`;
- `remove` удаляет только файлы с маркером `# Managed by aptx`;
- `aptx list` показывает `orphan shims` — маркер есть, пакета в сторе нет.

---

## 8. doctor

Проверки, каждая — `ok`/`warn`/`fail`:

| Проверка | Условие ok |
|---|---|
| python | ≥ 3.11 |
| bwrap | найден в PATH, `--version` ≥ 0.6 |
| userns | `user.max_user_namespaces` > 0, нет `kernel.unprivileged_userns_clone=0` |
| bwrap smoke | `bwrap --ro-bind / / --dev /dev --proc /proc -- true` возвращает 0 |
| apt | `apt-cache`, `apt-get` в PATH |
| dpkg | `dpkg`, `dpkg-deb` в PATH |
| store | каталог создаваем и записываем |
| bin dir | `$BIN_DIR` существует или создаваем, лежит в `PATH` |
| net | `apt-get download` возможен (наличие `*.Packages` в `/var/lib/apt/lists`) |
| /dev/dri | существует (для `--gpu`), иначе `warn` |
| /dev/kfd | существует (ROCm), иначе `warn` |
| root | не root (root — `warn`) |

`fail` хотя бы в одной критичной проверке (`python`, `bwrap`, `userns`, `apt`, `dpkg`) →
код `EX_UNAVAILABLE`. `warn` не влияет на код.

---

## 9. Коды ошибок и сообщения

Все сообщения aptx — в stderr, с префиксом `aptx: `. Формат:

```
aptx: error: <что случилось>
aptx: hint: <что сделать>
```

| Код | Имя | Когда |
|---|---|---|
| 0 | OK | успех |
| 2 | EX_USAGE | неверные аргументы, неизвестная команда, несколько бинарей без выбора |
| 64 | EX_USAGE_ENV | неверное окружение: нет `$HOME` и не заданы `APTX_CACHE`/`APTX_BIN` |
| 65 | EX_DATAERR | пакет не найден в apt, резолв замыкания не дал результата |
| 66 | EX_NOINPUT | `which`/`ls`: пакет не стейджен, бинарь не найден |
| 69 | EX_UNAVAILABLE | нет сети, `apt-get download` упал, bwrap недоступен |
| 70 | EX_SOFTWARE | внутренняя ошибка (битый манифест, невозможный план) |
| 71 | EX_OSERR | ошибка ФС/прав при распаковке или сборке фермы |
| 73 | EX_CANTCREAT | не удалось создать shim (чужой файл), стор не записываем |

Код возврата программы пробрасывается как есть. Если программа завершилась с кодом из
таблицы выше, отличить её ошибку от ошибки aptx можно по наличию `aptx: ` в stderr.

---

## 10. Этапы и критерии готовности

| Этап | Содержание | Готово, когда |
|---|---|---|
| **P0** | `doctor`, стор, манифест, `ls`/`which`/`rm-all`/`gc`/`store` | `aptx ls cowsay` → `/usr/games/cowsay`; `doctor` зелёный; `rm-all` чистит |
| **P1** | песочница без теней: tmpfs-корень, базовые binds, изоляция `$HOME`/`/tmp` | `aptx run ripgrep rg --version` работает; `find ~ -newer marker` пусто |
| **P2** | план + тени + симлинк-ферма | **`aptx run cowsay "hi"` печатает корову** |
| **P3** | `--persist`, `--rw`, `--rw-cwd`, `--ephemeral`, `/etc` merge | конфиг в `/etc` не портит хост; `--persist` переживает перезапуск |
| **P4** | `--gpu`, `--offline`, `shell`, `--backend=proot` | шелл с fio в PATH; `--offline` рубит сеть |
| **P5** | кэш фермы, `gc --older-than`, `store du`, `pin` | повторный запуск < 100 мс накладных |
| **P6** | pytest, README, `--help`, коды ошибок | набор §11 проходит |

---

## 11. Тесты

### 11.1 Приёмочные (ручные, из PROJECT.md §7)

| # | Команда | Ожидание |
|---|---|---|
| 1 | `aptx run ripgrep rg --version` | версия, код 0 |
| 2 | `aptx run fio fio --name=t --rw=read --size=8M --output=/tmp/o` | отчёт в песочнице, в `$PWD` пусто |
| 3 | `aptx run cowsay "hi"` | корова, код 0 |
| 4 | `aptx run smartctl -a /dev/nvme0n1` | без `--dev-bind` — ошибка доступа; с ним — работает |
| 5 | пакет с `dist-packages` | импорт модулей пакета работает |
| 6 | любой запуск, затем `find $HOME -newer /tmp/marker` | пусто |
| 7 | `aptx run foo` дважды | второй запуск не качает |
| 8 | `aptx run foo` без сети | внятная ошибка |
| 9 | `aptx --ephemeral run ripgrep rg --version` | `~/.cache/aptx` пуст |
| 10 | `aptx plan cowsay` | 5–10 строк плана |

### 11.2 pytest

`tests/` — без сети, без root, без реального bwrap там, где можно обойтись моками:

| Файл | Что проверяет |
|---|---|
| `test_plan.py` | shadow root, merge-точки, приоритеты, `.host`-копии, конфликты, `direct_binds` |
| `test_manifest.py` | TSV/JSON round-trip, сортировка, атомарность `.ready` |
| `test_store.py` | build из поддельного деба (создаётся `dpkg-deb` или мокается), gc по возрасту, rm-all |
| `test_stage.py` | ферма: пакет побеждает, host-ребёнок симлинкуется, хеш-кэш, `.host` |
| `test_sandbox.py` | сборка argv bwrap: порядок, `--clearenv`, PATH с `/usr/games`, `--offline`/`--gpu` |
| `test_shim.py` | создание/перезапись/чужой файл/удаление/list, права 0755 |
| `test_cli.py` | разбор флагов: всё после пакета уходит программе; коды ошибок |
| `test_doctor.py` | вердикты при разных моках окружения |

Интеграционные (помечены `@pytest.mark.integration`, требуют bwrap и сеть):
`test_integration.py` — `doctor`, `cowsay`, `ripgrep`, изоляция `$HOME`.

---

## 12. Структура кода

```
pyproject.toml
SPEC.md
README.md
src/aptx/
├── __init__.py        # __version__
├── __main__.py        # python -m aptx
├── cli.py             # argparse, диспетчеризация, коды выхода
├── config.py          # пути, env, константы песочницы
├── errors.py          # AptxError + коды
├── proc.py            # обёртка subprocess с логированием
├── fetch.py           # резолв замыкания, download, dpkg -x, dpkg-deb -c
├── store.py           # запись стора, манифест, gc, du, pins
├── plan.py            # построение плана
├── stage.py           # симлинк-ферма + кэш
├── sandbox.py         # bwrap / proot / no-sandbox, сборка argv
├── shim.py            # install / remove / list
└── doctor.py          # проверки окружения
tests/
```

Зависимости рантайма: **click ≥ 8.1**, **rich ≥ 13**. Dev: `pytest`, `coverage`.

---

## 13. Известные ограничения

Наследуются из `PROJECT.md` §9, плюс уточнения:

1. **Ферма больших каталогов.** `/usr/bin` (1439 записей) и `/usr/lib/x86_64-linux-gnu`
   (1701) при попадании в merge-точки дают ~3 тыс. symlink'ов. Первый запуск — 50–150 мс,
   дальше кэш `.stage-hash`. Если пакет не трогает эти каталоги — они просто ro-биндятся.
2. **Симлинк-ферма ломает `readlink`-навигацию**: программа, читающая `/proc/self/exe`,
   увидит `/run/aptx/root/...`. На поведение подавляющего большинства утилит не влияет.
3. **`--persist` + `$HOME`**: если `$PWD` находится внутри `$HOME`, ro-bind `$PWD`
   перекрывает tmpfs-`$HOME`; при `--persist` это ожидаемо, при обычном запуске —
   предупреждение в `--verbose`.
4. **Кросс-релизные пакеты**: предупреждение, если версия `libc6` в сторе ≠ хостовой.
5. **`apt-get download`** использует хостовый `/var/lib/apt/lists` только на чтение;
   при параллельном `apt update` — редкие гонки, лечатся повтором.
6. **`--ephemeral`** требует места в `$TMPDIR` на весь распакованный стор.
7. **`aptx shell` без `--new-session`**: внутри песочницы доступен TIOCSTI-ввод в
   родительский терминал. Для интерактивного шелла это осознанный компромисс
   (иначе нет job control); для `run` `--new-session` включён.
