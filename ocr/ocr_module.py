import cv2
import numpy as np
import pytesseract
from pdf2image import convert_from_path
from datetime import datetime
import yaml

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

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
            images = convert_from_path(file_path)
            for page_num, img in enumerate(images):
                img_np = np.array(img)
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
                    "blocks": blocks
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
                "blocks": blocks
            })

        return {
            "pages": all_pages,
            "timestamp": datetime.now().isoformat(),
            "ocr_engine": f"Tesseract {pytesseract.get_tesseract_version()}"
        }