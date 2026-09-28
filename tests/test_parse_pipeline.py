"""
Сквозная проверка разбора внутри сервиса: настоящие оркестратор, правила,
поиск координат и сборка результата; подменены только OCR (готовые блоки
слов) и модель — им в тестах взяться неоткуда.

Главное, что здесь проверяется, — координаты «один в один»: каждая рамка
поля в результате совпадает с распознанным словом на картинке страницы.
"""

import os
import shutil
import tempfile
import unittest

from PIL import Image

from tests.helpers import ROOT  # noqa: F401  (настраивает пути и заглушки)


W, H = 800, 600


def word(text, x, y, w, h=30, conf=93):
    return {'text': text, 'left': x, 'top': y, 'width': w, 'height': h, 'confidence': conf}


RECEIPT = [
    word('Сумма:', 50, 100, 95), word('258.00', 160, 100, 90), word('BYN', 262, 100, 50),
    word('Дата:', 50, 150, 75), word('12.09.2026', 160, 150, 150),
    word('RRN:', 50, 200, 65), word('203899067977', 160, 200, 185),
]


class FakeOCR:
    def __init__(self, blocks, size=(W, H)):
        self.blocks = blocks
        self.size = size
        self.seen_paths = []

    def extract(self, file_path, pages_dir=None):
        from ocr.ocr_module import OCRModule
        self.seen_paths.append((file_path, pages_dir))
        return {
            'pages': [{
                'page': 1,
                'text': OCRModule._text_from_blocks(self.blocks),
                'blocks': [dict(b) for b in self.blocks],
                'grid': None,
                'png_path': file_path,
                'img_width': self.size[0],
                'img_height': self.size[1],
            }],
            'timestamp': 'test',
            'ocr_engine': 'fake',
        }


class TwoPageOCR:
    """PDF из двух страниц: на каждой свои слова, своя картинка страницы."""

    def __init__(self, pages):
        self.pages = pages

    def extract(self, file_path, pages_dir=None):
        from ocr.ocr_module import OCRModule
        out = []
        for n, blocks in enumerate(self.pages, start=1):
            png = os.path.join(pages_dir, 'page-%d.png' % n)
            Image.new('RGB', (W, H), 'white').save(png)
            out.append({'page': n, 'text': OCRModule._text_from_blocks(blocks),
                        'blocks': [dict(b) for b in blocks], 'grid': None,
                        'png_path': png, 'img_width': W, 'img_height': H})
        return {'pages': out, 'timestamp': 'test', 'ocr_engine': 'fake'}


PAGE_2 = [word('Комиссия:', 50, 100, 150), word('1.20', 220, 100, 60),
          word('Код:', 50, 150, 70), word('777', 220, 150, 55)]


class FakeLLM:
    def __init__(self, records=None, error=None):
        self.records = records
        self.error = error
        self._last_ocr_hash = 'старый'
        self._last_result = ['чужой результат']

    def interpret_multi(self, ocr_data):
        assert self._last_ocr_hash is None, 'кэш модели не сброшен перед документом'
        if self.error:
            raise RuntimeError(self.error)
        return [dict(r, extra_fields=dict(r.get('extra_fields') or {})) for r in self.records]


class FakeClassifier:
    def classify(self, _data):
        return {'category': 'банковские комиссии'}


MODEL_RECORD = {
    '_page': 1, 'total': '258.00', 'currency': 'BYN', 'date': '12.09.2026',
    'company': None, 'invoice_number': None, 'vat': None,
    'document_type': 'чек', 'extra_fields': {},
}


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from parser_service.pipeline import DocumentPipeline
        cls.pipeline = DocumentPipeline()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.work_dir = os.path.join(self.tmp, 'a' * 32)
        os.makedirs(self.work_dir)
        self.source = os.path.join(self.work_dir, 'source.png')
        Image.new('RGB', (W, H), 'white').save(self.source)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_with(self, llm, ocr=None):
        orch = self.pipeline.orchestrator
        orch.ocr = ocr or FakeOCR(RECEIPT)
        orch.llm = self.pipeline.llm = llm
        orch.classifier = FakeClassifier()
        orch.translator = None
        return self.pipeline.run('a' * 32, self.source, 'чек.png', self.work_dir)

    @staticmethod
    def fields(result):
        return {f['name']: f for f in result['documents'][0]['fields']}

    def assert_boxes_are_ocr_words(self, result):
        words = {(p['number'], w['x'], w['y'], w['w'], w['h'], w['text'])
                 for p in result['pages'] for w in p['words']}
        for doc in result['documents']:
            for f in doc['fields']:
                for b in f['boxes']:
                    b = dict(b, key=(b['page'], b['x'], b['y'], b['w'], b['h'], b['text']))
                    self.assertIn(b['key'], words,
                                  'рамка поля «%s» не совпадает ни с одним словом' % f['label'])
                    self.assertEqual(b['page'], doc['page'])

    def test_model_fields_get_exact_coordinates(self):
        result = self.run_with(FakeLLM([MODEL_RECORD]))
        f = self.fields(result)

        self.assertEqual(f['total']['value'], '258.00')
        self.assertEqual(f['total']['source'], 'model')
        self.assertEqual([(b['x'], b['y'], b['w'], b['h']) for b in f['total']['boxes']],
                         [(160, 100, 90, 30)])
        self.assertEqual(f['currency']['boxes'][0]['text'], 'BYN')
        self.assertEqual(f['date']['boxes'][0]['text'], '12.09.2026')
        self.assert_boxes_are_ocr_words(result)

    def test_rules_add_what_model_missed(self):
        result = self.run_with(FakeLLM([MODEL_RECORD]))
        rrn = self.fields(result)['RRN']
        self.assertEqual(rrn['value'], '203899067977')
        self.assertEqual(rrn['source'], 'rules')
        self.assertEqual(rrn['boxes'][0]['x'], 160)

    def test_falls_back_to_rules_without_model(self):
        result = self.run_with(FakeLLM(error='llama-server process has terminated: signal: killed'))
        doc = result['documents'][0]
        self.assertEqual(doc['extracted_by'], 'rules')
        self.assertTrue(all(f['source'] == 'rules' for f in doc['fields']))
        self.assertIn('RRN', self.fields(result))
        self.assertTrue(any('Модель не ответила' in w for w in result['warnings']))
        self.assert_boxes_are_ocr_words(result)

    def test_page_image_saved_next_to_result_with_same_size(self):
        result = self.run_with(FakeLLM([MODEL_RECORD]))
        page = result['pages'][0]
        self.assertEqual((page['width'], page['height']), (W, H))
        self.assertEqual(page['image_url'], '/api/v1/parses/%s/pages/1/image' % ('a' * 32))
        with Image.open(os.path.join(self.work_dir, 'page-1.png')) as img:
            self.assertEqual(img.size, (W, H))
        self.assertFalse(any('размер картинки' in w for w in result['warnings']))

    def test_size_mismatch_is_reported(self):
        result = self.run_with(FakeLLM([MODEL_RECORD]), ocr=FakeOCR(RECEIPT, size=(W * 2, H)))
        self.assertTrue(any('размер картинки' in w for w in result['warnings']))

    def test_uses_own_work_dir_and_cleans_intermediate(self):
        ocr = FakeOCR(RECEIPT)
        self.run_with(FakeLLM([MODEL_RECORD]), ocr=ocr)
        processed, pages_dir = ocr.seen_paths[0]
        self.assertEqual(pages_dir, self.work_dir)
        self.assertEqual(os.path.dirname(processed), self.work_dir)   # не общий data/temp_proc.png
        self.assertFalse(os.path.exists(processed))

    def test_nothing_extracted_raises_with_logs(self):
        from parser_service.worker import ParseError
        with self.assertRaises(ParseError) as ctx:
            self.run_with(FakeLLM(error='модель молчит'), ocr=FakeOCR([]))
        self.assertTrue(ctx.exception.logs)

    def test_every_page_gets_its_own_document(self):
        """Модель дала запись только первой странице — вторую собирают правила."""
        pdf = os.path.join(self.work_dir, 'source.pdf')
        open(pdf, 'wb').close()
        orch = self.pipeline.orchestrator
        orch.ocr = TwoPageOCR([RECEIPT, PAGE_2])
        orch.llm = self.pipeline.llm = FakeLLM([MODEL_RECORD])
        orch.classifier = FakeClassifier()
        orch.translator = None
        result = self.pipeline.run('a' * 32, pdf, 'два листа.pdf', self.work_dir)

        self.assertEqual([d['page'] for d in result['documents']], [1, 2])
        first, second = result['documents']
        self.assertEqual(first['extracted_by'], 'model')
        self.assertEqual(second['extracted_by'], 'rules')
        values = {f['name']: f for f in second['fields']}
        self.assertEqual(values['Комиссия']['value'], '1.20')
        self.assertEqual(values['Комиссия']['boxes'][0]['page'], 2)     # рамка на своём листе
        self.assertEqual(len(result['pages']), 2)
        self.assert_boxes_are_ocr_words(result)

    def test_result_metadata(self):
        result = self.run_with(FakeLLM([MODEL_RECORD]))
        self.assertEqual(result['schema_version'], '1.0')
        self.assertEqual(result['source'], {'filename': 'чек.png'})
        self.assertEqual(result['documents'][0]['category'], 'банковские комиссии')
        self.assertIn('rules', result['engine'])
        self.assertEqual(result['summary']['total'], '258.00')
        self.assertTrue(result['logs'])


if __name__ == '__main__':
    unittest.main()
