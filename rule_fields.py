# -*- coding: utf-8 -*-
"""
Извлечение полей ПРАВИЛАМИ, по координатам OCR-блоков — без языковой модели.

Зачем: модель возвращает неполный набор полей, и набор меняется от прогона к
прогону. При этом бОльшая часть документа устроена геометрически очевидно:
подпись слева, значение справа на той же строке ("RRN  617486005488"), либо
строка таблицы "показатель | код | значение | значение". Такое берётся
детерминированно и всегда одинаково.

Модуль ничего не знает про конкретные документы: работает с любым набором
блоков вида {'text', 'left', 'top', 'width', 'height'}.
"""
import re

# Метка версии: видно, какой файл реально залит в контейнер.
#   docker exec -it <ID> python3 -c "import rule_fields; print(rule_fields.__version__)"
__version__ = '2026-09-22e сетка + огрызки из длинной пунктуации'

# Подмены, которые OCR делает В ЧИСЛАХ (в тексте их применять нельзя).
_DIGIT_CONFUSIONS = str.maketrans({
    'l': '1', 'I': '1', '|': '1', '!': '1', 'і': '1', ']': '1',
    'з': '3', 'З': '3', 'б': '6',
    'О': '0', 'о': '0', 'O': '0', 'o': '0',
})
_DASHES = {'-', '–', '—', '‒'}


def _text(cell):
    return ' '.join(b.get('text', '') for b in cell).strip()


def _median_height(blocks):
    heights = sorted(b.get('height', 0) for b in blocks)
    return heights[len(heights) // 2] if heights else 0


def _is_frame_scrap(block, медиана):
    """
    Обрывок рамки, принятый за букву.

    После стирания сетки от вертикальной линии остаётся штрих высотой в
    полторы-две строки, и Tesseract читает его как 'T' или '1' с низкой
    уверенностью. Текстом он не является, зато мешает всерьёз: такой блок
    перекрывает сразу две строки таблицы и склеивает их в одну — заголовок
    раздела уезжал в подпись соседнего показателя («II. КРАТКОСРОЧНЫЕ АКТИВЫ
    Запасы»). Отличаем по несоразмерной высоте при одном-двух символах:
    настоящие короткие ячейки ('6', '5', '-') ростом с обычную строку.
    """
    if not медиана:
        return False
    текст = str(block.get('text', '')).strip()
    return len(текст) <= 2 and block.get('height', 0) > 1.6 * медиана


def group_lines(blocks):
    """Блоки -> строки документа (по вертикальному перекрытию)."""
    items = [b for b in blocks if str(b.get('text', '')).strip()]
    items = [b for b in items if not _is_frame_scrap(b, _median_height(items))]
    lines = []
    for b in sorted(items, key=lambda b: (b.get('top', 0), b.get('left', 0))):
        top, bottom = b.get('top', 0), b.get('top', 0) + b.get('height', 0)
        for line in lines:
            l_top = min(q.get('top', 0) for q in line)
            l_bottom = max(q.get('top', 0) + q.get('height', 0) for q in line)
            overlap = min(bottom, l_bottom) - max(top, l_top)
            shortest = min(bottom - top, l_bottom - l_top) or 1
            # Одного перекрытия мало: у заголовка раздела рамка бывает высокой,
            # и он затягивал в свою строку соседнюю («II. КРАТКОСРОЧНЫЕ АКТИВЫ»
            # склеивалось с «Запасы»). Поэтому ещё требуем, чтобы совпадали
            # середины по вертикали.
            центры = abs((top + bottom) / 2 - (l_top + l_bottom) / 2)
            # Порог меряем по высоте УЖЕ НАБРАННОЙ СТРОКИ, а не по меньшему
            # из двух. Прочерк в пустой ячейке — чёрточка в несколько
            # пикселей, её середина неизбежно отстоит от середины строки на
            # пол-высоты цифры, и по меньшему порогу строка баланса
            # разваливалась на три (код отдельно, прочерк за 2026 отдельно,
            # прочерк за 2025 отдельно) — значения обеих граф пропадали.
            # Для блока ВЫШЕ строки порог тот же, что и был: высота строки и
            # есть меньшая из двух, так что заголовок раздела соседнюю строку
            # по-прежнему не затягивает.
            if overlap > 0.4 * shortest and центры < 0.5 * (l_bottom - l_top or 1):
                line.append(b)
                break
        else:
            lines.append([b])
    for line in lines:
        line.sort(key=lambda b: b.get('left', 0))
    lines.sort(key=lambda line: min(b.get('top', 0) for b in line))
    return lines


def split_cells(line, gap_factor=1.2):
    """
    Строка -> ячейки. Режем там, где горизонтальный провал заметно больше
    обычного пробела. Порог берём от высоты строки, а не в пикселях, — чтобы
    работало и на 300 dpi скане, и на мелком превью.
    """
    if not line:
        return []
    heights = sorted(b.get('height', 0) for b in line)
    line_h = heights[len(heights) // 2] or 1
    gap_limit = gap_factor * line_h

    cells, current = [], [line[0]]
    for prev, block in zip(line, line[1:]):
        gap = block.get('left', 0) - (prev.get('left', 0) + prev.get('width', 0))
        if gap > gap_limit:
            cells.append(current)
            current = [block]
        else:
            current.append(block)
    cells.append(current)
    return cells


_THOUSAND_SEP_RE = re.compile('(?<=\\d)[\\s  ](?=\\d{3}\\b)')
_NUMBER_RE = re.compile(r'^\d+([.,]\d{1,2})?$')


def normalize_number(text):
    """
    Числовая ячейка -> нормализованное число; '—' для прочерка; None если не число.

    Разделитель тысяч (пробел, и точка/запятая перед ровно тремя цифрами)
    выбрасываем, десятичный — сохраняем: '19 766 663' -> '19766663',
    но '1.2' -> '1.2' и '17.00' -> '17.00'.
    """
    s = str(text).strip()
    if not s:
        return None
    if s in _DASHES:
        return '—'
    # Сначала чиним путаницу OCR, потом срезаем хвостовую пунктуацию: иначе
    # ']' в '5]' успеет уйти как мусор, хотя это распознанная единица.
    s = s.translate(_DIGIT_CONFUSIONS)
    s = s.rstrip('.,;:\u00bb\u201d\'"')
    s = _THOUSAND_SEP_RE.sub('', s)
    s = re.sub(r'(?<=\d)[.,](?=\d{3}\b)', '', s)
    # Оставшиеся пробелы НЕ выбрасываем: разделитель тысяч уже убран выше,
    # а всё, что осталось, — след слипшихся ячеек ('110 1169'). Выбросив
    # его, мы выдавали ошибку разбора за правдоподобное число '1101169'.
    if s in _DASHES:
        return '—'
    if not _NUMBER_RE.match(s):
        return None
    return s.replace(',', '.')


def looks_like_number(text):
    return normalize_number(text) not in (None, '—')


def _is_label(text):
    """Подпись поля: содержит буквы, не слишком длинная, не сама по себе число."""
    letters = sum(ch.isalpha() for ch in text)
    return 3 <= len(text) <= 80 and letters >= 3 and not looks_like_number(text)


_JUNK_EDGE = '|_~`·•!' + ' \t'    # '*' не трогаем: им маскируют номер карты
_JUNK_LEAD = _JUNK_EDGE + '\'"\u201c\u201d\u2018\u2019\u201e\u201a,'   # плюс низкие кавычки: от сетки остаётся '„' и '‚'


def _drop_lone_paren(text):
    """
    Убирает скобку по краю, которой не хватает пары.

    От стёртой сетки остаются обрывки, и Tesseract читает их как скобку:
    '(1169', '232)'. Парные скобки не трогаем — в бухгалтерии ими обозначают
    отрицательное число, и «(726)» это значимая запись, а не мусор.
    """
    if text.count(')') > text.count('(') and text.endswith(')'):
        text = text[:-1].rstrip()
    if text.count('(') > text.count(')') and text.startswith('('):
        text = text[1:].lstrip()
    return text


def _clean(text):
    """
    Срезает по краям обрывки стёртых линий разметки.

    После удаления сетки от рамок остаются короткие хвосты, и Tesseract читает
    их как одиночную пунктуацию: '| Организация', "' 191 766 663", '(1169'.
    Внутри строки не трогаем ничего — там пунктуация значимая.
    """
    return _drop_lone_paren(text.strip(_JUNK_EDGE).lstrip(_JUNK_LEAD).strip())


def _cell_bounds(cell):
    left = min(b.get('left', 0) for b in cell)
    right = max(b.get('left', 0) + b.get('width', 0) for b in cell)
    return left, right


def _block_bounds(block):
    left = block.get('left', 0)
    return left, left + block.get('width', 0)


def _median(values):
    ordered = sorted(values)
    return ordered[len(ordered) // 2] if ordered else 0


def detect_columns(lines, min_members=3, overlap_share=0.3):
    """
    Находит колонки таблицы по вертикальному выравниванию ЧИСЛОВЫХ ячеек.

    По пробелам ищем только КАНДИДАТОВ, колонкой считаем то, что повторяется
    из строки в строку и перекрывается по горизонтали, и берём только числовые
    ячейки. Числовые — потому что в бланках данные всегда числа, а куски
    подписей («по разделу», «of measure») фантомных колонок не создают.

    Возвращает список (left, right), слева направо.
    """
    кандидаты = []
    for line in lines:
        for cell in split_cells(line)[1:]:      # первая ячейка строки — подпись
            if normalize_number(_clean(_text(cell))) is not None:
                кандидаты.append(_cell_bounds(cell))

    колонки = []
    for left, right in sorted(кандидаты):
        for кол in колонки:
            общее = min(right, кол['right']) - max(left, кол['left'])
            короче = min(right - left, кол['right'] - кол['left']) or 1
            if общее > overlap_share * короче:
                кол['left'] = min(кол['left'], left)
                кол['right'] = max(кол['right'], right)
                кол['n'] += 1
                break
        else:
            колонки.append({'left': left, 'right': right, 'n': 1})

    return [(k['left'], k['right']) for k in колонки if k['n'] >= min_members]


def split_by_columns(line, columns):
    """Строка -> (подпись, {номер колонки: ячейка}) по границам колонок."""
    if not columns:
        return _text(line), {}
    первая = columns[0][0]
    подпись, ячейки = [], {}
    for block in sorted(line, key=lambda b: b.get('left', 0)):
        left, right = _block_bounds(block)
        if (left + right) / 2 < первая:
            подпись.append(block)
            continue
        лучшая, лучшее = None, 0
        for i, (c_left, c_right) in enumerate(columns):
            общее = min(right, c_right) - max(left, c_left)
            if общее > лучшее:
                лучшая, лучшее = i, общее
        if лучшая is None:                      # между колонками — считаем подписью
            подпись.append(block)
        else:
            ячейки.setdefault(лучшая, []).append(block)
    return _clean(_text(_label_run(подпись))), ячейки


def _label_run(blocks):
    """
    Подпись без обрывков стёртой сетки.

    Обрывок лежит в провале между графами, ни в одну колонку не попадает и
    потому сваливался в подпись: «Основные средства .», «Вложения в
    долгосрочные активы | | 7», «ИТОГО по разделу П 7». Выбрасываем куски в
    куски без единой буквы. По длине отсеивать нельзя: в бланке подпись бывает
    разрежена («ИТОГО   по   разделу   I»), каждое слово становится отдельной
    ячейкой, и от названия осталось бы одно слово.
    """
    def значимая(cell, первая):
        текст = _clean(_text(cell))
        if not any(ch.isalpha() for ch in текст):
            return False                    # '|', '_', '.', '=', '7', '10_'
        # Буква с точкой в один-два знака ('о.') — тоже обрывок рамки, но
        # букву в себе несёт. Настоящее короткое слово знаков препинания не
        # содержит: 'по', 'I' в «ИТОГО по разделу I» остаются.
        return первая or len(текст) > 2 or текст.isalnum()

    cells = split_cells(blocks)
    осмысленные = [c for i, c in enumerate(cells) if значимая(c, i == 0)]
    if not осмысленные:
        return cells[0] if cells else blocks
    return [b for c in осмысленные for b in c]


_FORM_DATE_RE = re.compile(r'\b\d{1,2}[.\-/]\d{1,2}[.\-/](19|20)\d{2}\b')
# Год графы. Границу слова требовать нельзя: от стёртой линейки к году
# прилипает подчёркивание ('_2026 г.'), а '_' — часть слова, и '\\b' перед
# цифрой не срабатывает. Отсекаем только соседние ЦИФРЫ, чтобы не поймать
# кусок длинного числа.
_YEAR_RE = re.compile(r'(?<!\d)(19|20)\d{2}(?!\d)')


def _column_years(lines, columns, table_top, skip=None):
    """
    Имя колонки из шапки: «На 30 июня 2026 г.» -> «2026».

    Шапку разбираем по пробелам, а не по границам колонок: подпись колонки
    шире самой колонки и сдвинута относительно чисел (числа прижаты вправо,
    подпись идёт по центру), так что отдельное слово «2026» запросто окажется
    за границей колонки и год потеряется.
    """
    names = [None] * len(columns)
    шапка = [line for line in lines
             if min(b.get('top', 0) for b in line) < table_top]
    # Идём СНИЗУ ВВЕРХ: подпись колонки стоит прямо над таблицей, а вверху
    # листа есть ссылка на норматив «12.12.2016 № 104» — её год попадал в
    # имена колонок первым и держался там, потому что перезаписи нет.
    for line in reversed(шапка):
        for cell in split_cells(line):
            # именно _clean: от стёртой рамки к году липнет подчёркивание
            # ('_2026 г.'), и '\b' перед цифрой уже не срабатывает
            текст = _clean(_text(cell))
            if _FORM_DATE_RE.search(текст):
                continue                    # «12.12.2016» — дата бланка, не год графы
            год = _YEAR_RE.search(текст)
            if not год:
                continue
            i = _best_column(cell, columns, skip=skip)
            if i is None:
                i = _nearest_column(cell, columns, skip=skip)
            if i is not None and names[i] is None:
                names[i] = год.group(0)
    return names


def _nearest_column(cell, columns, skip=None):
    """Ближайшая колонка, если ячейка ни с одной не перекрывается."""
    if not columns:
        return None
    left, right = _cell_bounds(cell)
    центр = (left + right) / 2
    ширина = max(c_right - c_left for c_left, c_right in columns) or 1
    лучшая, лучшее = None, None
    for i, (c_left, c_right) in enumerate(columns):
        if i == skip:
            continue
        d = abs((c_left + c_right) / 2 - центр)
        if лучшее is None or d < лучшее:
            лучшая, лучшее = i, d
    return лучшая if лучшее is not None and лучшее <= 2 * ширина else None


def _is_row_code_column(cells):
    """Колонка кодов строк формы: почти всё — трёхзначные числа 100..999."""
    numbers = [normalize_number(_clean(_text(c))) for c in cells]
    codes = [n for n in numbers
             if n and n.isdigit() and len(n) == 3 and 100 <= int(n) <= 999]
    return len(numbers) >= 4 and len(codes) >= 0.6 * len(numbers)


def _looks_like_value(text):
    """
    Отсеиваем мусор: пустое, одиночную пунктуацию, слишком длинное, а также
    короткие буквенные огрызки ('aa', 'т') — они остаются от стёртой сетки и
    от печатей. Короткие ЧИСЛА при этом сохраняем: '6' и '5' в балансе —
    настоящие значения.
    """
    if not text or len(text) > 120 or not any(ch.isalnum() for ch in text):
        return False
    return len(text) >= 3 or any(ch.isdigit() for ch in text)


def _read_line(line, columns):
    """
    Разбирает одну строку. Возвращает (подпись, {колонка: ячейка}, таблица?)
    либо None, если строка полем не является.

    Строка таблицы режется по границам колонок. Но так можно резать не всё:
    в шапке документа значение — сплошной текст на всю ширину («Организация |
    Общество с ограниченной ответственностью ...»), и по колонкам он развалился
    бы на куски. Поэтому если содержимое справа от подписи не числовое, строка
    разбирается по пробелам как обычная пара «название — значение».
    """
    подпись, ячейки = split_by_columns(line, columns)
    числовых = sum(1 for c in ячейки.values()
                   if normalize_number(_clean(_text(c))) is not None)
    if len(ячейки) >= 2 and числовых >= len(ячейки) / 2 and _is_label(подпись):
        return подпись, ячейки, True

    cells = split_cells(line)
    if len(cells) > 2:
        # Каждый обрывок сетки режется в отдельную ячейку, и пара переставала
        # быть парой: «Организация | 7 | Общество с ограниченной ...» — поле
        # пропадало целиком. Выкидываем огрызки в один-два символа, но только
        # когда ячеек и так больше двух: иначе пострадало бы короткое
        # настоящее значение («Контакт-центр ЕРИП: 141»).
        # Считаем БУКВЫ И ЦИФРЫ, а не длину: от стёртой линейки остаются
        # огрызки длиннее двух знаков («—___.»), и по длине они проходили за
        # содержимое — из-за одного такого пропадало поле «Вид экономической
        # деятельности» целиком.
        значимо = lambda c: sum(ch.isalnum() for ch in _clean(_text(c))) > 2
        осмысленные = [c for c in cells if значимо(c)]
        if len(осмысленные) == 2:
            cells = осмысленные
    if len(cells) != 2:
        # Пара может быть разделена не пробелом, а двоеточием: «RRN: 2038990679»,
        # «Код авторизации: 542121». Пробел там обычный, по ширине провала такую
        # строку не разрежешь — и весь блок «Детали платежа» на чеке пропадал.
        по_двоеточию = _split_at_colon(line)
        if по_двоеточию:
            cells = по_двоеточию

    if len(cells) == 2:
        # Двоеточие и запятая в конце подписи — часть разделителя, а не имени
        подпись = _clean(_text(cells[0])).rstrip(' :,;')
        if _is_label(подпись):
            # Ячейку привязываем к колонке, если она в неё помещается: иначе
            # в строке, где все значения — прочерки, единственной уцелевшей
            # ячейкой остаётся код строки, и он уходит в поля как значение.
            номер = _best_column(cells[1], columns, contained=True)
            return подпись, {номер: cells[1]}, False
    return None


def _split_at_colon(line):
    """
    Режет строку на подпись и значение по двоеточию: [подпись], [значение].

    Двоеточием считаем только то, которым ЗАКАНЧИВАЕТСЯ слово. Внутри времени
    ('09:41:40') двоеточия стоят посреди блока, и такую строку мы не трогаем.
    Если подписи или значения не получилось — возвращаем None, строка идёт
    прежним путём.
    """
    for i, block in enumerate(line):
        if not str(block.get('text', '')).rstrip().endswith(':'):
            continue
        подпись = line[:i + 1]
        значение = line[i + 1:]
        if not значение:
            return None
        текст = _clean(_text(подпись)).rstrip(':').strip().rstrip(',').strip()
        if not _is_label(текст) or not _looks_like_value(_clean(_text(значение))):
            return None
        return [подпись, значение]
    return None


def _best_column(cell, columns, skip=None, contained=False):
    """
    Колонка, с которой ячейка перекрывается сильнее всего.

    contained=True дополнительно требует, чтобы ячейка помещалась в колонку:
    так значение шапки документа («220040, г. Минск, ул. Некрасова...»),
    растянутое на всю ширину, не приписывается случайной колонке таблицы.
    """
    left, right = _cell_bounds(cell)
    лучшая, лучшее = None, 0
    for i, (c_left, c_right) in enumerate(columns or []):
        if i == skip:
            continue
        общее = min(right, c_right) - max(left, c_left)
        if общее <= лучшее:
            continue
        if contained:
            # Ячейка считается «этой колонки», если начинается в её пределах и
            # не сильно шире её. Требовать полного вхождения нельзя: к числу
            # прилипает хвост от стёртой рамки ('212 Т'), и ячейка вылезает за
            # правый край. А растянутое на всю ширину значение шапки документа
            # («220040, г. Минск, ул. Некрасова...») этой проверки не проходит.
            ширина = c_right - c_left
            if left < c_left - 0.5 * ширина or (right - left) > 2.5 * ширина:
                continue
        лучшая, лучшее = i, общее
    return лучшая


_ROMAN_HEAD = re.compile(r'^[IVXА-Я]{1,4}[.)]\s')


def _is_section_heading(text):
    """
    Заголовок раздела или пояснение, а не продолжение подписи.

    «I. ДОЛГОСРОЧНЫЕ АКТИВЫ», «II. КРАТКОСРОЧНЫЕ АКТИВЫ», «в том числе:» стоят
    отдельной строкой без чисел — ровно как перенесённый кусок подписи. Но
    относятся они сразу к нескольким строкам, и приклеивать их к соседней
    нельзя.
    """
    text = text.strip()
    if text.endswith(':'):
        return True
    if _ROMAN_HEAD.match(text):
        return True
    буквы = [ch for ch in text if ch.isalpha()]
    return bool(буквы) and sum(ch.isupper() for ch in буквы) >= 0.7 * len(буквы)


def _next_table_row(разбор, i, шаг):
    """Ближайшая строка ТАБЛИЦЫ в заданную сторону, если по пути нет другой."""
    j = i + шаг
    while 0 <= j < len(разбор):
        разобрано = разбор[j][1]
        if разобрано is not None:
            return j if разобрано[2] else None
        j += шаг
    return None


def _label_reach(разбор, columns, table_top):
    """
    Самая дальняя правая граница подписи В ТАБЛИЦЕ.

    Нужна, чтобы отличить ПЕРВУЮ строку переноса от второй. Подпись
    переносится ровно потому, что не влезла в графу, — значит её первая
    строка дотянута почти до правого края, а остаток на второй строке
    заметно короче.

    Считаем только по строкам таблицы: в шапке документа значение идёт
    сплошным текстом через всю страницу, и его куски задрали бы планку.
    """
    граница = columns[0][0]
    правые = []
    for line, _ in разбор:
        if min(b.get('top', 0) for b in line) < table_top:
            continue
        свои = [b for b in line
                if _block_bounds(b)[1] <= граница
                and any(ch.isalpha() for ch in _clean(str(b.get('text', ''))))]
        if свои:
            правые.append(max(_block_bounds(b)[1] for b in свои))
    return max(правые) if правые else 0


def _join_wrapped_labels(разбор, columns, table_top):
    """
    Приклеивает к строке таблицы кусок подписи, перенесённый на соседнюю.

    В бланке длинное название не влезает в графу и переносится: «Долгосрочные
    финансовые / вложения», «Налог на добавленную стоимость по приобретенным /
    товарам, работам, услугам». Числа стоят только на одной из двух строк,
    вторая приходит вообще без ячеек — полем она не становилась и пропадала, а
    поле получало обрубленное имя.

    Берём только то, что стоит В ТАБЛИЦЕ, левее первой колонки и вплотную к
    своей строке; заголовки разделов отсекает _is_section_heading.
    """
    граница = columns[0][0] if columns else None
    предел = _label_reach(разбор, columns, table_top) if columns else 0
    for i, (line, разобрано) in enumerate(разбор):
        if разобрано is not None:
            continue
        # Смотрим только на блоки С БУКВАМИ: обрывки сетки разбросаны по всей
        # ширине листа, и по ним строка «Налог на добавленную стоимость по
        # приобретенным | _ | | | _» выглядела вылезающей за колонку подписей,
        # хотя сам текст прижат к левому краю.
        значимые = [b for b in line
                    if any(ch.isalpha() for ch in _clean(str(b.get('text', ''))))]
        if not значимые:
            continue
        if any(looks_like_number(_clean(str(b.get('text', '')))) for b in line):
            continue                        # в строке есть число — это не перенос
        текст = _clean(_text(значимые))
        if not _is_label(текст) or _is_section_heading(текст):
            continue
        верх = min(b.get('top', 0) for b in значимые)
        if верх < table_top:
            continue
        if граница is not None and max(_block_bounds(b)[1] for b in значимые) > граница:
            continue

        высота = max(b.get('height', 0) for b in значимые) or 1

        # Куда приклеивать, решаем по ДЛИНЕ куска, а не по расстоянию: шаг
        # строк на бланке одинаковый, и до соседа сверху и снизу ровно
        # столько же пикселей (замер на балансе: 58 против 59 — по
        # расстоянию выигрывал не тот). Длинный кусок дотянут до правого
        # края графы, значит это ПЕРВАЯ строка переноса и числа стоят ниже
        # («Налог на добавленную стоимость по приобретенным / товарам,
        # работам, услугам»). Короткий — остаток второй строки, числа выше
        # («Долгосрочные финансовые / вложения»).
        правый = max(_block_bounds(b)[1] for b in значимые)
        шаг = 1 if предел and правый >= 0.85 * предел else -1
        j = _next_table_row(разбор, i, шаг)
        if j is None:
            continue
        сосед = min(b.get('top', 0) for b in разбор[j][0])
        if abs(сосед - верх) > 2.2 * высота:   # это не перенос, а отдельная строка
            continue

        подпись, ячейки, таблица = разбор[j][1]
        склеено = (подпись + ' ' + текст) if шаг < 0 else (текст + ' ' + подпись)
        разбор[j][1] = (_clean(склеено), ячейки, таблица)
        разбор[i][1] = None


def extract_fields(blocks, min_table_rows=3, grid=None):
    """
    Поля, извлечённые геометрически: [{'label', 'value', 'blocks'}].

    'blocks' — те самые OCR-блоки, из которых взято значение, поэтому
    подсветке не нужно потом искать их по тексту (а искать «19766663» по
    блокам «19», «766», «66з» она бы и не смогла).

    Если передана сетка (разметка бланка, найденная по самим линейкам) —
    таблица разбирается ПО НЕЙ, а догадки о колонках не используются вовсе.
    Строки выше таблицы (шапка документа: «Организация», «Адрес») разбираются
    прежним путём — там разметки нет, там обычные пары «название — значение».
    """
    if grid:
        разобрано = _fields_from_grid(blocks, grid)
        if разобрано is not None:
            поля, верх = разобрано
            выше = [b for b in blocks
                    if b.get('top', 0) + b.get('height', 0) / 2 < верх]
            # Запасному пути отдаём ГОТОВЫЕ границы граф и верх таблицы: иначе
            # он заново угадает колонки по огрызкам шапки и сделает поле из
            # её же заголовка («строки» = «2026 г.»).
            графы = [(a, b) for a, b in zip(grid['cols'][1:], grid['cols'][2:])]
            return _fields_by_guess(выше, min_table_rows,
                                    columns=графы, table_top=верх) + поля
    return _fields_by_guess(blocks, min_table_rows)


def _band(v, границы):
    """Номер полосы, в которую попала координата; None — если мимо."""
    for i in range(len(границы) - 1):
        if границы[i] <= v < границы[i + 1]:
            return i
    return None


def grid_cells(blocks, grid):
    """Блоки -> {(ряд, графа): [блоки]}. Каждый блок кладём по его середине."""
    ячейки = {}
    for b in blocks:
        cx = b.get('left', 0) + b.get('width', 0) / 2
        cy = b.get('top', 0) + b.get('height', 0) / 2
        r, c = _band(cy, grid['rows']), _band(cx, grid['cols'])
        if r is not None and c is not None:
            ячейки.setdefault((r, c), []).append(b)
    for cell in ячейки.values():
        cell.sort(key=lambda b: (b.get('top', 0), b.get('left', 0)))
    return ячейки


def _cell_text(cell):
    """
    Текст ячейки в порядке чтения: сверху вниз, слева направо.

    Внутри ячейки бывает две строки («Налог на добавленную стоимость по
    приобретенным / товарам, работам, услугам», число «14 245» на скане
    чуть съезжает по вертикали). Простая сортировка по верхней границе их
    перемешивала: получалось «245) 14» и подпись задом наперёд.
    """
    if not cell:
        return ''
    return ' '.join(' '.join(str(b.get('text', '')) for b in line)
                    for line in group_lines(cell)).strip()


def _cell_keep(cell, числовая):
    """
    Блоки ячейки без обрывков разметки.

    Линейку мы стираем перед распознаванием, и её огрызки Tesseract читает
    как 'ee', '—', 'a'. Лежат они у самого края ячейки — внутрь попадают, но
    содержимым не являются: значение выходило «8 007 ee», подпись «Уставный
    капитал —». В числовой ячейке оставляем блоки С ЦИФРАМИ, в текстовой —
    те, в которых есть буква или цифра. Ячейку-прочерк не трогаем: там
    значимого блока нет вовсе, и она целиком означает «значения нет».
    """
    if not cell:
        return []
    if числовая:
        с_цифрами = [b for b in cell
                     if any(ch.isdigit() for ch in str(b.get('text', '')))]
        if с_цифрами:
            return с_цифрами
    значимые = [b for b in cell
                if any(ch.isalnum() for ch in _clean(str(b.get('text', ''))))]
    return значимые or list(cell)


def _is_row_code(текст):
    n = normalize_number(текст)
    return bool(n) and n.isdigit() and len(n) == 3 and 100 <= int(n) <= 999


def _fields_from_grid(blocks, grid, мин_кодов=4):
    """
    Поля таблицы по разметке бланка: [(поля), верх таблицы] либо None.

    Ничего не угадываем: ряд — это ряд между линейками, графа — графа между
    линейками. Двухстрочная подпись («Налог на добавленную стоимость по
    приобретенным / товарам, работам, услугам») лежит в ОДНОЙ ячейке, так что
    склеивать переносы не требуется. Обрывок стёртой линейки лежит НА линейке,
    то есть вне ячейки, и в подпись не попадает.

    None означает «это не таблица с кодами строк» — тогда работает прежний
    путь: на чеке разметки нет, там гадать по выравниванию как раз уместно.
    """
    ячейки = grid_cells(blocks, grid)
    рядов, граф = len(grid['rows']) - 1, len(grid['cols']) - 1
    def блоки(r, c, числовая=False):
        return _cell_keep(ячейки.get((r, c), []), числовая)

    def текст(r, c, числовая=False):
        return _clean(_cell_text(блоки(r, c, числовая)))

    code_col, лучшее = None, 0
    for c in range(граф):
        коды = sum(1 for r in range(рядов) if _is_row_code(текст(r, c)))
        if коды > лучшее:
            code_col, лучшее = c, коды
    if лучшее < мин_кодов:
        return None

    данные = [r for r in range(рядов) if _is_row_code(текст(r, code_col))]
    первая, последняя = min(данные), max(данные)

    # Графа подписей — где больше всего буквенных ячеек.
    label_col, лучшее = 0, -1
    for c in range(граф):
        буквенных = sum(1 for r in range(первая, последняя + 1)
                        if any(ch.isalpha() for ch in текст(r, c)))
        if буквенных > лучшее:
            label_col, лучшее = c, буквенных

    # Годы граф — из шапки САМОЙ таблицы, каждая ячейка уже в своей графе.
    names = [None] * граф
    for c in range(граф):
        if c in (code_col, label_col):
            continue
        for r in range(первая - 1, -1, -1):        # снизу вверх, от таблицы
            t = текст(r, c)
            if _FORM_DATE_RE.search(t):
                continue
            год = _YEAR_RE.search(t)
            if год:
                names[c] = год.group(0)
                break

    fields, занятые = [], set()
    for r in range(первая, последняя + 1):
        подпись = текст(r, label_col)
        if not _is_label(подпись):
            continue
        for c in range(граф):
            if c in (code_col, label_col):
                continue
            значение = текст(r, c, числовая=True)
            if not _looks_like_value(значение):
                continue
            число = normalize_number(значение)
            if число == '—':
                continue                            # прочерк: значения нет
            имя = '{} ({})'.format(подпись, names[c]) if names[c] else подпись
            if имя in занятые:
                имя = '{} (графа {})'.format(подпись, c + 1)
            занятые.add(имя)
            fields.append({'label': имя,
                           'value': число if число is not None else значение,
                           'blocks': блоки(r, c, числовая=True)})
    return fields, grid['rows'][первая]


def _fields_by_guess(blocks, min_table_rows=3, columns=None, table_top=None):
    """Прежний путь: колонки угадываются по выравниванию чисел (чеки, бланки
    без разметки). Используется, когда сетка не найдена или это не таблица."""
    lines = group_lines(blocks)
    готовые = columns
    columns = готовые if готовые else detect_columns(lines)
    if len(columns) < 2:
        columns = []

    разбор = [[line, _read_line(line, columns)] for line in lines]
    строки = [р for _, р in разбор if р]
    табличные = [(п, я) for п, я, т in строки if т]

    # колонка кодов строк — определяется по всей странице, не по одной строке
    code_col = None
    if columns and len(табличные) >= min_table_rows:
        for i in range(len(columns)):
            # считаем по ВСЕМ строкам, а не только по табличным: в строках,
            # где все значения — прочерки, код остаётся единственной ячейкой,
            # и без них колонка кодов не набирает большинства
            в_колонке = [c for _, я, _т in строки for j, c in я.items() if j == i]
            if _is_row_code_column(в_колонке):
                code_col = i
                break

    # годы раздаём ПОСЛЕ того, как узнали колонку кодов, и мимо неё: подпись
    # «На 30 июня 2026 г.» в шапке смещена влево и иначе липнет к кодам
    names, готовый_верх = [], table_top
    table_top = готовый_верх
    if columns and табличные and table_top is None:
        table_top = min(b.get('top', 0) for _, я in табличные
                        for c in я.values() for b in c)
    if columns and table_top is not None:
        names = _column_years(lines, columns, table_top, skip=code_col)
        if готовый_верх is None:
            _join_wrapped_labels(разбор, columns, table_top)

    fields = []
    for line, разобрано in разбор:
        if разобрано is None:
            continue
        подпись, ячейки, таблица = разобрано
        # Шапка самой таблицы («Код строки | На 30 июня 2026 г.») стоит выше
        # первой строки данных и по колонкам режется как обычная строка.
        # Её ячейки — не значения, а названия граф, полем им становиться
        # незачем. Пары шапки ДОКУМЕНТА это не задевает: их значение идёт
        # сплошным текстом и лежит в ячейке без номера колонки.
        шапка_таблицы = (not таблица and table_top is not None
                         and min(b.get('top', 0) for b in line) < table_top)
        занятые = set()
        for i in sorted(ячейки, key=lambda k: (k is None, k)):
            if i is not None and (i == code_col or шапка_таблицы):
                continue
            текст = _clean(_text(ячейки[i]))
            if not _looks_like_value(текст):
                continue
            число = normalize_number(текст)
            if число == '—':
                continue                        # прочерк: значения нет
            имя = подпись
            if таблица and i is not None and names and i < len(names) and names[i]:
                имя = '{} ({})'.format(подпись, names[i])
            elif имя in занятые:
                имя = '{} (колонка {})'.format(подпись, (i or 0) + 1)
            занятые.add(имя)
            fields.append({
                'label': имя,
                'value': число if число is not None else текст,
                'blocks': list(ячейки[i]),
            })
    return fields


def _block_key(block):
    return (block.get('left'), block.get('top'),
            block.get('width'), block.get('height'), block.get('text'))


def _norm_label(label):
    return re.sub(r'[^\w]+', '', str(label).lower(), flags=re.UNICODE)


def merge_fields(parsed_item, blocks, mapping=None, grid=None):
    """
    Дополняет результат модели полями, извлечёнными правилами.

    Модель — источник приоритетный: её поля не перезаписываются, правила
    только добавляют то, чего она не вернула.

    Если передан mapping, блоки значений кладутся в него напрямую: искать их
    по тексту бесполезно — значение уже нормализовано ('19766663'), а в
    документе оно разбито на блоки '19', '766', '66з'.

    Функция идемпотентна: её можно вызвать дважды на одном parsed_item
    (оркестратор — чтобы поля попали в БД, web_server — чтобы попали в
    подсветку), второй вызов ничего не продублирует.

    Возвращает число ДОБАВЛЕННЫХ полей.
    """
    try:
        found = extract_fields(blocks, grid=grid)
    except Exception as exc:            # правила не должны ронять обработку документа
        print('[rule_fields] пропущено: %s' % exc)
        return 0

    extra = parsed_item.setdefault('extra_fields', {})
    model_labels = {_norm_label(k) for k in ('total', 'date', 'company',
                                             'invoice_number', 'vat', 'currency')}
    claimed = set()
    if mapping:
        claimed = {_block_key(b) for cells in mapping.values() for b in cells}

    added = 0
    for field in found:
        label, value = field['label'], field['value']
        norm = _norm_label(label)
        if not norm or norm in model_labels:
            continue
        keys = {_block_key(b) for b in field['blocks']}

        if label in extra:
            # Уже добавлено прошлым вызовом. Значение не трогаем, а подсветку
            # ПЕРЕЗАПИСЫВАЕМ всегда: до нас mapping заполнил поиск по тексту, а
            # он геометрии не знает. Значение «60» есть в бланке дважды (строки
            # 160 и 260), и рамка рисовалась на обеих сразу — рядом с числом
            # появлялись цветные обрывки, ни к чему не относящиеся. Блоки
            # правил взяты из самой ячейки и всегда точнее.
            if extra[label] != value:
                continue
        elif _norm_label_in(extra, norm) or keys <= claimed:
            continue                     # поле есть у модели / блоки уже подсвечены
        else:
            extra[label] = value
            added += 1

        if mapping is not None:
            mapping['extra::%s' % label] = field['blocks']
            claimed |= keys
    return added


def _norm_label_in(extra, norm):
    return any(_norm_label(k) == norm for k in extra)
