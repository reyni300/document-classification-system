# -*- coding: utf-8 -*-
"""
Диагностика разбора страницы. Кладётся в корень проекта, рядом с web_server.py.

Запуск внутри работающего контейнера:
    docker exec -it <ID> python3 dump_blocks.py страница.pdf

Печатает: сколько слов распознал OCR, какие нашлись колонки, как каждая строка
режется на ячейки и какие поля получились. Работает с любой версией
rule_fields — ни на какие внутренние функции не опирается.
"""
import json
import sys

from ocr.ocr_module import OCRModule
import rule_fields as rf


def main(path):
    страницы = OCRModule().extract(path)['pages']
    print(f'страниц: {len(страницы)}')
    выгрузка = []

    for н, стр in enumerate(страницы, 1):
        blocks = стр.get('blocks', [])
        выгрузка.append({'page': н, 'img_width': стр.get('img_width'),
                         'img_height': стр.get('img_height'),
                         'grid': стр.get('grid'), 'blocks': blocks})

        print(f'\n=== страница {н}')
        print(f'    БЛОКОВ: {len(blocks)}   размер {стр.get("img_width")}x{стр.get("img_height")}')
        уверенных = sum(1 for b in blocks if b.get('confidence', 0) >= 60)
        print(f'    из них уверенно прочитано: {уверенных}')

        grid = стр.get('grid')
        if grid:
            print(f'    СЕТКА БЛАНКА: рядов {len(grid["rows"]) - 1}, '
                  f'граф {len(grid["cols"]) - 1}, границы граф {grid["cols"]}')
        else:
            print('    СЕТКА БЛАНКА: не найдена — работает разбор по выравниванию')

        lines = rf.group_lines(blocks)
        columns = rf.detect_columns(lines)
        print(f'    строк: {len(lines)}')
        print(f'    КОЛОНКИ: {[(int(a), int(b)) for a, b in columns]}')

        print('\n    как режутся строки (ячейки разделены знаком |):')
        for line in lines:
            ячейки = [rf._text(c) for c in rf.split_cells(line)]
            print('      ' + '  |  '.join(ячейки))

        if grid:
            проверки = rf.check_totals(blocks, grid)
            if проверки:
                плохих = [п for п in проверки if п['расхождение']]
                print(f'\n    ПРОВЕРКА СХОДИМОСТИ: {len(проверки) - len(плохих)} из '
                      f'{len(проверки)} сумм сходится')
                for п in проверки:
                    знак = ('сходится' if not п['расхождение']
                            else f"РАСХОЖДЕНИЕ {п['расхождение']:+d}")
                    print(f"      {п['что']:30} графа {п['графа']}: "
                          f"{п['посчитано']:>8} против {п['в_бланке']:>8}   {знак}")
                for в in rf.explain_mismatches(проверки, rf.table_rows(blocks, grid)):
                    if в['строка']:
                        print(f"      -> строка {в['строка']}, графа {в['графа']}: "
                              f"прочитано {в['прочитано']}, вероятно {в['вероятно']} "
                              f"({в['почему']})")
                    else:
                        print(f"      -> {в['почему']}")

        item = {'extra_fields': {}}
        rf.merge_fields(item, blocks, grid=grid)
        поля = item['extra_fields']
        print(f'\n    ПОЛЯ ({len(поля)}):')
        for k, v in поля.items():
            print(f'      {k!r} = {v!r}')

    with open('blocks_dump.json', 'w', encoding='utf-8') as f:
        json.dump(выгрузка, f, ensure_ascii=False, indent=1)
    print('\nблоки сохранены в blocks_dump.json')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('укажи файл: python3 dump_blocks.py страница.pdf')
        sys.exit(1)
    main(sys.argv[1])
