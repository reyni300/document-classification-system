"""
Повреждённый ответ модели.

Настоящий случай с чека Приорбанка: дата, сумма, валюта, номер и компания
в ответе mistral записаны правильно, а дальше ответ ломается — обрывается
или портится в extra_fields. Раньше из-за поломки в конце выбрасывался весь
ответ, и документ собирался одними правилами. Теперь берутся все целые поля.
"""
import unittest

from tests.helpers import ROOT  # noqa: F401  (настраивает пути и заглушки)
import llm.llm_agent as agent_module
from llm.llm_agent import LLMAgent, parse_json_prefix

HEAD = ('{"date":"07.02.2022","total":"258.00","currency":"BYN",'
        '"invoice_number":"547788888",'
        '"company":"Общество с ограниченной ответственностью \\"Датком Столица\\"",'
        '"vat":null,"document_type":"Банковская квитанция","extra_fields":{')

BROKEN = {
    'оборван без единой закрывающей скобки': HEAD + '"RRN":"2038990',
    'оборван после закрытого вложенного объекта': HEAD + '"Карта":{"номер":"****1234"},"Сумма":"258.0',
    'перенос строки внутри значения': HEAD + '"Адрес":"г. Минск,\nул. Ленина"}}',
    'табуляция внутри значения': HEAD + '"Кассир":"Иванов\tИ."}}',
    'значение без кавычек': HEAD + '"RRN":"203899067977","Карта": ****1234}}',
    'оборван на экранировании': HEAD + '"Назначение":"оплата \\"',
    'лишний текст после объекта': HEAD + '"RRN":"1"}} Надеюсь, это поможет!',
    'снаружи не закрыт': HEAD + '"RRN":"203899067977"}',
}


class BrokenAnswerTest(unittest.TestCase):
    def setUp(self):
        self._chat = agent_module.ollama.chat
        self.options = []

    def tearDown(self):
        agent_module.ollama.chat = self._chat

    def answer(self, content):
        def chat(**kwargs):
            self.options.append(kwargs.get('options'))
            return {'message': {'content': content}}
        agent_module.ollama.chat = chat
        agent = LLMAgent()
        return agent, agent._extract_from_text('Итого 258.00 BYN', page_num=1)

    def test_head_fields_survive_any_breakage(self):
        for name, content in BROKEN.items():
            with self.subTest(name):
                agent, result = self.answer(content)
                self.assertEqual(agent._last_errors, [], name)
                self.assertEqual(result['date'], '07.02.2022')
                self.assertEqual(result['total'], '258.00')
                self.assertEqual(result['currency'], 'BYN')
                self.assertEqual(result['invoice_number'], '547788888')
                self.assertIn('Датком Столица', result['company'])

    def test_whole_extra_fields_are_kept(self):
        # валидатор модели приводит extra_fields к виду «Ключ»: "строка"
        _, result = self.answer(BROKEN['оборван после закрытого вложенного объекта'])
        self.assertIn('****1234', str(result['extra_fields'].get('Карта')))
        self.assertNotIn('Сумма', result['extra_fields'])          # оборванное не берём

        _, result = self.answer(BROKEN['значение без кавычек'])
        extra = {k.lower(): v for k, v in result['extra_fields'].items()}
        self.assertEqual(extra.get('rrn'), '203899067977')

    def test_answer_limit_is_explicit(self):
        self.answer(BROKEN['снаружи не закрыт'])
        self.assertGreaterEqual(self.options[0]['num_predict'], 512)

    def test_no_json_at_all_still_fails(self):
        agent, _ = self.answer('Извините, не могу помочь')
        self.assertIn('нет JSON', agent._last_errors[0])

    def test_nothing_whole_fails(self):
        agent, _ = self.answer('{"date":"07.02.20')
        self.assertIn('не удалось разобрать', agent._last_errors[0])

    def test_complete_answer_unchanged(self):
        agent, result = self.answer(HEAD + '"RRN":"203899067977"}}')
        self.assertEqual(agent._last_errors, [])
        extra = {k.lower(): v for k, v in result['extra_fields'].items()}
        self.assertEqual(extra, {'rrn': '203899067977'})


class MultiPageTest(unittest.TestCase):
    """Каждая страница — своя запись с настоящим номером страницы."""

    def setUp(self):
        self._chat = agent_module.ollama.chat

    def tearDown(self):
        agent_module.ollama.chat = self._chat

    def run_pages(self, answers):
        calls = []

        def chat(**kwargs):
            calls.append(kwargs)
            answer = answers[len(calls) - 1]
            if isinstance(answer, Exception):
                raise answer
            return {'message': {'content': answer}}
        agent_module.ollama.chat = chat
        ocr = {'pages': [{'text': 'Баланс, лист %d' % n} for n in range(1, len(answers) + 1)]}
        return LLMAgent().interpret_multi(ocr), calls

    def test_two_pages_two_records(self):
        records, calls = self.run_pages(['{"total": "1"}', '{"total": "2"}'])
        self.assertEqual(len(calls), 2)                     # по вызову на страницу, без склейки
        self.assertEqual([r['_page'] for r in records], [1, 2])
        self.assertEqual([r['total'] for r in records], ['1.00', '2.00'])

    def test_failed_page_keeps_real_page_number(self):
        records, _ = self.run_pages(['нет тут JSON', '{"total": "2"}'])
        self.assertEqual([r['_page'] for r in records], [2])   # не «1» по порядку


class ParsePrefixTest(unittest.TestCase):
    def test_first_object_of_several(self):
        self.assertEqual(parse_json_prefix('{"a": 1}\n{"b": 2}'), {'a': 1})

    def test_nothing(self):
        self.assertIsNone(parse_json_prefix('нет тут JSON'))
        self.assertIsNone(parse_json_prefix('{"a": '))

    def test_trailing_comma(self):
        self.assertEqual(parse_json_prefix('{"a": 1, "b": [1, 2], }'), {'a': 1, 'b': [1, 2]})


if __name__ == '__main__':
    unittest.main()
