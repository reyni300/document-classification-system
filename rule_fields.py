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

# Подмены, которые OCR делает В ЧИСЛАХ (в тексте их применять нельзя).
_DIGIT_CONFUSIONS = str.maketrans({
    'l': '1', 'I': '1', '|': '1', '!': '1', 'і': '1',
    'з': '3', 'З': '3', 'б': '6',
    'О': '0', 'о': '0', 'O': '0', 'o': '0',
})
_DASHES = {'-', '–', '—', '‒'}


def _text(cell):
    return ' '.join(b.get('text', '') for b in cell).strip()


def group_lines(blocks):
    """Блоки -> строки документа (по вертикальному перекрытию)."""
    items = [b for b in blocks if str(b.get('text', '')).strip()]
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
            if overlap > 0.4 * shortest and центры < 0.5 * shortest:
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


_THOUSAND_SEP_RE = re.compile(r'(?<=\d)[\s  ](?=\d{3}\b)')
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
    s = s.translate(_DIGIT_CONFUSIONS)
    s = _THOUSAND_SEP_RE.sub('', s)
    s = re.sub(r'(?<=\d)[.,](?=\d{3}\b)', '', s)
    s = s.replace(' ', '').replace(' ', '').replace(' ', '')
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


_JUNK_EDGE = '|_~`\u00b7\u2022!' + ' \t'    # '*' не трогаем: им маскируют номер карты
_JUNK_LEAD = _JUNK_EDGE + '\'"\u201c\u201d\u2018\u2019'


def _clean(text):
    """
    Срезает по краям обрывки стёртых линий разметки.

    После удаления сетки от рамок остаются короткие хвосты, и Tesseract читает
    их как одиночную пунктуацию: '| Организация', "' 191 766 663".
    Закрывающие скобки и кавычки не трогаем — они значимые: «(код ОКЭД 64910)»,
    «ООО "СМАРТ Партнер"». Внутри строки не трогаем ничего.
    """
    return text.strip(_JUNK_EDGE).lstrip(_JUNK_LEAD).strip()


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

    Раньше ячейки определялись только по величине пробела внутри строки, и это
    разваливалось на двух вещах сразу: у жирных заголовков («ИТОГО по разделу I»)
    пробелы широкие, и подпись рвалась на куски; а числа выровнены по правому
    краю, и их центры разъезжались настолько, что одна колонка распадалась
    на несколько.

    Поэтому: по пробелам ищем только КАНДИДАТОВ, колонкой считаем то, что
    повторяется из строки в строку и перекрывается по горизонтали, и берём
    только числовые ячейки. Числовые — потому что в бланках данные всегда
    числа, а куски подписей («form», «of measure») числами не являются и
    фантомных колонок больше не создают.

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
    return _clean(_text(подпись)), ячейки


def _column_years(lines, columns, table_top, skip=None):
    """
    Имя колонки из шапки: «На 30 июня 2026 г.» -> «2026».

    Шапку разбираем по пробелам, а не по границам колонок: подпись колонки
    шире самой колонки и сдвинута относительно чисел (числа прижаты вправо,
    подпись идёт по центру), так что отдельное слово «2026» запросто окажется
    за границей колонки и год потеряется.
    """
    names = [None] * len(columns)
    for line in lines:
        if min(b.get('top', 0) for b in line) >= table_top:
            continue
        for cell in split_cells(line):
            год = re.search(r'\b(19|20)\d{2}\b', _text(cell))
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
    if len(cells) == 2:
        подпись = _clean(_text(cells[0]))
        if _is_label(подпись):
            # Числовую ячейку привязываем к колонке: иначе в строке, где все
            # значения — прочерки, единственной уцелевшей ячейкой остаётся код
            # строки, и он уходит в поля как значение показателя.
            # Ячейку привязываем к колонке, если она в неё помещается: иначе
            # в строке, где все значения — прочерки, единственной уцелевшей
            # ячейкой остаётся код строки, и он уходит в поля как значение.
            номер = _best_column(cells[1], columns, contained=True)
            return подпись, {номер: cells[1]}, False
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


def extract_fields(blocks, min_table_rows=3):
    """
    Поля, извлечённые геометрически: [{'label', 'value', 'blocks'}].

    'blocks' — те самые OCR-блоки, из которых взято значение, поэтому
    подсветке не нужно потом искать их по тексту (а искать «19766663» по
    блокам «19», «766», «66з» она бы и не смогла).
    """
    lines = group_lines(blocks)
    columns = detect_columns(lines)
    if len(columns) < 2:
        columns = []

    строки = [р for р in (_read_line(line, columns) for line in lines) if р]
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
    names = []
    if columns and табличные:
        table_top = min(b.get('top', 0) for _, я in табличные
                        for c in я.values() for b in c)
        names = _column_years(lines, columns, table_top, skip=code_col)

    fields = []
    for подпись, ячейки, таблица in строки:
        занятые = set()
        for i in sorted(ячейки, key=lambda k: (k is None, k)):
            if i is not None and i == code_col:
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


def merge_fields(parsed_item, blocks, mapping=None):
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
        found = extract_fields(blocks)
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
            # уже добавлено прошлым вызовом — значение не трогаем,
            # но блоки в подсветку отдать всё равно надо
            if extra[label] != value or keys <= claimed:
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
