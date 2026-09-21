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
        """Одна картинка -> (текст, блоки). Общий путь для PDF и изображений."""
        ocr_cfg = config['ocr']
        binary = self.preprocess(img_np)

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
        return self._text_from_blocks(blocks), blocks

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

                text, blocks = self._recognize(np.array(img_pil))
                all_pages.append({
                    "page": page_num + 1,
                    "text": text,
                    "blocks": blocks,
                    "png_path": png_path,          # путь к PNG для отдачи браузеру
                    "img_width": img_pil.width,    # реальные размеры PNG
                    "img_height": img_pil.height
                })
        else:
            img = cv2.imread(file_path)
            if img is None:
                raise ValueError(f"Could not read image file: {file_path}")
            # cv2.imread отдаёт BGR, а preprocess ждёт RGB — переворачиваем каналы
            text, blocks = self._recognize(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            all_pages.append({
                "page": 1,
                "text": text,
                "blocks": blocks,
                "png_path": file_path,
                "img_width": img.shape[1],
                "img_height": img.shape[0]
            })

        return {
            "pages": all_pages,
            "timestamp": datetime.now().isoformat(),
            "ocr_engine": f"Tesseract {pytesseract.get_tesseract_version()}"
        }
