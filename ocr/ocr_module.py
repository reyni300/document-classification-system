import cv2
import numpy as np
import pytesseract
from pdf2image import convert_from_path
from datetime import datetime
import yaml
import os
import tempfile

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

# Папка для хранения конвертированных страниц PDF
PDF_PAGES_DIR = os.path.join(tempfile.gettempdir(), "dokanaliz_pages")
os.makedirs(PDF_PAGES_DIR, exist_ok=True)


class OCRModule:
    def preprocess(self, img):
        gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        gray = cv2.convertScaleAbs(gray, alpha=config['ocr']['preprocessing']['alpha'], beta=0)
        thresh = cv2.adaptiveThreshold(
            gray, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            config['ocr']['preprocessing']['adaptive_block_size'],
            config['ocr']['preprocessing']['adaptive_constant']
        )
        return thresh

    def remove_table_lines(self, binary):
        """
        Стирает линии разметки таблицы перед распознаванием.

        Зачем: на бланках с полностью разлинованной таблицей (баланс, форма
        отчётности) Tesseract принимает рамки ячеек за текст. Разбор страницы
        рассыпается: вместо слов получаются огрызки вида 'a м pн vo Lo] и =>',
        и бОльшая часть содержимого теряется совсем. Замер на такой странице:
        11 распознанных слов до стирания против 66 после.

        Линии ищем морфологическим открытием длинным горизонтальным и
        вертикальным ядром — так отбираются только протяжённые штрихи,
        буквы и цифры такое ядро не переживают и остаются на месте.
        На документе без разметки стирать нечего, и результат не меняется.
        """
        inv = 255 - binary
        h, w = binary.shape[:2]
        hor_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(25, w // 25), 1))
        ver_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(25, h // 25)))
        lines = cv2.add(cv2.morphologyEx(inv, cv2.MORPH_OPEN, hor_kernel),
                        cv2.morphologyEx(inv, cv2.MORPH_OPEN, ver_kernel))
        lines = cv2.dilate(lines, np.ones((2, 2), np.uint8))  # захватываем края штриха
        return 255 - cv2.subtract(inv, lines)

    ЛЮФТ_ЛИНЕЙКИ = 12      # линейка уезжает вбок: лист сканирован с перекосом

    @staticmethod
    def _полосы(счёт, порог, слипание=6):
        есть = np.where(счёт > порог)[0]
        группы = []
        for p in есть:
            if группы and p - группы[-1][-1] <= слипание:
                группы[-1].append(p)
            else:
                группы.append([p])
        return [int((g[0] + g[-1]) / 2) for g in группы]

    def table_grid(self, binary, мин_рядов=5, мин_граф=3):
        """
        Границы ячеек таблицы: {'rows': [y...], 'cols': [x...]} либо None.

        Зачем: колонки бланка НАРИСОВАНЫ в документе, и до сих пор мы их
        стирали, а потом угадывали заново по тому, как выровнялись числа.
        Гадание разваливалось на каждом новом бланке: на второй странице
        баланса оно нашло четыре колонки вместо трёх, одну выдуманную.

        Ядра морфологии короткие: лист лежит с перекосом, сплошного штриха во
        всю ширину или высоту на скане не бывает — длинное ядро не находит
        вообще ничего. Отбираем по суммарной длине штрихов, не по
        непрерывности.

        Графу таблицы от рамки шапки документа отличаем по тому, в скольких
        строках листа линейка вообще присутствует: настоящая графа идёт через
        70-90% строк, разделитель шапки — через 15-20%.
        """
        inv = 255 - binary
        h, w = binary.shape[:2]

        гор = cv2.morphologyEx(inv, cv2.MORPH_OPEN,
                               cv2.getStructuringElement(cv2.MORPH_RECT, (w // 12, 1)))
        ряды = self._полосы((гор > 0).sum(axis=1), 0.15 * w)
        if len(ряды) < мин_рядов + 1:
            return None

        вер = cv2.morphologyEx(inv, cv2.MORPH_OPEN,
                               cv2.getStructuringElement(cv2.MORPH_RECT, (1, 40)))
        люфт = self.ЛЮФТ_ЛИНЕЙКИ
        графы = []
        for x in self._полосы((вер > 0).sum(axis=0), 0.06 * h):
            строк = 0
            for верх, низ in zip(ряды, ряды[1:]):
                высота = низ - верх
                if высота < 10:
                    continue
                полоса = вер[верх:низ, max(0, x - люфт):x + люфт + 1]
                if (полоса > 0).any(axis=1).sum() > 0.5 * высота:
                    строк += 1
            if строк > 0.5 * (len(ряды) - 1):
                графы.append(x)

        if len(графы) < мин_граф + 1:
            return None
        return {'rows': ряды, 'cols': графы}

    # --- перечитывание числовых ячеек по одной ---------------------------
    ОТСТУП_ОТ_ЛИНЕЙКИ = 5      # чтобы в вырез не попал штрих рамки
    УВЕЛИЧЕНИЕ = 2             # Tesseract читает лучше, когда цифра крупная
    ПОЛЕ_ВОКРУГ = 25           # белое поле по краям выреза
    ТОЛЬКО_ЦИФРЫ = '--psm 7 -c tessedit_char_whitelist=0123456789'

    @staticmethod
    def _похоже_на_число(текст):
        текст = текст.strip().replace(' ', '').replace('\u00a0', '')
        return (bool(текст) and any(c.isdigit() for c in текст)
                and all(c in '0123456789.,' for c in текст))

    @staticmethod
    def _полоса(v, границы):
        for i in range(len(границы) - 1):
            if границы[i] <= v < границы[i + 1]:
                return i
        return None

    def _по_ячейкам(self, blocks, grid):
        ячейки = {}
        for b in blocks:
            r = self._полоса(b['top'] + b['height'] / 2, grid['rows'])
            c = self._полоса(b['left'] + b['width'] / 2, grid['cols'])
            if r is not None and c is not None:
                ячейки.setdefault((r, c), []).append(b)
        return ячейки

    def _прочитать_ячейку(self, binary, grid, r, c):
        """
        Одна ячейка -> блоки В КООРДИНАТАХ СТРАНИЦЫ.

        Зачем отдельно: на целой странице Tesseract решает сразу три задачи —
        разобрать вёрстку, справиться с остатками стёртых линеек и прочитать
        символы. Отсюда '8007ee', '4910—', '|—217'. В вырезе задачи на вёрстку
        нет, соседей нет, обрывки рамки остались снаружи. Замер на второй
        странице баланса: 9 верных чисел из 19 целой страницей против 16 из 16
        по ячейкам.

        Координаты пересчитываются обратно, иначе подсветка встанет не туда:
        снимаем белое поле, делим на увеличение, прибавляем угол ячейки.
        """
        отступ, масштаб, поле = (self.ОТСТУП_ОТ_ЛИНЕЙКИ, self.УВЕЛИЧЕНИЕ,
                                 self.ПОЛЕ_ВОКРУГ)
        x0, y0 = grid['cols'][c] + отступ, grid['rows'][r] + отступ
        x1, y1 = grid['cols'][c + 1] - отступ, grid['rows'][r + 1] - отступ
        if x1 - x0 < 10 or y1 - y0 < 10:
            return []
        кроп = binary[y0:y1, x0:x1]
        if кроп.size == 0 or (255 - кроп).sum() < 255 * 40:
            return []                      # пусто или один прочерк — читать нечего
        крупно = cv2.resize(кроп, None, fx=масштаб, fy=масштаб,
                            interpolation=cv2.INTER_CUBIC)
        крупно = cv2.copyMakeBorder(крупно, поле, поле, поле, поле,
                                    cv2.BORDER_CONSTANT, value=255)
        try:
            d = pytesseract.image_to_data(крупно, lang=config['ocr']['lang'],
                                          config=self.ТОЛЬКО_ЦИФРЫ,
                                          output_type=pytesseract.Output.DICT)
        except Exception as exc:
            print(f'[OCR] ячейка ({r},{c}) не прочиталась: {exc}')
            return []
        out = []
        for i, t in enumerate(d['text']):
            t = t.strip()
            conf = int(d['conf'][i]) if d['conf'][i] != -1 else 0
            if not t or conf <= 0:
                continue
            out.append({'text': t,
                        'left': x0 + (d['left'][i] - поле) // масштаб,
                        'top': y0 + (d['top'][i] - поле) // масштаб,
                        'width': max(1, d['width'][i] // масштаб),
                        'height': max(1, d['height'][i] // масштаб),
                        'confidence': conf})
        return out

    def refine_number_cells(self, binary, blocks, grid, мин_чисел=4):
        """
        Перечитывает ЧИСЛОВЫЕ графы по ячейкам и заменяет в них блоки.

        Подписи не трогаем: ошибка в названии строки косметическая, поле всё
        равно узнаётся, а вот цифра обязана быть точной. Числовой считается
        графа, где общий проход уже нашёл хотя бы несколько чисел, — так мы
        не тратим время на графу подписей.

        Если по ячейке ничего не вышло, блоки общего прохода остаются: хуже
        точно не станет.
        """
        if not grid:
            return blocks
        ячейки = self._по_ячейкам(blocks, grid)
        рядов, граф = len(grid['rows']) - 1, len(grid['cols']) - 1

        числовые = []
        for c in range(граф):
            сколько = sum(1 for r in range(рядов)
                          if self._похоже_на_число(
                              ' '.join(b['text'] for b in ячейки.get((r, c), []))))
            if сколько >= мин_чисел:
                числовые.append(c)
        if not числовые:
            return blocks

        заменено = прочитано = 0
        убрать, добавить = [], []
        for c in числовые:
            for r in range(рядов):
                старые = ячейки.get((r, c), [])
                старый_текст = ' '.join(b['text'] for b in старые)
                # Чистое число не трогаем. Перечитывание помогает там, где к
                # числу прилип мусор ('82!', '| — 217', '5 647) _—'), но на
                # уже чистой ячейке может само добавить лишнее: в графе кодов
                # к '700' приклеивалась единица от рамки, и строка БАЛАНС
                # пропадала целиком. Так испортить нечего.
                if self._похоже_на_число(старый_текст):
                    continue
                новые = self._прочитать_ячейку(binary, grid, r, c)
                прочитано += 1
                if not новые:
                    continue
                новый_текст = ' '.join(b['text'] for b in новые)
                if not self._похоже_на_число(новый_текст):
                    continue                # по ячейке тоже не число — оставляем как было
                убрать.extend(id(b) for b in старые)
                добавить.extend(новые)
                заменено += 1
        print(f'[OCR] числовых граф {len(числовые)}, ячеек перечитано '
              f'{прочитано}, блоков заменено в {заменено}')
        убрать = set(убрать)
        return [b for b in blocks if id(b) not in убрать] + добавить

    # Способы распознавания, из которых выбирается лучший. Ни один не годится
    # на все документы: на чистом бланке выигрывает разбор вёрстки самим
    # Tesseract (psm 3), на плотной таблице в рамках он же теряет почти всё,
    # и там нужны стирание сетки и режим разрозненного текста. Проверено на
    # двух сканах: замена одного фиксированного режима на выбор из трёх даёт
    # 31 значение из 33 на первом и 22 из 28 на втором.
    CANDIDATES = (
        {'remove_lines': False, 'psm': 3},
        {'remove_lines': False, 'psm': 11},
        {'remove_lines': True, 'psm': 11},
    )

    def _blocks_from(self, image, psm):
        data = pytesseract.image_to_data(
            image, lang=config['ocr']['lang'], config=f'--psm {psm}',
            output_type=pytesseract.Output.DICT)
        blocks = []
        for i in range(len(data['text'])):
            txt = data['text'][i].strip()
            if not txt:
                continue
            conf = int(data['conf'][i]) if data['conf'][i] != -1 else 0
            blocks.append({
                "text": txt,
                "left": int(data['left'][i]),
                "top": int(data['top'][i]),
                "width": int(data['width'][i]),
                "height": int(data['height'][i]),
                "confidence": conf
            })
        return blocks

    @staticmethod
    def _overlap(a, b):
        dx = min(a['left'] + a['width'], b['left'] + b['width']) - max(a['left'], b['left'])
        dy = min(a['top'] + a['height'], b['top'] + b['height']) - max(a['top'], b['top'])
        return dx > 0 and dy > 0

    @staticmethod
    def _quality(blocks, min_conf=60):
        """
        Во сколько слов страницы можно верить.

        Именно это число, а не общее количество слов и не средняя уверенность,
        совпадает с реальным качеством разбора: вариант с кучей мусорных
        обрывков даёт много слов при низкой уверенности каждого.
        """
        return sum(1 for b in blocks if b['confidence'] >= min_conf)

    def _recognize(self, img_np):
        """Одна картинка -> (текст, блоки, сетка). Общий путь для PDF и картинок."""
        ocr_cfg = config['ocr']
        binary = self.preprocess(img_np)
        grid = self.table_grid(binary) if ocr_cfg.get('table_grid', True) else None
        if grid:
            print(f"[OCR] сетка таблицы: {len(grid['rows']) - 1} рядов, "
                  f"{len(grid['cols']) - 1} граф")

        if not ocr_cfg.get('auto_mode', True):
            image = (self.remove_table_lines(binary)
                     if ocr_cfg.get('remove_table_lines', False) else binary)
            blocks = self._blocks_from(image, ocr_cfg.get('psm', 3))
        else:
            без_линий = None
            все, лучшие, лучший_счёт, лучший_вид = [], [], -1, ''
            for вариант in self.CANDIDATES:
                if вариант['remove_lines']:
                    если_нужно = без_линий = (без_линий if без_линий is not None
                                              else self.remove_table_lines(binary))
                else:
                    если_нужно = binary
                blocks = self._blocks_from(если_нужно, вариант['psm'])
                все.append(blocks)
                счёт = self._quality(blocks)
                if счёт > лучший_счёт:
                    лучшие, лучший_счёт = blocks, счёт
                    лучший_вид = ('без сетки, ' if вариант['remove_lines'] else '') \
                                 + f"psm {вариант['psm']}"

            # Лучший вариант всё равно кое-где промахивается — одиночная цифра
            # у самой рамки достаётся то одному режиму, то другому. Поэтому
            # добираем из остальных те слова, которые прочитаны уверенно и
            # стоят там, где у победителя пусто.
            blocks = list(лучшие)
            добрано = 0
            for прочие in все:
                if прочие is лучшие:
                    continue
                for b in прочие:
                    if b['confidence'] >= 80 and not any(self._overlap(b, x) for x in blocks):
                        blocks.append(b)
                        добрано += 1
            print(f'[OCR] режим: {лучший_вид}, уверенно прочитано слов '
                  f'{лучший_счёт}, добрано из других режимов {добрано}')

        # Текст собираем из тех же блоков, что отдаём наверх, а не отдельным
        # вызовом image_to_string: раньше это были два независимых прохода
        # Tesseract, и LLM могла читать не то, что потом подсвечивалось.
        if grid and ocr_cfg.get('refine_number_cells', True):
            blocks = self.refine_number_cells(binary, blocks, grid)

        return self._text_from_blocks(blocks), blocks, grid

    @staticmethod
    def _text_from_blocks(blocks):
        """Блоки -> плоский текст со строками, в порядке чтения."""
        if not blocks:
            return ""
        lines, ordered = [], sorted(blocks, key=lambda b: (b['top'], b['left']))
        for b in ordered:
            for line in lines:
                top = min(q['top'] for q in line)
                bottom = max(q['top'] + q['height'] for q in line)
                overlap = min(b['top'] + b['height'], bottom) - max(b['top'], top)
                shortest = min(b['height'], bottom - top) or 1
                if overlap > 0.4 * shortest:
                    line.append(b)
                    break
            else:
                lines.append([b])
        lines.sort(key=lambda line: min(b['top'] for b in line))
        return "\n".join(' '.join(b['text'] for b in sorted(line, key=lambda q: q['left']))
                         for line in lines)

    def extract(self, file_path):
        all_pages = []

        if file_path.lower().endswith(".pdf"):
            # Конвертируем PDF в изображения с высоким DPI для качественного OCR
            images = convert_from_path(file_path, dpi=config['ocr'].get('pdf_dpi', 300))
            base_name = os.path.splitext(os.path.basename(file_path))[0]

            for page_num, img_pil in enumerate(images):
                # Сохраняем PNG-версию страницы для предпросмотра в браузере
                png_path = os.path.join(PDF_PAGES_DIR, f"{base_name}_page{page_num + 1}.png")
                img_pil.save(png_path, "PNG")

                text, blocks, grid = self._recognize(np.array(img_pil))
                all_pages.append({
                    "page": page_num + 1,
                    "text": text,
                    "blocks": blocks,
                    "grid": grid,
                    "png_path": png_path,          # путь к PNG для отдачи браузеру
                    "img_width": img_pil.width,    # реальные размеры PNG
                    "img_height": img_pil.height
                })
        else:
            img = cv2.imread(file_path)
            if img is None:
                raise ValueError(f"Could not read image file: {file_path}")
            # cv2.imread отдаёт BGR, а preprocess ждёт RGB — переворачиваем каналы
            text, blocks, grid = self._recognize(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            all_pages.append({
                "page": 1,
                "text": text,
                "blocks": blocks,
                "grid": grid,
                "png_path": file_path,
                "img_width": img.shape[1],
                "img_height": img.shape[0]
            })

        return {
            "pages": all_pages,
            "timestamp": datetime.now().isoformat(),
            "ocr_engine": f"Tesseract {pytesseract.get_tesseract_version()}"
        }
