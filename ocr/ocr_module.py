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
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.convertScaleAbs(gray, alpha=config['ocr']['preprocessing']['alpha'], beta=0)
        thresh = cv2.adaptiveThreshold(
            gray, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            config['ocr']['preprocessing']['adaptive_block_size'],
            config['ocr']['preprocessing']['adaptive_constant']
        )
        return thresh

    def extract(self, file_path):
        all_pages = []
        lang = config['ocr']['lang']

        if file_path.lower().endswith(".pdf"):
            # Конвертируем PDF в изображения с высоким DPI для качественного OCR
            images = convert_from_path(file_path, dpi=300)
            base_name = os.path.splitext(os.path.basename(file_path))[0]

            for page_num, img_pil in enumerate(images):
                # Сохраняем PNG-версию страницы для предпросмотра в браузере
                png_path = os.path.join(PDF_PAGES_DIR, f"{base_name}_page{page_num + 1}.png")
                img_pil.save(png_path, "PNG")

                img_np = np.array(img_pil)
                processed = self.preprocess(img_np)
                data = pytesseract.image_to_data(processed, lang=lang, output_type=pytesseract.Output.DICT)
                text = pytesseract.image_to_string(processed, lang=lang)
                blocks = []
                for i in range(len(data['text'])):
                    txt = data['text'][i].strip()
                    if txt:
                        conf = int(data['conf'][i]) if data['conf'][i] != -1 else 0
                        blocks.append({
                            "text": txt,
                            "left": int(data['left'][i]),
                            "top": int(data['top'][i]),
                            "width": int(data['width'][i]),
                            "height": int(data['height'][i]),
                            "confidence": conf
                        })
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
            processed = self.preprocess(img)
            data = pytesseract.image_to_data(processed, lang=lang, output_type=pytesseract.Output.DICT)
            text = pytesseract.image_to_string(processed, lang=lang)
            blocks = []
            for i in range(len(data['text'])):
                txt = data['text'][i].strip()
                if txt:
                    conf = int(data['conf'][i]) if data['conf'][i] != -1 else 0
                    blocks.append({
                        "text": txt,
                        "left": int(data['left'][i]),
                        "top": int(data['top'][i]),
                        "width": int(data['width'][i]),
                        "height": int(data['height'][i]),
                        "confidence": conf
                    })
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
