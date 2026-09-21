# -*- coding: utf-8 -*-
"""
Диагностика разбора страницы. Кладётся в корень проекта, рядом с web_server.py.

Запуск внутри работающего контейнера:
    docker exec -it <ID> python3 dump_blocks.py страница.pdf

Печатает, как именно разбирается страница: какие нашлись колонки, какая из них
признана колонкой кодов строк, как названы остальные, как каждая строка
разложена по колонкам и какие поля в итоге вышли. Рядом кладёт blocks_dump.json.
"""
import json
import sys

from ocr.ocr_module import OCRModule
import rule_fields as rf


def разобрать(blocks):
    lines = rf.group_lines(blocks)
    columns = rf.detect_columns(lines)
    if len(columns) < 2:
        columns = []

    строки = [р for р in (rf._read_line(l, columns) for l in lines) if р]
    табличные = [(п, я) for п, я, т in строки if т]

    code_col = None
    if columns and len(табличные) >= 3:
        for i in range(len(columns)):
            в_колонке = [c for _, я, _т in строки for j, c in я.items() if j == i]
            if rf._is_row_code_column(в_колонке):
                code_col = i
                break

    names, израсходованы = [], set()
    if columns and табличные:
        table_top = min(b.get('top', 0) for _, я in табличные
                        for c in я.values() for b in c)
        names, израсходованы = rf._column_years(lines, columns, table_top, skip=code_col)
    else:
        table_top = None
    return lines, columns, строки, табличные, code_col, names, table_top


def main(path):
    страницы = OCRModule().extract(path)['pages']
    print(f'страниц: {len(страницы)}')
    выгрузка = []

    for н, стр in enumerate(страницы, 1):
        blocks = стр.get('blocks', [])
        выгрузка.append({'page': н, 'img_width': стр.get('img_width'),
                         'img_height': стр.get('img_height'), 'blocks': blocks})

        lines, columns, строки, табличные, code_col, names, table_top = разобрать(blocks)

        print(f'\n=== страница {н}: блоков {len(blocks)}, '
              f'{стр.get("img_width")}x{стр.get("img_height")}')
        print(f'    строк всего {len(lines)}, из них разобрано {len(строки)}, '
              f'табличных {len(табличные)}, верх таблицы {table_top}')
        print(f'    КОЛОНКИ: {[(int(a), int(b)) for a, b in columns]}')
        print(f'    колонка кодов строк: '
              f'{"не найдена" if code_col is None else code_col}')
        print(f'    имена колонок: {names}')

        if columns:
            print('\n    почему колонка признана (или не признана) колонкой кодов:')
            for i in range(len(columns)):
                в_колонке = [rf._clean(rf._text(c)) for _, я, _т in строки
                             for j, c in я.items() if j == i]
                print(f'      колонка {i}: {len(в_колонке)} ячеек, '
                      f'код? {rf._is_row_code_column([[{"text": t, "left": 0, "top": 0, "width": 1, "height": 1}] for t in в_колонке])}')
                print(f'         {в_колонке[:14]}')

        print('\n    как разложены строки (подпись >> колонка: ячейка):')
        for line in lines:
            подпись, ячейки = rf.split_by_columns(line, columns)
            по_колонкам = {k: rf._clean(rf._text(v)) for k, v in ячейки.items()}
            разбор = rf._read_line(line, columns)
            метка = 'пропущена' if разбор is None else ('таблица' if разбор[2] else 'пара')
            print(f'      [{метка:^9}] {подпись!r} >> {по_колонкам}')

        item = {'extra_fields': {}}
        rf.merge_fields(item, blocks)
        print(f'\n    ПОЛЯ ({len(item["extra_fields"])}):')
        for k, v in item['extra_fields'].items():
            print(f'      {k!r} = {v!r}')

    with open('blocks_dump.json', 'w', encoding='utf-8') as f:
        json.dump(выгрузка, f, ensure_ascii=False, indent=1)
    print('\nблоки сохранены в blocks_dump.json — пришли этот файл')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('укажи файл: python3 dump_blocks.py страница.pdf')
        sys.exit(1)
    main(sys.argv[1])
