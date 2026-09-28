import unittest

from tests.helpers import ROOT  # noqa: F401  (настраивает пути)
from parser_service import result_schema as rs


def block(text, x, y, w=60, h=30, conf=91):
    return {'text': text, 'left': x, 'top': y, 'width': w, 'height': h, 'confidence': conf}


class ResultSchemaTest(unittest.TestCase):
    def setUp(self):
        self.record = {
            '_page': 2,
            'total': '258.00', 'currency': 'BYN', 'date': '12.09.2026',
            'company': None, 'invoice_number': 'N/A', 'vat': '',
            'document_type': 'чек', 'category': 'банковские комиссии',
            'validation_valid': False, 'validation_errors': ['Неверная дата'],
            'extra_fields': {'RRN': '203899067977', 'Сумма (2)': '10', 'Пустое': None},
            '_rule_fields': ['RRN'],
        }
        self.mapping = {
            'total': [block('258', 100, 50, 40), block('.00', 142, 50, 30)],
            'currency': [block('BYN', 180, 52, 45, 26)],
            'extra::RRN': [block('203899067977', 100, 90, 180)],
        }

    def fields(self):
        return {f['name']: f for f in rs.build_fields(self.record, self.mapping, page=2)}

    def test_empty_values_are_skipped(self):
        names = set(self.fields())
        self.assertEqual(names, {'total', 'currency', 'date', 'RRN', 'Сумма (2)'})

    def test_standard_fields_first_in_fixed_order(self):
        names = [f['name'] for f in rs.build_fields(self.record, self.mapping, page=2)]
        self.assertEqual(names[:3], ['total', 'date', 'currency'])

    def test_boxes_are_exact_block_coordinates(self):
        total = self.fields()['total']
        self.assertEqual(total['boxes'], [
            {'page': 2, 'x': 100, 'y': 50, 'w': 40, 'h': 30, 'text': '258', 'confidence': 91},
            {'page': 2, 'x': 142, 'y': 50, 'w': 30, 'h': 30, 'text': '.00', 'confidence': 91},
        ])
        self.assertEqual(total['bbox'], {'x': 100, 'y': 50, 'w': 72, 'h': 30})

    def test_unlocated_field_has_empty_boxes(self):
        date = self.fields()['date']
        self.assertEqual(date['boxes'], [])
        self.assertIsNone(date['bbox'])

    def test_source_of_each_field(self):
        f = self.fields()
        self.assertEqual(f['total']['source'], 'model')
        self.assertEqual(f['RRN']['source'], 'rules')
        self.assertEqual(f['Сумма (2)']['source'], 'model')
        self.assertEqual(f['RRN']['kind'], 'extra')
        self.assertEqual(f['total']['label'], 'Сумма')

    def test_whole_record_by_rules(self):
        self.record['extracted_by'] = 'rules'
        self.assertTrue(all(f['source'] == 'rules' for f in self.fields().values()))

    def test_document_and_summary(self):
        page = {'page': 2, 'img_width': 800, 'img_height': 600,
                'blocks': [block('258', 100, 50, 40)], 'grid': {'rows': [1, 2], 'cols': [3, 4]}}
        result = rs.build_result('a' * 32, 'чек.png', [self.record], [self.mapping], [page],
                                 {2: '/img/2'}, engine={'ocr': 'x'}, logs=['л'], warnings=['п'])
        self.assertEqual(result['schema_version'], rs.SCHEMA_VERSION)
        doc = result['documents'][0]
        self.assertEqual(doc['page'], 2)
        self.assertEqual(doc['validation'], {'valid': False, 'errors': ['Неверная дата']})
        self.assertEqual(doc['fields_total'], 5)
        self.assertEqual(doc['fields_located'], 3)
        self.assertEqual(result['pages'][0]['image_url'], '/img/2')
        self.assertEqual(result['pages'][0]['width'], 800)
        self.assertEqual(result['pages'][0]['words'][0]['x'], 100)
        self.assertEqual(result['pages'][0]['table_grid'], {'rows': [1, 2], 'cols': [3, 4]})
        self.assertEqual(result['summary']['total'], '258.00')
        self.assertEqual(result['summary']['currency'], 'BYN')
        self.assertEqual(result['summary']['fields_located'], 3)
        # служебные ключи записи (_page, _rule_fields) наружу не выходят
        self.assertNotIn('_rule_fields', str(result['documents']))

    def test_values_are_json_friendly(self):
        self.record['total'] = 258.0
        self.record['extra_fields']['Объект'] = object()
        f = self.fields()
        self.assertEqual(f['total']['value'], 258.0)
        self.assertIsInstance(f['Объект']['value'], str)


if __name__ == '__main__':
    unittest.main()
